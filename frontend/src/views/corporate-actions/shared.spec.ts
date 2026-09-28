import { describe, expect, it } from 'vitest'

import { cashDividendAmounts, hkDividendNotes, suggestionSourceLabel, taxFromRate } from './shared'

describe('cashDividendAmounts', () => {
  it('没有显式 net 时按 gross − tax 派生', () => {
    expect(cashDividendAmounts({ total_dividend: '1000', tax_withheld: '100' })).toEqual({
      gross: 1000,
      tax: 100,
      net: 900
    })
  })

  it('显式 net（含 0）优先', () => {
    expect(
      cashDividendAmounts({ total_dividend: '1000', tax_withheld: '0', net_dividend: '0' }).net
    ).toBe(0)
    expect(
      cashDividendAmounts({ total_dividend: '1000', tax_withheld: '0', net_dividend: '950' }).net
    ).toBe(950)
  })

  it('缺失的总额/税额按 0', () => {
    expect(cashDividendAmounts({})).toEqual({ gross: 0, tax: 0, net: 0 })
  })
})

describe('taxFromRate', () => {
  it('总额 × 税率%，保留 2 位', () => {
    expect(taxFromRate(1000, 10)).toBe(100)
    expect(taxFromRate(1234.56, 20)).toBe(246.91)
    expect(taxFromRate(37051.2, 10)).toBe(3705.12)
  })

  it('任一缺失返回 null', () => {
    expect(taxFromRate(null, 10)).toBeNull()
    expect(taxFromRate(1000, null)).toBeNull()
  })
})

describe('suggestionSourceLabel', () => {
  it('来源映射为短标签', () => {
    expect(suggestionSourceLabel('hkexnews-dividend')).toBe('披露易')
    expect(suggestionSourceLabel('tushare-dividend')).toBe('Tushare')
    expect(suggestionSourceLabel('other')).toBe('other')
  })
})

describe('hkDividendNotes', () => {
  it('非披露易建议没有说明行', () => {
    expect(hkDividendNotes(null)).toEqual([])
    expect(hkDividendNotes({ source: 'tushare' })).toEqual([])
  })

  it('宣派人民币派发港元 + H 股代扣税', () => {
    const lines = hkDividendNotes({
      source: 'hkexnews',
      withholding_applicable: true,
      components: [
        {
          dividend_type: '末期',
          dividend_nature: '普通股息',
          amount: '0.10391',
          currency: 'HKD',
          declared_amount: '0.0908',
          declared_currency: 'CNY',
          exchange_rate: { from: 'CNY', to: 'HKD', rate: '1.144387' },
          status: '更新公告',
          withholding: {
            applicable: true,
            rates_percent: ['10', '20'],
            non_resident_enterprise_percent: '10',
            southbound_individual_percent: '20'
          }
        }
      ]
    })
    expect(lines).toEqual([
      '末期·普通股息 每股 0.10391 HKD（宣派 0.0908 CNY，1 CNY = 1.144387 HKD） · 更新公告',
      '公告代扣所得税：非居民企业（含 HKSCC 代理人）10%、港股通个人 20%（按持有渠道不同，以实际到账为准）'
    ])
  })

  it('同日多笔、不代扣、以股代息与币种选择', () => {
    const lines = hkDividendNotes({
      source: 'hkexnews',
      withholding_applicable: false,
      scrip_option: true,
      currency_election: true,
      components: [
        { dividend_type: '末期', dividend_nature: '普通股息', amount: '0.8', currency: 'HKD' },
        { dividend_type: '末期', dividend_nature: '特別股息', amount: '0.4', currency: 'HKD' }
      ]
    })
    expect(lines).toEqual([
      '末期·普通股息 每股 0.8 HKD',
      '末期·特別股息 每股 0.4 HKD',
      '公告：发行人不代扣所得税（港股通等渠道仍可能由结算机构代扣，以到账为准）',
      '可选以股代息：若选择以股代息，实际不收现金',
      '可选择派发币种：实际到账币种可能与此不同'
    ])
  })
})
