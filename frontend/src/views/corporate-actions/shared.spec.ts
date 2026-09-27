import { describe, expect, it } from 'vitest'

import { cashDividendAmounts, taxFromRate } from './shared'

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
