import { describe, expect, it } from 'vitest'
import {
  grahamBasisText,
  grahamPriceText,
  grahamSupplementText,
  grahamSupplementTitle
} from './grahamFormat'

describe('grahamFormat', () => {
  it('TTM 构成、每股盈利与价格日期', () => {
    const text = grahamBasisText({
      label: 'TTM = 20251231 年报 + 20260630 中报 − 上年同期中报',
      price: 436.6,
      price_currency: 'HKD',
      price_date: '2026-09-25',
      eps_ttm: 30.4012,
      components: [
        {
          period: '20251231|FY',
          sign: '+',
          eps: 26.53,
          currency: 'CNY',
          eps_in_price_currency: 28.9
        },
        {
          period: '20260630|H1',
          sign: '+',
          eps: 14.2,
          currency: 'CNY',
          eps_in_price_currency: 15.5
        },
        {
          period: '20250630|H1',
          sign: '-',
          eps: 12.8,
          currency: 'CNY',
          eps_in_price_currency: 13.9
        }
      ]
    })
    expect(text).toContain(
      '构成 +2025-12-31 年报 26.53 CNY +2026-06-30 H1 14.20 CNY −2025-06-30 H1 12.80 CNY'
    )
    expect(text).toContain('每股盈利 30.4012 HKD')
    expect(text).toContain('价格 436.60 HKD（2026-09-25）')
  })

  it('陈价与 ADS 口径如实标注；单一年报不列构成', () => {
    const text = grahamBasisText({
      label: '20251231 年报（20-F 发行人不披露季报）',
      price: 77.57,
      price_currency: 'USD',
      price_date: '2026-09-10',
      price_stale: true,
      price_age_days: 17,
      share_ratio_note: '1 ADS = 4 股（20-F 封面 2026-04-29）',
      share_ratio_source: '20-F',
      components: [
        { period: '20251231|FY', sign: '+', eps: 2.5, currency: 'USD', eps_in_price_currency: 2.5 }
      ],
      eps_ttm: 10
    })
    expect(text).not.toContain('构成')
    expect(text).toContain('陈价 17 天')
    expect(text).toContain('1 ADS = 4 股（20-F 封面 2026-04-29）')
  })

  it('A股 快照依据与缺价', () => {
    expect(grahamPriceText(undefined)).toBe('')
    expect(grahamBasisText({ label: 'Tushare daily_basic 快照（pe_ttm / pb）', price: null })).toBe(
      'Tushare daily_basic 快照（pe_ttm / pb）'
    )
  })

  it('补充口径：缺值显示 —，tooltip 带来源', () => {
    const supplement = {
      static_pe: 14.67,
      graham_avg3_pe: null,
      static_basis: 'FY2025 每股盈利 3.971',
      avg3_basis: '近三个财年每股盈利不全',
      basis: '年报口径，仅供参考，不参与判定'
    }
    expect(grahamSupplementText(supplement)).toBe('年报口径 PE 14.67 · 三年均值 PE —（仅供参考）')
    expect(grahamSupplementTitle(supplement)).toContain('近三个财年每股盈利不全')
    expect(grahamSupplementText(undefined)).toBe('')
  })
})
