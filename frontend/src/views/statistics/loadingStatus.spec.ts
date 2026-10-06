// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope } from 'vue'
vi.mock('@/api', () => ({
  default: {
    getPerformanceAnalytics: vi.fn(),
    getStatsByMarket: vi.fn(),
    getStatsByTime: vi.fn(),
    getHoldingsCostBreakdown: vi.fn(),
    getSummary: vi.fn()
  }
}))
vi.mock('@/styles/theme', () => ({ useTheme: () => ({ resolved: { value: 'light' } }) }))
vi.mock('@/utils/showApiError', () => ({ showApiError: vi.fn() }))
vi.mock('@/composables/useMediaQuery', () => ({ useMediaQuery: () => ({ value: true }) }))
import api from '@/api'
import { showApiError } from '@/utils/showApiError'
import { useAnalytics } from './useAnalytics'
import { useDistributionStats } from './useDistributionStats'
const emptyAnalytics = {
  calculation_level: 'empty',
  curve: [],
  metrics: {},
  trade_skill: { sample_count: 0 },
  data_quality: { warnings: [] }
}
describe('statistics successful data and failed requests', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
  })
  it('failed analytics stays unknown, while a successful empty response is known', async () => {
    const scope = effectScope(),
      analytics = scope.run(() => useAnalytics({ isUnmounted: () => false }))!
    vi.mocked(api.getPerformanceAnalytics)
      .mockRejectedValueOnce(new Error('unavailable'))
      .mockResolvedValueOnce({ data: emptyAnalytics } as never)
    expect(await analytics.load()).toBe(false)
    expect(analytics.state.loaded).toBe(false)
    expect(analytics.state.error).toBe('unavailable')
    expect(await analytics.load()).toBe(true)
    expect(analytics.state.loaded).toBe(true)
    expect(analytics.state.error).toBe('')
    expect(analytics.tradeSkill.sample_count).toBe(0)
    scope.stop()
  })
  it('an obsolete analytics error cannot replace a more recent successful curve', async () => {
    let reject!: (reason: Error) => void
    const previous = new Promise((_yes, no) => (reject = no))
    vi.mocked(api.getPerformanceAnalytics)
      .mockReturnValueOnce(previous as never)
      .mockResolvedValueOnce({
        data: {
          ...emptyAnalytics,
          date_range: { start_date: '2026-01-02', end_date: '2026-02-03' }
        }
      } as never)
    const scope = effectScope(),
      analytics = scope.run(() => useAnalytics({ isUnmounted: () => false }))!
    const old = analytics.load()
    expect(await analytics.load()).toBe(true)
    reject(new Error('old request failed'))
    expect(await old).toBe(false)
    expect(analytics.state.error).toBe('')
    expect(analytics.effectiveRangeLabel).toContain('2026/01/02 至 2026/02/03')
    expect(showApiError).not.toHaveBeenCalled()
    scope.stop()
  })
  it('curve options keep unknown points as gaps and link independently scaled drawdowns by date', async () => {
    const values = [
      0,
      '0',
      null,
      undefined,
      '',
      '   ',
      'invalid',
      NaN,
      Infinity,
      -Infinity,
      'Infinity',
      12.34
    ]
    const dates = values.map((_, index) => `2026-01-${String(index + 1).padStart(2, '0')}`)
    vi.mocked(api.getPerformanceAnalytics).mockResolvedValueOnce({
      data: {
        ...emptyAnalytics,
        calculation_level: 'full',
        curve: values.map((value, index) => ({
          date: dates[index],
          cumulative_return_rate: value,
          drawdown_rate: index === values.length - 1 ? -12.34 : value
        })),
        benchmarks: [
          {
            code: 'TEST',
            name: '测试基准',
            status: 'ok',
            points: values.map((value, index) => ({
              date: dates[index],
              cumulative_return_rate: value
            }))
          },
          {
            code: 'LATER',
            name: '晚起点基准',
            status: 'ok',
            alignment: 'first_available',
            points: [{ date: dates.at(-1), cumulative_return_rate: 0 }]
          }
        ]
      }
    } as never)
    const scope = effectScope(),
      analytics = scope.run(() => useAnalytics({ isUnmounted: () => false }))!
    try {
      await analytics.load()
      const option = analytics.chartOption
      const gaps = [0, 0, null, null, null, null, null, null, null, null, null]
      expect(option.series[0].data).toEqual([...gaps, 12.34])
      expect(option.series[1].data).toEqual([...gaps, -12.34])
      expect(option.series[2].data).toEqual([...gaps, 12.34])
      expect(option.series[3].data).toEqual([...Array(11).fill(null), 0])
      expect(option.series.every((series) => series.connectNulls === false)).toBe(true)
      expect(option.tooltip.valueFormatter(null)).toBe('—')
      expect(option.tooltip.valueFormatter('Infinity')).toBe('—')
      expect(option.tooltip.valueFormatter('')).toBe('—')
      expect(option.tooltip.valueFormatter(0)).toBe('0.00%')
      expect(option.grid).toHaveLength(2)
      expect(option.yAxis.map((axis) => axis.name)).toEqual(['收益率（%）', '回撤（%）'])
      expect(option.series.map((series) => [series.xAxisIndex, series.yAxisIndex])).toEqual([
        [0, 0],
        [1, 1],
        [0, 0],
        [0, 0]
      ])
      expect(option.xAxis.map((axis) => axis.data)).toEqual([dates, dates])
      expect(option.dataZoom.map((zoom) => zoom.xAxisIndex)).toEqual([
        [0, 1],
        [0, 1]
      ])
      expect(option.axisPointer.link).toEqual([{ xAxisIndex: 'all' }])
      // 回撤已由独立子图的轴名标识，主图图例只包含组合收益与基准。
      expect(option.legend.data).toEqual(['累计 TTWR 收益率', '测试基准', '晚起点基准'])
    } finally {
      scope.stop()
    }
  })
  it('failed time regrouping retains the successful result and its original month identity', async () => {
    vi.mocked(api.getStatsByTime)
      .mockResolvedValueOnce({
        data: [{ period: '2026-01', buy_amount: 100, sell_amount: 0 }]
      } as never)
      .mockRejectedValueOnce(new Error('year unavailable'))
    const scope = effectScope(),
      dist = scope.run(useDistributionStats)!
    await dist.loadTimeStats()
    dist.state.timeGroupBy = 'year'
    await dist.loadTimeStats()
    expect(dist.state.timeStats[0].period).toBe('2026-01')
    expect(dist.state.timeResultGroupBy).toBe('month')
    expect(dist.state.timeStatus.loaded).toBe(true)
    expect(dist.state.timeStatus.error).toBe('year unavailable')
    scope.stop()
  })
  it('a slower month failure does not overwrite a successful year regrouping', async () => {
    let reject!: (reason: Error) => void
    const previous = new Promise((_yes, no) => (reject = no))
    vi.mocked(api.getStatsByTime)
      .mockReturnValueOnce(previous as never)
      .mockResolvedValueOnce({
        data: [{ period: '2026', buy_amount: 200, sell_amount: 0 }]
      } as never)
    const scope = effectScope(),
      dist = scope.run(useDistributionStats)!
    const old = dist.loadTimeStats()
    dist.state.timeGroupBy = 'year'
    await dist.loadTimeStats()
    reject(new Error('obsolete month'))
    await old
    expect(dist.state.timeStats[0].period).toBe('2026')
    expect(dist.state.timeResultGroupBy).toBe('year')
    expect(dist.state.timeStatus.error).toBe('')
    expect(showApiError).not.toHaveBeenCalled()
    scope.stop()
  })
})
