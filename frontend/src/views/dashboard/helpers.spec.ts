import { describe, expect, it } from 'vitest'
import { cardTone, mergeDashboardWarnings, periodIsEstimated } from './helpers'

describe('cardTone', () => {
  it('缺值为中性，不再被当成绿', () => {
    expect(cardTone(null)).toBe('summary-card-neutral')
    expect(cardTone(undefined)).toBe('summary-card-neutral')
    expect(cardTone(0)).toBe('summary-card-neutral')
    expect(cardTone(-1)).toBe('summary-card-danger')
    expect(cardTone('12.5')).toBe('summary-card-success')
  })
})

describe('periodIsEstimated', () => {
  it('期初基准陈旧或有估值补记的实物转入都打估算', () => {
    expect(periodIsEstimated({ status: 'exact' })).toBe(false)
    expect(periodIsEstimated({ status: 'estimated' })).toBe(true)
    expect(periodIsEstimated({ status: 'exact', estimated_inflow_events: 2 })).toBe(true)
    expect(periodIsEstimated({ status: 'unavailable', estimated_inflow_events: 2 })).toBe(false)
  })
})

describe('mergeDashboardWarnings', () => {
  const snapshotMissing = '以下标的缺少可用估值价格：NOPX:A股'
  const periodMissing = '1 只持仓无可用价格，未计入市值：NOPX'

  it('同一批缺价标的只留快照那一条', () => {
    const warnings = mergeDashboardWarnings({
      snapshotWarnings: [snapshotMissing],
      snapshotMissingKeys: ['NOPX:A股'],
      periodWarnings: [periodMissing, '本月损益为估算：…'],
      periodUnpriced: [{ symbol: 'NOPX', market: 'A股' }]
    })
    expect(warnings).toEqual([snapshotMissing, '本月损益为估算：…'])
  })

  it('区间损益里有快照未覆盖的缺价标的时保留它那条', () => {
    const warnings = mergeDashboardWarnings({
      snapshotWarnings: [snapshotMissing],
      snapshotMissingKeys: ['NOPX:A股'],
      periodWarnings: ['2 只持仓无可用价格，未计入市值：NOPX、OTHER'],
      periodUnpriced: [
        { symbol: 'NOPX', market: 'A股' },
        { symbol: 'OTHER', market: '港股' }
      ]
    })
    expect(warnings).toHaveLength(2)
  })

  it('逐字相同的警告去重；区间损益缺失时只用快照', () => {
    expect(mergeDashboardWarnings({ snapshotWarnings: ['a', 'a'], periodWarnings: ['a'] })).toEqual(
      ['a']
    )
    expect(mergeDashboardWarnings({ snapshotWarnings: ['x'] })).toEqual(['x'])
  })
})
