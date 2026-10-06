import { beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope } from 'vue'
vi.mock('@/api', () => ({
  default: {
    getCorporateActions: vi.fn(),
    getCorporateActionsCount: vi.fn(),
    getCorporateActionsSummary: vi.fn()
  }
}))
vi.mock('@/utils/showApiError', () => ({ showApiError: vi.fn() }))
import api from '@/api'
import { useCorporateActionsList } from './useCorporateActionsList'

describe('company action summary follows its own successful query', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.getCorporateActions).mockResolvedValue({ data: [] } as never)
    vi.mocked(api.getCorporateActionsCount).mockResolvedValue({ data: { total: 0 } } as never)
  })
  it('a delayed old market summary cannot replace the latest market result', async () => {
    let release!: (value: unknown) => void
    vi.mocked(api.getCorporateActionsSummary)
      .mockReturnValueOnce(new Promise((resolve) => (release = resolve)) as never)
      .mockResolvedValueOnce({
        data: { total_count: 1, cash_dividends: { net_dividend: 22.22 } }
      } as never)
    const scope = effectScope(),
      list = scope.run(useCorporateActionsList)!
    list.filters.market = 'A股'
    const old = list.loadActions()
    await vi.waitFor(() => expect(api.getCorporateActionsSummary).toHaveBeenCalledTimes(1))
    list.filters.market = '港股'
    await list.loadActions()
    release({ data: { total_count: 1, cash_dividends: { net_dividend: 111.11 } } })
    await old
    expect(list.summary?.cash_dividends?.net_dividend).toBe(22.22)
    expect(list.summaryError).toBe('')
    expect(list.summaryLoading).toBe(false)
    expect(api.getCorporateActionsSummary).toHaveBeenNthCalledWith(1, { market: 'A股' })
    expect(api.getCorporateActionsSummary).toHaveBeenNthCalledWith(2, { market: '港股' })
    scope.stop()
  })
  it('a failed first summary stays unknown and a later failure preserves the last known values', async () => {
    vi.mocked(api.getCorporateActionsSummary)
      .mockRejectedValueOnce(new Error('temporarily unavailable'))
      .mockResolvedValueOnce({
        data: {
          total_count: 0,
          cash_dividends: { total_dividend: null, total_tax: null, net_dividend: 0 }
        }
      } as never)
      .mockRejectedValueOnce(new Error('refresh failed'))
    const scope = effectScope(),
      list = scope.run(useCorporateActionsList)!
    await list.loadActions()
    expect(list.summaryHasLoaded).toBe(false)
    expect(list.summary).toBe(null)
    expect(list.summaryError).toBe('temporarily unavailable')
    await list.loadActions()
    expect(list.summaryHasLoaded).toBe(true)
    expect(list.summary?.cash_dividends?.total_tax).toBe(null)
    expect(list.summary?.cash_dividends?.net_dividend).toBe(0)
    await list.loadActions()
    expect(list.summary?.cash_dividends?.net_dividend).toBe(0)
    expect(list.summaryError).toBe('refresh failed')
    scope.stop()
  })
})
