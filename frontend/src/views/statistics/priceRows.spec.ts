import { describe, expect, it } from 'vitest'
import {
  collectPrices,
  fillMissingPrices,
  mergePriceRows,
  priceKey,
  serverPriceMap,
  type PriceHoldingLike
} from './priceRows'

function holding(overrides: Partial<PriceHoldingLike>): PriceHoldingLike {
  return {
    symbol: '600000',
    market: 'A股',
    name: '浦发银行',
    currency: 'CNY',
    quantity: '100',
    total_cost: '1000',
    avg_cost: '10',
    current_price: null,
    price_updated_at: null,
    ...overrides
  }
}

describe('mergePriceRows', () => {
  it('同一标的多账户行合并为一行，数量/成本相加、均价重算', () => {
    const rows = mergePriceRows([
      holding({ quantity: '100', total_cost: '1000', avg_cost: '10' }),
      holding({ quantity: '300', total_cost: '3600', avg_cost: '12' })
    ])
    expect(rows).toHaveLength(1)
    expect(rows[0].key).toBe('600000:A股')
    expect(rows[0].quantity).toBe(400)
    expect(rows[0].avg_cost).toBe(11.5)
    expect(rows[0].account_count).toBe(2)
  })

  it('单账户行保留原均价（不重算）', () => {
    const rows = mergePriceRows([
      holding({ quantity: '3', total_cost: '10', avg_cost: '3.333333' })
    ])
    expect(rows[0].avg_cost).toBe(3.333333)
  })

  it('同码不同市场是两行，键带市场', () => {
    const rows = mergePriceRows([
      holding({ symbol: '0700', market: '港股' }),
      holding({ symbol: '0700', market: '港股通' })
    ])
    expect(rows.map((row) => row.key)).toEqual(['0700:港股', '0700:港股通'])
  })

  it('现价取更新时间最晚的一行（与后端 resolve_server_prices 同口径），无价行不参与', () => {
    const rows = mergePriceRows([
      holding({ current_price: '12', price_updated_at: '2026-09-25T08:00:00Z' }),
      holding({ current_price: '9', price_updated_at: '2026-08-01T08:00:00Z' }),
      holding({ current_price: null, price_updated_at: '2026-09-26T08:00:00Z' })
    ])
    expect(rows[0].current_price).toBe(12)

    const reversed = mergePriceRows([
      holding({ current_price: '9', price_updated_at: '2026-08-01T08:00:00Z' }),
      holding({ current_price: '12', price_updated_at: '2026-09-25T08:00:00Z' })
    ])
    expect(reversed[0].current_price).toBe(12)
  })

  it('全部无价时为 null', () => {
    const rows = mergePriceRows([holding({ current_price: '0' }), holding({})])
    expect(rows[0].current_price).toBeNull()
  })
})

describe('collectPrices', () => {
  it('键为 symbol:market，只收正价', () => {
    const rows = mergePriceRows([
      holding({ current_price: '11' }),
      holding({ symbol: 'AAPL', market: '美股', current_price: null })
    ])
    expect(collectPrices(rows)).toEqual({ '600000:A股': 11 })
  })
})

describe('fillMissingPrices / serverPriceMap', () => {
  it('空价行用服务端估值价补齐，已有价格不覆盖', () => {
    const rows = mergePriceRows([
      holding({ current_price: '11' }),
      holding({ symbol: 'PCT', market: '新加坡股', current_price: null })
    ])
    const server = serverPriceMap([
      { symbol: '600000', market: 'A股', current_price: 10.5 },
      { symbol: 'PCT', market: '新加坡股', current_price: 6 },
      { symbol: 'BAD', market: 'A股', current_price: null }
    ])
    expect(server).toEqual({ '600000:A股': 10.5, 'PCT:新加坡股': 6 })
    const filled = fillMissingPrices(rows, server)
    expect(filled.map((row) => row.current_price)).toEqual([11, 6])
    expect(priceKey('PCT', '新加坡股')).toBe('PCT:新加坡股')
  })
})
