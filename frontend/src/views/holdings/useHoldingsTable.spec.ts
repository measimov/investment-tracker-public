import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('@/api', () => ({
  default: {
    getHoldings: vi.fn(),
    updateHoldingPrice: vi.fn(),
    getExchangeRates: vi.fn().mockResolvedValue({ data: [] })
  }
}))
vi.mock('../../api', () => ({
  default: {
    getHoldings: vi.fn(),
    updateHoldingPrice: vi.fn(),
    getExchangeRates: vi.fn().mockResolvedValue({ data: [] })
  }
}))

import api from '@/api'
import { useHoldingsTable } from './useHoldingsTable'

function row(id: number, accountId: number, overrides: Record<string, unknown> = {}) {
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
    current_price: '20',
    price_updated_at: '2026-09-26T00:00:00Z',
    updated_at: '2026-09-26T00:00:00Z',
    ...overrides
  }
}

describe('useHoldingsTable merged rows', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(api.getHoldings).mockReset()
    vi.mocked(api.updateHoldingPrice).mockReset()
  })

  it('merges accounts and saves a price with one request per security (PR #216 review P2)', async () => {
    vi.mocked(api.getHoldings).mockResolvedValue({ data: [row(1, 11), row(2, 12)] } as never)
    vi.mocked(api.updateHoldingPrice).mockResolvedValue({ data: row(1, 11) } as never)
    const table = useHoldingsTable({ isUnmounted: () => false })
    await table.loadHoldings()

    expect(table.rows).toHaveLength(1)
    const merged = table.rows[0]
    expect(merged.quantity).toBe(200)
    expect(merged.totalCost).toBe(60000)
    expect(merged.accounts.map((h) => h.id)).toEqual([1, 2])

    table.state.currentPrices['00700:港股'] = 30
    await table.savePriceToDatabase(merged)
    table.state.currentPrices['00700:港股'] = 40
    await table.savePriceToDatabase(merged)

    // 后端按标的更新全部账户：每次保存只发一个请求，不再逐账户循环（旧批次的后续写入会覆盖新价）
    expect(api.updateHoldingPrice).toHaveBeenCalledTimes(2)
    expect(vi.mocked(api.updateHoldingPrice).mock.calls.map((call) => call[1])).toEqual([30, 40])
  })

  it('price edit: enter/blur commits once, Esc cancels without saving (#217)', async () => {
    vi.mocked(api.getHoldings).mockResolvedValue({ data: [row(1, 11)] } as never)
    vi.mocked(api.updateHoldingPrice).mockResolvedValue({
      data: row(1, 11, { current_price: '25', price_source: 'manual', price_as_of: null })
    } as never)
    const table = useHoldingsTable({ isUnmounted: () => false })
    await table.loadHoldings()
    const merged = table.rows[0]

    table.startPriceEdit(merged)
    expect(table.isEditingPrice(merged)).toBe(true)
    expect(table.state.priceDraft).toBe(20)
    table.state.priceDraft = 99
    table.cancelPriceEdit()
    await table.commitPriceEdit(merged) // Esc 之后的失焦：已退出编辑态，不保存
    expect(api.updateHoldingPrice).not.toHaveBeenCalled()
    expect(table.state.currentPrices['00700:港股']).toBe(20)

    table.startPriceEdit(merged)
    table.state.priceDraft = 25
    await table.commitPriceEdit(merged)
    await table.commitPriceEdit(merged) // 回车后紧跟的失焦不重复提交
    expect(api.updateHoldingPrice).toHaveBeenCalledTimes(1)
    expect(table.state.currentPrices['00700:港股']).toBe(25)
    // 服务端回填的 price_source=manual 立刻反映到行上（「手工」标）
    expect(table.priceInfoOf(table.rows[0])?.manual).toBe(true)

    // 未改价 / 非正数：不发请求
    table.startPriceEdit(table.rows[0])
    await table.commitPriceEdit(table.rows[0])
    table.startPriceEdit(table.rows[0])
    table.state.priceDraft = 0
    await table.commitPriceEdit(table.rows[0])
    expect(api.updateHoldingPrice).toHaveBeenCalledTimes(1)
  })

  it('account view: only the clicked row edits, another row blurring does not cancel it (#226 P2)', async () => {
    vi.mocked(api.getHoldings).mockResolvedValue({ data: [row(1, 11), row(2, 12)] } as never)
    vi.mocked(api.updateHoldingPrice).mockResolvedValue({
      data: row(1, 11, { current_price: '26', price_source: 'manual', price_as_of: null })
    } as never)
    const table = useHoldingsTable({ isUnmounted: () => false })
    table.setViewMode('account')
    await table.loadHoldings()
    const [first, second] = table.rows
    expect(first.key).not.toBe(second.key)
    expect(table.priceKey(first)).toBe(table.priceKey(second)) // 同一标的共用价格键

    table.startPriceEdit(first)
    expect(table.isEditingPrice(first)).toBe(true)
    expect(table.isEditingPrice(second)).toBe(false) // 只挂一个输入框

    // 另一行的失焦（此前第二个输入框抢到焦点后再失焦）不得结束第一行的编辑
    await table.commitPriceEdit(second)
    expect(table.isEditingPrice(first)).toBe(true)
    expect(api.updateHoldingPrice).not.toHaveBeenCalled()

    table.state.priceDraft = 26
    await table.commitPriceEdit(first)
    expect(api.updateHoldingPrice).toHaveBeenCalledTimes(1)
    // 价格仍按标的共享：两行都显示新价
    expect(table.priceOf(first)).toBe(26)
    expect(table.priceOf(second)).toBe(26)
    table.setViewMode('merged')
  })

  it('sorts by CNY amount with unconvertible rows at the bottom in both directions', async () => {
    vi.mocked(api.getHoldings).mockResolvedValue({
      data: [
        // 汇率表为空：HKD 行无法折人民币
        row(1, 11),
        row(2, 11, {
          symbol: '600000',
          market: 'A股',
          currency: 'CNY',
          total_cost: '1000',
          current_price: '12'
        }),
        row(3, 11, {
          symbol: '600001',
          market: 'A股',
          currency: 'CNY',
          total_cost: '1000',
          current_price: '8'
        })
      ]
    } as never)
    const table = useHoldingsTable({ isUnmounted: () => false })
    await table.loadHoldings()

    // 默认：市值降序
    expect(table.rows.map((r) => r.symbol)).toEqual(['600000', '600001', '00700'])
    table.setSort({ prop: 'profit', order: 'ascending' })
    expect(table.rows.map((r) => r.symbol)).toEqual(['600001', '600000', '00700'])
    expect(table.profitCNYOf(table.rows[0])).toBe(-200)
    expect(table.profitCNYOf(table.rows[2])).toBeNull()
    table.setSort({ prop: 'profit', order: 'descending' })
    expect(table.rows.map((r) => r.symbol)).toEqual(['600000', '600001', '00700'])

    expect(table.securityCount).toBe(3)
    expect(table.pricedSecurityCount).toBe(2)
    expect(table.marketSubtotals.map((m) => [m.market, m.count, m.pricedCount])).toEqual([
      ['A股', 2, 2],
      ['港股', 1, 0]
    ])
  })
})
