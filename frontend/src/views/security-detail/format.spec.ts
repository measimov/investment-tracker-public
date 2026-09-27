import { describe, expect, it } from 'vitest'
import {
  annualOnly,
  daysAgo,
  daysAgoText,
  digestSourceHref,
  digestTypeLabel,
  formatCount,
  formatEps,
  formatPct,
  formatPerShare,
  formatPeriod,
  formatPp,
  formatRatio,
  formatWanShares,
  formatYi,
  isAnalysisOutdated,
  latestImplementedDividendAnnDate,
  periodKind,
  periodKindLabel
} from './format'

describe('按字段定精度', () => {
  it('股数 0 位、百分比 2 位、pp 1 位、比率 2 位、EPS 2–3 位、每股分红 2–4 位', () => {
    expect(formatCount(1234567)).toBe('1,234,567')
    expect(formatPct(35.1234)).toBe('35.12')
    expect(formatPp(21.26)).toBe('21.3')
    expect(formatRatio(1.34789)).toBe('1.35')
    expect(formatEps(2.5)).toBe('2.50')
    expect(formatEps(24.7491)).toBe('24.749')
    expect(formatPerShare(0.0512)).toBe('0.0512')
    expect(formatPerShare(1.2)).toBe('1.20')
  })

  it('缺值与非数值显示占位符', () => {
    expect(formatRatio(null)).toBe('—')
    expect(formatPct('')).toBe('—')
    expect(formatEps('abc')).toBe('—')
    expect(formatYi(undefined)).toBe('—')
  })

  it('亿与万股换算', () => {
    expect(formatYi(123456789012)).toBe('1,234.57')
    // Tushare share_float.float_share 单位是「股」：25,076,106 股 = 2,507.61 万股
    expect(formatWanShares(25076106)).toBe('2,507.61')
  })
})

describe('报告期与期别', () => {
  it('由期末日推断期别', () => {
    expect(periodKind('20250331')).toBe('Q1')
    expect(periodKind('20250630')).toBe('H1')
    expect(periodKind('20250930')).toBe('Q3')
    expect(periodKind('20251231')).toBe('FY')
    expect(periodKind('20250815')).toBeNull()
    expect(periodKindLabel('20250930')).toBe('三季报')
    expect(periodKindLabel(null)).toBe('—')
    expect(formatPeriod('20251231')).toBe('2025-12-31')
    expect(formatPeriod('')).toBe('—')
  })

  it('只看年报', () => {
    const rows = [{ end_date: '20251231' }, { end_date: '20250930' }, { end_date: '20241231' }]
    expect(annualOnly(rows).map((r) => r.end_date)).toEqual(['20251231', '20241231'])
  })
})

describe('财报摘要原文链接', () => {
  it('字符串直链原样；EDGAR dict 拼 Archives 地址', () => {
    expect(digestSourceHref('http://static.cninfo.com.cn/a.PDF')).toBe(
      'http://static.cninfo.com.cn/a.PDF'
    )
    expect(
      digestSourceHref({
        cik: 1737806,
        accession: '0001104659-25-036123',
        document: 'pdd-20241231x20f.htm'
      })
    ).toBe(
      'https://www.sec.gov/Archives/edgar/data/1737806/000110465925036123/pdd-20241231x20f.htm'
    )
    expect(digestSourceHref({ cik: '0000320193', accession: '1-2', document: 'a.htm' })).toBe(
      'https://www.sec.gov/Archives/edgar/data/320193/12/a.htm'
    )
  })

  it('形状不对不给链接', () => {
    expect(digestSourceHref(null)).toBeNull()
    expect(digestSourceHref('javascript:alert(1)')).toBeNull()
    expect(digestSourceHref({ cik: 1 })).toBeNull()
    expect(digestSourceHref({ cik: 'x', accession: '1', document: 'a' })).toBeNull()
    expect(digestTypeLabel('20-F')).toBe('20-F')
    expect(digestTypeLabel('semi')).toBe('中报')
  })
})

describe('新鲜度', () => {
  const now = new Date(2026, 8, 26, 10, 0)

  it('N 天前按本地自然日', () => {
    expect(daysAgo(new Date(2026, 8, 26, 1, 0).toISOString(), now)).toBe(0)
    expect(daysAgoText(new Date(2026, 8, 25, 23, 0).toISOString(), now)).toBe('昨天')
    expect(daysAgoText(new Date(2026, 8, 16, 12, 0).toISOString(), now)).toBe('10 天前')
    expect(daysAgoText(null, now)).toBe('')
  })

  it('分析早于最新摘要/报表才算可能过期', () => {
    expect(isAnalysisOutdated('2026-09-01T00:00:00Z', '2026-09-02T00:00:00Z')).toBe(true)
    expect(isAnalysisOutdated('2026-09-02T00:00:00Z', '2026-09-01T23:00:00Z')).toBe(false)
    // 时区不同也按时刻比较
    expect(isAnalysisOutdated('2026-09-02T07:00:00+08:00', '2026-09-01T23:30:00Z')).toBe(true)
    expect(isAnalysisOutdated(null, '2026-09-02T00:00:00Z')).toBe(false)
    expect(isAnalysisOutdated('2026-09-02T00:00:00Z', null)).toBe(false)
  })
})

describe('分红最新公告', () => {
  it('只看已实施行的 ann_date，忽略更新的预案', () => {
    const rows = [
      { end_date: '20251231', div_proc: '预案', ann_date: '20260328' },
      { end_date: '20250630', div_proc: '实施', ann_date: '20250901' },
      { end_date: '20241231', div_proc: '实施', ann_date: '20250620' }
    ]
    expect(latestImplementedDividendAnnDate(rows)).toBe('20250901')
    expect(latestImplementedDividendAnnDate([])).toBeNull()
  })
})
