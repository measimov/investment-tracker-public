import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('@/api', () => ({
  default: { getHoldings: vi.fn(), updateHoldingPrice: vi.fn() }
}))
vi.mock('../api', () => ({
  default: { getHoldings: vi.fn(), updateHoldingPrice: vi.fn() }
}))

import api from '../api'
import { useHoldingsStore, type Holding } from './holdings'

function holding(id: number, accountId: number, price: string): Holding {
  return {
    id,
    user_id: 1,
    broker_account_id: accountId,
    symbol: '00700',
    name: '腾讯控股',
    market: '港股',
    quantity: '100',
    avg_cost: '300',
    total_cost: '30000',
    currency: 'HKD',
    current_price: price,
    price_updated_at: '2026-09-26T00:00:00Z',
    price_as_of: '2026-09-25',
    price_source: 'tencent-quote',
    updated_at: '2026-09-26T00:00:00Z'
  } as unknown as Holding
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => (resolve = r))
  return { promise, resolve }
}

describe('holdings store price updates', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(api.getHoldings).mockReset()
    vi.mocked(api.updateHoldingPrice).mockReset()
  })

  it('a slower older save cannot overwrite a newer price (PR #216 review P2)', async () => {
    const store = useHoldingsStore()
    vi.mocked(api.getHoldings).mockResolvedValue({
      data: [holding(1, 11, '20'), holding(2, 12, '20')]
    } as never)
    await store.fetchHoldings()

    const first = deferred<{ data: Holding }>()
    const second = deferred<{ data: Holding }>()
    vi.mocked(api.updateHoldingPrice)
      .mockReturnValueOnce(first.promise as never)
      .mockReturnValueOnce(second.promise as never)

    const saveOld = store.updateHoldingPrice(1, 30, '00700:港股')
    const saveNew = store.updateHoldingPrice(1, 40, '00700:港股')
    // 新请求先返回，旧请求后返回（服务端已按 40 落库，旧响应只是迟到）
    second.resolve({ data: holding(1, 11, '40') })
    await saveNew
    first.resolve({ data: holding(1, 11, '30') })
    await saveOld

    // 每次保存只发一个请求（后端按标的更新全部账户），不会再有旧批次的后续写入
    expect(api.updateHoldingPrice).toHaveBeenCalledTimes(2)
    const cached = await store.fetchHoldings()
    expect(cached.map((h) => h.current_price)).toEqual(['40', '40'])
  })

  it('syncs the price of sibling account rows from the server response', async () => {
    const store = useHoldingsStore()
    vi.mocked(api.getHoldings).mockResolvedValue({
      data: [holding(1, 11, '20'), holding(2, 12, '20')]
    } as never)
    await store.fetchHoldings()
    vi.mocked(api.updateHoldingPrice).mockResolvedValue({
      data: { ...holding(1, 11, '25'), price_as_of: null, price_source: 'manual' }
    } as never)

    await store.updateHoldingPrice(1, 25, '00700:港股')

    const cached = await store.fetchHoldings()
    expect(cached.map((h) => [h.id, h.current_price, h.broker_account_id])).toEqual([
      [1, '25', 11],
      [2, '25', 12]
    ])
    // 行情日期与来源随价格整组同步（#217）：兄弟行也要变成手工价
    expect(cached.map((h) => [h.price_as_of, h.price_source])).toEqual([
      [null, 'manual'],
      [null, 'manual']
    ])
  })
})
