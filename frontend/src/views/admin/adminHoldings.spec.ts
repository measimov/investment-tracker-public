import { describe, expect, it } from 'vitest'
import type { AdminHolding } from '../../types'
import {
  rowMarketValue,
  rowProfit,
  rowProfitPercent,
  summarizeAdminHoldings,
  type ToCNY
} from './adminHoldings'

function holding(overrides: Partial<AdminHolding>): AdminHolding {
  return {
    id: 1,
    user_id: 1,
    symbol: '600000',
    market: 'A股',
    currency: 'CNY',
    quantity: '100',
    avg_cost: '10',
    total_cost: '1000',
    current_price: '12',
    unknown_cost_quantity: '0',
    updated_at: '2026-09-01T00:00:00Z',
    ...overrides
  }
}

const RATES: Record<string, number> = { CNY: 1, USD: 7, HKD: 0.9 }
const toCNY: ToCNY = (amount, currency) => {
  const rate = RATES[currency ?? 'CNY']
  return rate === undefined ? null : amount * rate
}

describe('admin holding rows', () => {
  it('computes value/profit/percent in original currency', () => {
    const row = holding({})
    expect(rowMarketValue(row)).toBe(1200)
    expect(rowProfit(row)).toBe(200)
    expect(rowProfitPercent(row)).toBe(20)
  })

  it('returns null instead of a total loss when the price is missing', () => {
    for (const current_price of [null, undefined]) {
      const row = holding({ current_price })
      expect(rowMarketValue(row)).toBeNull()
      expect(rowProfit(row)).toBeNull()
      expect(rowProfitPercent(row)).toBeNull()
    }
  })

  it('keeps a real zero price as zero (not missing)', () => {
    const row = holding({ current_price: '0' })
    expect(rowMarketValue(row)).toBe(0)
    expect(rowProfitPercent(row)).toBe(-100)
  })

  it('has no percent when cost is zero', () => {
    expect(rowProfitPercent(holding({ total_cost: '0' }))).toBeNull()
  })
})

describe('summarizeAdminHoldings', () => {
  it('converts every currency to CNY instead of adding raw amounts', () => {
    const summary = summarizeAdminHoldings(
      [
        holding({}),
        holding({ currency: 'USD', total_cost: '100', quantity: '10', current_price: '11' })
      ],
      toCNY
    )
    expect(summary.totalCostCNY).toBe(1000 + 700)
    expect(summary.totalValueCNY).toBe(1200 + 770)
    expect(summary.profitCNY).toBe(200 + 70)
    expect(summary.unpricedCount).toBe(0)
    expect(summary.missingRateCount).toBe(0)
  })

  it('counts unpriced rows in cost only, never as a loss', () => {
    const summary = summarizeAdminHoldings(
      [holding({}), holding({ current_price: null, total_cost: '500' })],
      toCNY
    )
    expect(summary.totalCostCNY).toBe(1500)
    expect(summary.totalValueCNY).toBe(1200)
    expect(summary.profitCNY).toBe(200)
    expect(summary.unpricedCount).toBe(1)
  })

  it('excludes rows without an exchange rate and reports the currencies', () => {
    const summary = summarizeAdminHoldings(
      [
        holding({}),
        holding({ currency: 'THB', total_cost: '9999' }),
        holding({ currency: 'THB', total_cost: '1' }),
        holding({ currency: 'JPY', total_cost: '1' })
      ],
      toCNY
    )
    expect(summary.totalCostCNY).toBe(1000)
    expect(summary.totalValueCNY).toBe(1200)
    expect(summary.missingRateCount).toBe(3)
    expect(summary.missingRateCurrencies).toEqual(['THB', 'JPY'])
  })
})
