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
import { useHoldingsStore } from '@/stores/holdings'
import { useHoldingsTable } from './useHoldingsTable'

vi.mock('@/utils/showApiError', () => ({ showApiError: vi.fn() }))

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

  it('a slow old market cannot overwrite the latest rows or current prices', async () => {
    let release!: (value: unknown) => void
    const old = new Promise((resolve) => (release = resolve))
    vi.mocked(api.getHoldings)
      .mockReturnValueOnce(old as never)
      .mockResolvedValueOnce({
        data: [row(2, 11, { symbol: '600000', market: 'A股', current_price: '12' })]
      } as never)
    const table = useHoldingsTable({ isUnmounted: () => false })
    table.state.selectedMarket = '港股'
    const oldLoad = table.loadHoldings({ force: true })
    table.state.selectedMarket = 'A股'
    await table.loadHoldings({ force: true })
    release({ data: [row(1, 11, { current_price: '999' })] })
    await oldLoad
    expect(table.state.holdings.map((h) => h.symbol)).toEqual(['600000'])
    expect(table.state.currentPrices['600000:A股']).toBe(12)
    expect(table.state.currentPrices['00700:港股']).toBeUndefined()
    expect(table.state.loading).toBe(false)
  })

  it('merges accounts and saves a price with one request per security (PR #216 review P2)', async () => {
    vi.mocked(api.getHoldings).mockResolvedValue({ data: [row(1, 11), row(2, 12)] } as never)
    vi.mocked(api.updateHoldingPrice)
      .mockResolvedValueOnce({ data: row(1, 11, { current_price: '30' }) } as never)
      .mockResolvedValueOnce({ data: row(1, 11, { current_price: '40' }) } as never)
    const table = useHoldingsTable({ isUnmounted: () => false })
    await table.loadHoldings()

    expect(table.rows).toHaveLength(1)
    const merged = table.rows[0]
    expect(merged.quantity).toBe(200)
    expect(merged.totalCost).toBe(60000)
    expect(merged.accounts.map((h) => h.id)).toEqual([1, 2])

    await table.savePriceToDatabase(merged, 30)
    await table.savePriceToDatabase(merged, 40)

    // 后端按标的更新全部账户：每次保存只发一个请求，不再逐账户循环（旧批次的后续写入会覆盖新价）
    expect(api.updateHoldingPrice).toHaveBeenCalledTimes(2)
    expect(vi.mocked(api.updateHoldingPrice).mock.calls.map((call) => call[1])).toEqual([30, 40])
  })

  it('pending and failed drafts keep the confirmed price and valuation; only server confirmation updates them', async () => {
    vi.mocked(api.getHoldings).mockResolvedValue({ data: [row(1, 11), row(2, 12)] } as never)
    const table = useHoldingsTable({ isUnmounted: () => false })
    await table.loadHoldings()
    const merged = table.rows[0]
    let reject!: (reason: Error) => void
    vi.mocked(api.updateHoldingPrice).mockReturnValueOnce(
      new Promise((_, fail) => (reject = fail)) as never
    )
    table.startPriceEdit(merged)
    table.state.priceDraft = 25
    const failed = table.commitPriceEdit(merged)
    expect(table.priceOf(merged)).toBe(20)
    expect(table.marketValueOf(merged)).toBe(4000)
    expect(table.isEditingPrice(merged)).toBe(false)
    await table.commitPriceEdit(merged)
    expect(api.updateHoldingPrice).toHaveBeenCalledTimes(1)
    reject(new Error('明确虚构保存失败'))
    await failed
    expect(table.priceOf(merged)).toBe(20)
    expect(table.marketValueOf(merged)).toBe(4000)

    vi.mocked(api.updateHoldingPrice).mockResolvedValueOnce({
      data: row(1, 11, { current_price: '24.5678', price_source: 'manual', price_as_of: null })
    } as never)
    table.startPriceEdit(merged)
    table.state.priceDraft = 25
    await table.commitPriceEdit(merged)
    expect(table.priceOf(table.rows[0])).toBe(24.5678)
    expect(table.marketValueOf(table.rows[0])).toBeCloseTo(4913.56, 8)
    expect(table.rows[0].accounts.map((holding) => holding.current_price)).toEqual([
      '24.5678',
      '24.5678'
    ])
    expect(table.priceInfoOf(table.rows[0])?.manual).toBe(true)
  })

  it.each(['success', 'failure'])(
    'editing back to the confirmed price sends the latest intent despite an older pending save (%s)',
    async (outcome) => {
      vi.mocked(api.getHoldings).mockResolvedValue({
        data: [row(1, 11, { current_price: '10' }), row(2, 12, { current_price: '10' })]
      } as never)
      const table = useHoldingsTable({ isUnmounted: () => false })
      await table.loadHoldings()
      const merged = table.rows[0]
      let reply!: (value: unknown) => void, reject!: (failure: Error) => void
      vi.mocked(api.updateHoldingPrice)
        .mockReturnValueOnce(
          new Promise((resolve, fail) => {
            reply = resolve
            reject = fail
          }) as never
        )
        .mockResolvedValueOnce({
          data: row(1, 11, { current_price: '10', price_source: 'manual', price_as_of: null })
        } as never)
      table.startPriceEdit(merged)
      table.state.priceDraft = 20
      const older = table.commitPriceEdit(merged)
      expect(table.priceOf(merged)).toBe(10)
      expect(table.marketValueOf(merged)).toBe(2000)
      table.startPriceEdit(merged)
      table.state.priceDraft = 10
      await table.commitPriceEdit(merged)
      await table.commitPriceEdit(merged) // Enter后blur不重复
      expect(vi.mocked(api.updateHoldingPrice).mock.calls.map((call) => call[1])).toEqual([20, 10])
      if (outcome === 'success') reply({ data: row(1, 11, { current_price: '20' }) })
      else reject(new Error('UI明确虚构旧保存失败'))
      await older
      expect(table.priceOf(merged)).toBe(10)
      expect(table.marketValueOf(merged)).toBe(2000)
      expect(table.rows[0].accounts.map((holding) => holding.current_price)).toEqual(['10', '10'])
      table.startPriceEdit(table.rows[0])
      await table.commitPriceEdit(table.rows[0])
      expect(api.updateHoldingPrice).toHaveBeenCalledTimes(2)
    }
  )

  it('an older completion cannot clear a newer pending target or duplicate the same intent', async () => {
    vi.mocked(api.getHoldings).mockResolvedValue({
      data: [row(1, 11, { current_price: '10' })]
    } as never)
    const table = useHoldingsTable({ isUnmounted: () => false })
    await table.loadHoldings()
    const merged = table.rows[0]
    let first!: (value: unknown) => void, second!: (value: unknown) => void
    vi.mocked(api.updateHoldingPrice)
      .mockReturnValueOnce(new Promise((resolve) => (first = resolve)) as never)
      .mockReturnValueOnce(new Promise((resolve) => (second = resolve)) as never)
    table.startPriceEdit(merged)
    table.state.priceDraft = 20
    const old = table.commitPriceEdit(merged)
    table.startPriceEdit(merged)
    table.state.priceDraft = 30
    const latest = table.commitPriceEdit(merged)
    first({ data: row(1, 11, { current_price: '20' }) })
    await old
    expect(table.priceOf(merged)).toBe(10)
    table.startPriceEdit(merged)
    table.state.priceDraft = 30
    await table.commitPriceEdit(merged)
    expect(vi.mocked(api.updateHoldingPrice).mock.calls.map((call) => call[1])).toEqual([20, 30])
    second({ data: row(1, 11, { current_price: '30' }) })
    await latest
    expect(table.priceOf(merged)).toBe(30)
    table.startPriceEdit(merged)
    table.state.priceDraft = 10
    vi.mocked(api.updateHoldingPrice).mockResolvedValueOnce({
      data: row(1, 11, { current_price: '10' })
    } as never)
    await table.commitPriceEdit(merged)
    expect(table.priceOf(merged)).toBe(10)
    expect(api.updateHoldingPrice).toHaveBeenCalledTimes(3)
  })

  it('late older confirmations and saves finishing after unmount cannot overwrite the current display', async () => {
    vi.mocked(api.getHoldings).mockResolvedValue({ data: [row(1, 11)] } as never)
    let gone = false
    const table = useHoldingsTable({ isUnmounted: () => gone })
    await table.loadHoldings()
    const merged = table.rows[0]
    let oldReply!: (value: unknown) => void
    vi.mocked(api.updateHoldingPrice)
      .mockReturnValueOnce(new Promise((resolve) => (oldReply = resolve)) as never)
      .mockResolvedValueOnce({ data: row(1, 11, { current_price: '40' }) } as never)
    const old = table.savePriceToDatabase(merged, 30)
    await table.savePriceToDatabase(merged, 40)
    expect(table.priceOf(merged)).toBe(40)
    oldReply({ data: row(1, 11, { current_price: '30' }) })
    await old
    expect(table.priceOf(merged)).toBe(40)

    vi.mocked(api.updateHoldingPrice).mockReturnValueOnce(
      new Promise((resolve) => (oldReply = resolve)) as never
    )
    const closing = table.savePriceToDatabase(merged, 50)
    gone = true
    oldReply({ data: row(1, 11, { current_price: '50' }) })
    await closing
    expect(table.priceOf(merged)).toBe(40)
  })

  it('a confirmation read finishing after a market change cannot replace the selected rows', async () => {
    let release!: (value: unknown) => void
    vi.mocked(api.getHoldings)
      .mockResolvedValueOnce({ data: [row(1, 11)] } as never)
      .mockReturnValueOnce(new Promise((resolve) => (release = resolve)) as never)
      .mockResolvedValueOnce({
        data: [row(2, 11, { symbol: '600000', market: 'A股', current_price: '12' })]
      } as never)
    vi.mocked(api.updateHoldingPrice).mockImplementationOnce(async () => {
      useHoldingsStore().invalidate()
      return { data: row(1, 11, { current_price: '25' }) } as never
    })
    const table = useHoldingsTable({ isUnmounted: () => false })
    table.state.selectedMarket = '港股'
    await table.loadHoldings()
    const save = table.savePriceToDatabase(table.rows[0], 25)
    await vi.waitFor(() => expect(api.getHoldings).toHaveBeenCalledTimes(2))
    table.state.selectedMarket = 'A股'
    await table.loadHoldings()
    release({ data: [row(1, 11, { current_price: '25' })] })
    await save
    expect(table.state.holdings.map((holding) => holding.symbol)).toEqual(['600000'])
    expect(table.state.currentPrices['600000:A股']).toBe(12)
    expect(table.state.currentPrices['00700:港股']).toBe(25)
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

  it('keyword/tag filters narrow rows in both views; summary and focus ignore them (#235)', async () => {
    vi.mocked(api.getHoldings).mockResolvedValue({
      data: [
        row(1, 11),
        row(2, 12),
        row(3, 11, { symbol: '600036', name: '招商银行', market: 'A股', currency: 'CNY' })
      ]
    } as never)
    const table = useHoldingsTable({
      isUnmounted: () => false,
      tagSourceOf: (r) => ({
        industry: r.symbol === '600036' ? '银行' : '软件服务',
        aiTags: r.symbol === '600036' ? ['高股息', '估值偏低'] : ['业绩增长'],
        riskLevel: r.symbol === '600036' ? 'low' : 'high',
        opinionTags: [],
        eventTypes: []
      })
    })
    await table.loadHoldings()
    expect(table.rows).toHaveLength(2)

    table.state.keyword = '700'
    expect(table.rows.map((r) => r.symbol)).toEqual(['00700'])
    expect(table.isFiltered).toBe(true)
    expect(table.totalRowCount).toBe(2)
    // 按账户视图同一口径：两行（两个账户）都留下
    table.setViewMode('account')
    expect(table.rows.map((r) => r.key).sort()).toEqual(['00700:港股:11', '00700:港股:12'])
    table.state.keyword = '招商'
    expect(table.rows.map((r) => r.symbol)).toEqual(['600036'])
    table.setViewMode('merged')

    // 标签：按标的去重计数，筛选用全部 AI 标签
    table.clearFilters()
    const ai = table.tagOptions.find((group) => group.group === 'ai')!
    expect(ai.options.map((o) => [o.label, o.count])).toContainEqual(['业绩增长', 1])
    table.state.selectedTags = ['ai:估值偏低']
    expect(table.rows.map((r) => r.symbol)).toEqual(['600036'])
    table.state.selectedTags = ['ai:估值偏低', 'ai:业绩增长', 'risk:high']
    expect(table.rows.map((r) => r.symbol)).toEqual(['00700'])
    // 行业组：选项来自当前持仓，与其他组 AND
    const industry = table.tagOptions.find((group) => group.group === 'industry')!
    expect(industry.options.map((o) => o.label).sort()).toEqual(['软件服务', '银行'].sort())
    table.state.selectedTags = ['industry:银行']
    expect(table.rows.map((r) => r.symbol)).toEqual(['600036'])
    table.state.selectedTags = ['industry:银行', 'risk:high']
    expect(table.rows).toHaveLength(0)

    // 汇总不受筛选影响
    expect(table.securityCount).toBe(2)

    // 深链定位：省略前导零也算同一只；未持有时 focusHeld=false
    table.clearFilters()
    table.state.focus = { symbol: '700', market: '港股' }
    expect(table.focusHeld).toBe(true)
    expect(table.rows.filter((r) => table.isFocused(r)).map((r) => r.symbol)).toEqual(['00700'])
    table.state.focus = { symbol: '00700', market: 'A股' }
    expect(table.focusHeld).toBe(false)
    table.state.focus = { symbol: '600036', market: null }
    expect(table.focusHeld).toBe(true)
    table.state.selectedAccount = 12
    expect(table.focusVisible).toBe(false)
  })
})
