import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api', () => ({ default: { getPeriodPnl: vi.fn() } }))
import api from '@/api'
import { usePeriodPnl } from './usePeriodPnl'

const payload = { periods: { mtd: { pnl_cny: 10 }, ytd: { pnl_cny: 20 } } }

describe('period P&L valuation requests', () => {
  beforeEach(() => vi.clearAllMocks())

  it('forwards manual prices and clears them when returning to server pricing', async () => {
    vi.mocked(api.getPeriodPnl).mockResolvedValue({ data: payload } as never)
    const periods = usePeriodPnl()
    const prices = { '600000:A股': 12 }
    expect(await periods.load(prices)).toBe(true)
    expect(api.getPeriodPnl).toHaveBeenLastCalledWith(prices)
    expect(await periods.load()).toBe(true)
    expect(api.getPeriodPnl).toHaveBeenLastCalledWith(null)
  })

  it('does not retain a prior valuation when the manual-price request fails', async () => {
    vi.mocked(api.getPeriodPnl)
      .mockResolvedValueOnce({ data: payload } as never)
      .mockRejectedValueOnce(new Error('试价失败'))
    const periods = usePeriodPnl()
    expect(await periods.load()).toBe(true)
    expect(await periods.load({ '600000:A股': 12 })).toBe(false)
    expect(periods.state.data).toBeNull()
    expect(periods.state.error).toBe('试价失败')
  })

  it('rejects incomplete responses without showing invented period values', async () => {
    vi.mocked(api.getPeriodPnl).mockResolvedValueOnce({ data: { periods: {} } } as never)
    const periods = usePeriodPnl()
    expect(await periods.load()).toBe(false)
    expect(periods.state.data).toBeNull()
    expect(periods.state.error).toBe('返回的期间损益不完整')
  })

  it('ignores late errors from an older valuation', async () => {
    let reject!: (reason: Error) => void
    const old = new Promise((_resolve, no) => (reject = no))
    vi.mocked(api.getPeriodPnl)
      .mockReturnValueOnce(old as never)
      .mockResolvedValueOnce({ data: payload } as never)
    const periods = usePeriodPnl()
    const first = periods.load()
    expect(await periods.load({ '600000:A股': 12 })).toBe(true)
    reject(new Error('obsolete'))
    expect(await first).toBe(false)
    expect(periods.state.data).toEqual(payload)
    expect(periods.state.error).toBe('')
  })
})
