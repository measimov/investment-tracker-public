import { describe, expect, it } from 'vitest'
import {
  collectPriceIssues,
  describeIssueItem,
  isPriceIssueWarning,
  missingSummary,
  staleSummary
} from './priceIssues'

const freshness = {
  '600000:A股': { source: 'holding', stale: false, price_date: '2026-09-29' },
  '000333:A股': {
    source: 'latest_history',
    stale: true,
    price_date: '2026-09-10',
    name: '美的集团'
  },
  '00700:港股': { source: 'holding', stale: true, price_date: '2026-08-15', name: '腾讯控股' },
  'NOPX:新加坡股': { source: 'missing', stale: true }
}

describe('priceIssues（#286）', () => {
  it('陈价按价格日期由旧到新，缺价单列', () => {
    const issues = collectPriceIssues(freshness)
    expect(issues.stale.map((item) => item.key)).toEqual(['00700:港股', '000333:A股'])
    expect(issues.missing.map((item) => item.key)).toEqual(['NOPX:新加坡股'])
  })

  it('摘要一行：数量 + 最早日期', () => {
    const issues = collectPriceIssues(freshness)
    expect(staleSummary(issues)).toBe('2 只持仓价格超过 7 天未更新，最早 2026-08-15')
    expect(missingSummary(issues)).toBe('1 只持仓缺少可用估值价格，未计入市值')
    expect(staleSummary(collectPriceIssues({}))).toBeNull()
  })

  it('清单行带名称，没有名称退回代码', () => {
    const [first] = collectPriceIssues(freshness).stale
    expect(describeIssueItem(first)).toBe('腾讯控股（00700 · 港股） 2026-08-15')
    expect(describeIssueItem(collectPriceIssues(freshness).missing[0])).toBe('NOPX · 新加坡股')
  })

  it('识别后端清单式原文', () => {
    expect(isPriceIssueWarning('以下标的估值价格超过 7 天未更新：000333:A股')).toBe(true)
    expect(isPriceIssueWarning('缺少 HKD 对 CNY 的汇率')).toBe(false)
  })
})
