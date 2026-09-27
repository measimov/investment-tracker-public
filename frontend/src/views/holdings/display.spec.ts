import { describe, expect, it } from 'vitest'
import {
  buildMarketSubtotals,
  describePrice,
  displayAnalysisTag,
  priceSourceLabel,
  sortNullsLast
} from './display'

// 本地时刻构造 ISO 串：断言与运行机器的时区无关
const localIso = (y: number, m: number, d: number, h = 10) => new Date(y, m - 1, d, h).toISOString()

describe('describePrice', () => {
  it('shows the quote trade date rather than the refresh time (#217)', () => {
    // 周六刷新，行情是周五收盘
    const info = describePrice(
      {
        price: 97.45,
        priceAsOf: '2026-09-25',
        priceUpdatedAt: localIso(2026, 9, 26),
        priceSource: 'tencent-quote'
      },
      '2026-09-26'
    )
    expect(info?.label).toBe('行情 2026/09/25')
    expect(info?.stale).toBe(false)
    expect(info?.manual).toBe(false)
    expect(info?.ageDays).toBe(1)
    expect(info?.tooltip[1]).toBe('来源 腾讯行情')
  })

  it('falls back to the refresh time when the source gave no date', () => {
    const info = describePrice(
      {
        price: 1,
        priceAsOf: null,
        priceUpdatedAt: localIso(2026, 9, 26),
        priceSource: 'tushare-rt_k'
      },
      '2026-09-26'
    )
    expect(info?.label).toBe('刷新于 2026/09/26')
    expect(info?.tooltip[0]).toBe('行情日期未知')
  })

  it('marks manual prices and goes stale after 7 days', () => {
    const fresh = describePrice(
      { price: 2, priceAsOf: null, priceUpdatedAt: localIso(2026, 9, 19), priceSource: 'manual' },
      '2026-09-26'
    )
    expect(fresh?.manual).toBe(true)
    expect(fresh?.label).toBe('录入于 2026/09/19')
    expect(fresh?.stale).toBe(false) // 恰好 7 天不算陈价（与后端 > 7 同口径）

    const stale = describePrice(
      { price: 2, priceAsOf: null, priceUpdatedAt: localIso(2026, 9, 18), priceSource: 'manual' },
      '2026-09-26'
    )
    expect(stale?.stale).toBe(true)
    expect(stale?.tooltip.at(-1)).toContain('陈价')
  })

  it('an old trade date is stale even if refreshed today (suspended stock)', () => {
    const info = describePrice(
      {
        price: 5,
        priceAsOf: '2026-07-01',
        priceUpdatedAt: localIso(2026, 9, 26),
        priceSource: 'tushare-daily'
      },
      '2026-09-26'
    )
    expect(info?.stale).toBe(true)
  })

  it('returns null without a price and stale without any date', () => {
    expect(
      describePrice(
        { price: null, priceAsOf: null, priceUpdatedAt: null, priceSource: null },
        '2026-09-26'
      )
    ).toBeNull()
    const undated = describePrice(
      { price: 3, priceAsOf: null, priceUpdatedAt: null, priceSource: null },
      '2026-09-26'
    )
    expect(undated?.stale).toBe(true)
    expect(undated?.label).toBe('')
  })
})

describe('priceSourceLabel', () => {
  it('maps known sources', () => {
    expect(priceSourceLabel('manual')).toBe('手工录入')
    expect(priceSourceLabel('tushare-hk_daily')).toBe('Tushare hk_daily')
    expect(priceSourceLabel(null)).toBe('未知')
    expect(priceSourceLabel('other')).toBe('other')
  })
})

describe('sortNullsLast', () => {
  const rows = [{ v: 1 }, { v: null }, { v: 3 }, { v: -2 }]
  it('keeps missing values at the bottom in both directions', () => {
    expect(sortNullsLast(rows, (r) => r.v, 'descending').map((r) => r.v)).toEqual([3, 1, -2, null])
    expect(sortNullsLast(rows, (r) => r.v, 'ascending').map((r) => r.v)).toEqual([-2, 1, 3, null])
  })
})

describe('buildMarketSubtotals', () => {
  it('counts securities once, excludes unpriced rows from value and profit', () => {
    const subtotals = buildMarketSubtotals([
      // 同一标的两个账户行只计一只
      { market: '港股', key: '00700:港股', priced: true, valueCNY: 300, costCNY: 400 },
      { market: '港股', key: '00700:港股', priced: true, valueCNY: 100, costCNY: 100 },
      { market: '港股', key: '09999:港股', priced: false, valueCNY: 0, costCNY: 999 },
      { market: 'A股', key: '600000:A股', priced: true, valueCNY: 600, costCNY: 500 }
    ])
    expect(subtotals).toEqual([
      { market: 'A股', count: 1, pricedCount: 1, valueCNY: 600, profitCNY: 100, share: 60 },
      { market: '港股', count: 2, pricedCount: 1, valueCNY: 400, profitCNY: -100, share: 40 }
    ])
  })

  it('share is null when nothing is priced', () => {
    const [only] = buildMarketSubtotals([
      { market: '美股', key: 'X:美股', priced: false, valueCNY: 0, costCNY: 1 }
    ])
    expect(only.share).toBeNull()
    expect(only.profitCNY).toBe(0)
  })
})

describe('displayAnalysisTag', () => {
  it('skips noise tags', () => {
    expect(displayAnalysisTag(['数据不足', '估值偏低'])).toBe('估值偏低')
    expect(displayAnalysisTag(['数据不足'])).toBeNull()
    expect(displayAnalysisTag(undefined)).toBeNull()
  })
})
