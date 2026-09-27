import { toNumber } from '../../utils/helpers'
import type { AdminHolding } from '../../types'

/** 折人民币：缺汇率返回 null（useExchangeRates().convertToCNY 的签名） */
export type ToCNY = (amount: number, currency: string | null | undefined) => number | null

function hasPrice(row: AdminHolding): boolean {
  return row.current_price !== null && row.current_price !== undefined && row.current_price !== ''
}

/** 原币市值；缺现价返回 null——不能按 0 算，否则整行显示成全亏 */
export function rowMarketValue(row: AdminHolding): number | null {
  if (!hasPrice(row)) return null
  return toNumber(row.quantity) * toNumber(row.current_price)
}

/** 原币浮动盈亏；缺现价返回 null */
export function rowProfit(row: AdminHolding): number | null {
  const value = rowMarketValue(row)
  return value === null ? null : value - toNumber(row.total_cost)
}

/** 盈亏率（%）；缺现价或成本为 0 返回 null */
export function rowProfitPercent(row: AdminHolding): number | null {
  const profit = rowProfit(row)
  const cost = toNumber(row.total_cost)
  if (profit === null || cost === 0) return null
  return (profit / cost) * 100
}

export interface AdminHoldingsSummary {
  /** 有汇率的全部行的成本合计（CNY） */
  totalCostCNY: number
  /** 有现价且有汇率的行的市值合计（CNY） */
  totalValueCNY: number
  /** 与市值同一批行（有现价且有汇率）的浮动盈亏合计（CNY）——成本口径与市值对齐 */
  profitCNY: number
  /** 缺现价的行数：不计入市值与盈亏 */
  unpricedCount: number
  /** 缺汇率的行数：不计入任何 CNY 汇总 */
  missingRateCount: number
  /** 缺汇率的币种（去重，按出现顺序） */
  missingRateCurrencies: string[]
}

/**
 * 管理员「所有持仓」的 CNY 汇总（#219）。此前直接把各币种原币金额相加并标成
 * CNY，缺价行按 0 计入市值——港元/美元持仓与缺价持仓都会让汇总失真。
 * 缺汇率的行整行剔除（不按原值混入），缺现价的行只计入成本、不计入市值与盈亏。
 */
export function summarizeAdminHoldings(rows: AdminHolding[], toCNY: ToCNY): AdminHoldingsSummary {
  const summary: AdminHoldingsSummary = {
    totalCostCNY: 0,
    totalValueCNY: 0,
    profitCNY: 0,
    unpricedCount: 0,
    missingRateCount: 0,
    missingRateCurrencies: []
  }

  for (const row of rows) {
    const costCNY = toCNY(toNumber(row.total_cost), row.currency)
    if (costCNY === null) {
      summary.missingRateCount += 1
      if (!summary.missingRateCurrencies.includes(row.currency)) {
        summary.missingRateCurrencies.push(row.currency)
      }
      continue
    }
    summary.totalCostCNY += costCNY

    const value = rowMarketValue(row)
    if (value === null) {
      summary.unpricedCount += 1
      continue
    }
    // 同一币种的汇率已在上面确认存在，这里不会是 null
    const valueCNY = toCNY(value, row.currency) ?? 0
    summary.totalValueCNY += valueCNY
    summary.profitCNY += valueCNY - costCNY
  }

  return summary
}
