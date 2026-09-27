/**
 * 价格弹窗（what-if）的纯函数层（#218）。
 *
 * - 持仓是账户级的，同一标的每账户一行；弹窗按 `symbol:market` 合并成一行，
 *   否则多账户行各填一个价、后填的覆盖先填的。
 * - 价格映射的键必须是 `symbol:market`（与后端 `resolve_server_prices` /
 *   `get_current_price` 同一约定）：裸代码会让同码跨市场互相覆盖。
 */

import { toNumber } from '@/utils/helpers'
import type { PriceInputRow } from './types'

export function priceKey(symbol: string, market: string): string {
  return `${symbol}:${market}`
}

export interface PriceHoldingLike {
  symbol: string
  market: string
  name?: string | null
  currency?: string | null
  quantity: number | string
  total_cost: number | string
  avg_cost: number | string
  current_price?: number | string | null
  price_updated_at?: string | null
}

function updatedAtValue(value: string | null | undefined): number {
  if (!value) return Number.NEGATIVE_INFINITY
  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? Number.NEGATIVE_INFINITY : parsed
}

/**
 * 账户级持仓 → 每标的一行。数量/成本相加、均价按合并后重算；
 * 现价与后端同口径：取更新时间最晚的那一行的正价（无价的行不参与）。
 */
export function mergePriceRows(holdings: PriceHoldingLike[]): PriceInputRow[] {
  const merged = new Map<
    string,
    Omit<PriceInputRow, 'account_count'> & {
      total_cost: number
      rowCount: number
      priceAsOf: number
    }
  >()
  for (const holding of holdings) {
    const key = priceKey(holding.symbol, holding.market)
    const quantity = toNumber(holding.quantity)
    const totalCost = toNumber(holding.total_cost)
    const price = toNumber(holding.current_price)
    const asOf = updatedAtValue(holding.price_updated_at)
    const existing = merged.get(key)
    if (!existing) {
      merged.set(key, {
        key,
        symbol: holding.symbol,
        name: holding.name ?? null,
        market: holding.market,
        currency: holding.currency || 'CNY',
        quantity,
        total_cost: totalCost,
        avg_cost: toNumber(holding.avg_cost),
        current_price: price > 0 ? price : null,
        rowCount: 1,
        priceAsOf: price > 0 ? asOf : Number.NEGATIVE_INFINITY
      })
      continue
    }
    existing.quantity += quantity
    existing.total_cost += totalCost
    existing.rowCount += 1
    existing.avg_cost = existing.quantity > 0 ? existing.total_cost / existing.quantity : 0
    if (!existing.name && holding.name) existing.name = holding.name
    if (price > 0 && (existing.current_price === null || asOf > existing.priceAsOf)) {
      existing.current_price = price
      existing.priceAsOf = asOf
    }
  }
  return Array.from(merged.values()).map((row) => ({
    key: row.key,
    symbol: row.symbol,
    name: row.name,
    market: row.market,
    currency: row.currency,
    quantity: row.quantity,
    avg_cost: row.avg_cost,
    current_price: row.current_price,
    account_count: row.rowCount
  }))
}

/**
 * 用服务端估值价（含历史收盘兜底）补齐弹窗里空着的价格——否则只靠
 * Holding.current_price 的持仓在试算时变成「无价」被剔除，与 GET 的结果不一致。
 * 已有价格（含用户手填）不覆盖。
 */
export function fillMissingPrices(
  rows: PriceInputRow[],
  serverPrices: Record<string, number>
): PriceInputRow[] {
  return rows.map((row) => {
    if (row.current_price && row.current_price > 0) return row
    const fallback = serverPrices[row.key]
    return fallback && fallback > 0 ? { ...row, current_price: fallback } : row
  })
}

/** 弹窗行 → POST 请求体的价格映射（键 `symbol:market`，只收正价）。 */
export function collectPrices(rows: PriceInputRow[]): Record<string, number> {
  const prices: Record<string, number> = {}
  for (const row of rows) {
    if (row.current_price && row.current_price > 0) {
      prices[row.key] = row.current_price
    }
  }
  return prices
}

/** GET 业绩摘要的 holdings_detail → 服务端实际使用的估值价映射。 */
export function serverPriceMap(
  holdingsDetail: Array<Record<string, unknown>> | null | undefined
): Record<string, number> {
  const prices: Record<string, number> = {}
  for (const item of holdingsDetail || []) {
    const symbol = item.symbol
    const market = item.market
    const price = toNumber(item.current_price as number | string | null | undefined)
    if (typeof symbol === 'string' && typeof market === 'string' && price > 0) {
      prices[priceKey(symbol, market)] = price
    }
  }
  return prices
}
