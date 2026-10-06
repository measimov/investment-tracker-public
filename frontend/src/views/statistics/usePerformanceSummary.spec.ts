import { describe, expect, it, vi, beforeEach } from 'vitest'
vi.mock('@/api', () => ({ default: { getPerformanceSummary: vi.fn() } }))
vi.mock('@/utils/showApiError', () => ({ showApiError: vi.fn() }))
import api from '@/api'
import { showApiError } from '@/utils/showApiError'
import { usePerformanceSummary } from './usePerformanceSummary'

const payload = {
  current_performance: { unrealized_pnl_cny: 0, holdings_detail: [] },
  realized_pnl: { realized_pnl: 0 },
  dividend_summary: { total_dividend_net: 0 },
  total_realized_return: { total_realized_return: 0 },
  account_return: { total_return: 0 }
}
describe('performance summary failures', () => {
  beforeEach(() => vi.clearAllMocks())
  it('failed first load retains missing metrics instead of inventing zero', async () => {
    vi.mocked(api.getPerformanceSummary).mockRejectedValueOnce(new Error('summary unavailable'))
    const summary = usePerformanceSummary()
    expect(await summary.load()).toBe(false)
    expect(summary.state.accountReturn.total_return).toBeNull()
    expect(summary.state.dividendSummary.total_dividend_net).toBeNull()
    expect(summary.state.loaded).toBe(false)
    expect(summary.state.error).toBe('summary unavailable')
    expect(showApiError).toHaveBeenCalledOnce()
  })
  it('an error keeps the last successful data and a successful retry clears it', async () => {
    vi.mocked(api.getPerformanceSummary)
      .mockResolvedValueOnce({ data: payload } as never)
      .mockRejectedValueOnce(new Error('failed refresh'))
      .mockResolvedValueOnce({ data: payload } as never)
    const summary = usePerformanceSummary()
    expect(await summary.load()).toBe(true)
    expect(await summary.load()).toBe(false)
    expect(summary.state.accountReturn.total_return).toBe(0)
    expect(summary.state.loaded).toBe(true)
    expect(summary.state.error).toBe('failed refresh')
    expect(await summary.load()).toBe(true)
    expect(summary.state.error).toBe('')
  })
  it('incomplete responses fail without mixing old and new financial blocks', async () => {
    vi.mocked(api.getPerformanceSummary).mockResolvedValueOnce({
      data: { account_return: { total_return: 100 } }
    } as never)
    const summary = usePerformanceSummary()
    expect(await summary.load()).toBe(false)
    expect(summary.state.accountReturn.total_return).toBeNull()
    expect(summary.state.error).toBe('返回的业绩摘要不完整')
  })
  it('late errors cannot overwrite a newer successful valuation', async () => {
    let reject!: (reason: Error) => void
    const old = new Promise((_resolve, no) => (reject = no))
    vi.mocked(api.getPerformanceSummary)
      .mockReturnValueOnce(old as never)
      .mockResolvedValueOnce({ data: payload } as never)
    const summary = usePerformanceSummary()
    const first = summary.load()
    expect(await summary.load({ '600000:A股': 10 })).toBe(true)
    reject(new Error('obsolete'))
    expect(await first).toBe(false)
    expect(summary.state.error).toBe('')
    expect(showApiError).not.toHaveBeenCalled()
  })
})
