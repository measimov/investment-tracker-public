/**
 * 统计页的页面私有类型与小工具（issue #140：Statistics.vue 按 feature 拆分）。
 *
 * 这些都是后端 Dict[str, Any] 统计端点的前端形状（无 Pydantic model），
 * 按 #141 的约定属于"跨组件共享的手写形状"——本页面内唯一权威副本。
 */

import { formatNumber } from '@/utils/helpers'

export interface TimeStat {
  period: string
  buy_amount: number
  sell_amount: number
  [key: string]: unknown
}

export interface ProfitLossItem {
  total_cost: number
  currency: string
  [key: string]: unknown
}

export interface SummaryStats {
  total_invested_cny?: number
  missing_rate_currencies?: string[]
  [key: string]: unknown
}

export interface CurrentPerformance {
  unrealized_pnl: number
  current_holdings_cost: number
  unrealized_pnl_rate: number
  current_market_value: number
  holdings_detail: Array<Record<string, unknown>>
  missing_rate_currencies?: string[]
  [key: string]: unknown
}

export interface RealizedPnL {
  realized_pnl: number
  sold_cost: number
  realized_pnl_rate: number
  trades_detail: Array<Record<string, unknown>>
  data_quality?: { warnings?: string[] }
  [key: string]: unknown
}

export interface DividendSummary {
  total_dividend_gross: number
  total_tax: number
  total_dividend_net: number
  by_symbol: Array<Record<string, unknown>>
  missing_rate_currencies?: string[]
  [key: string]: unknown
}

export interface TotalRealizedReturn {
  realized_trading_pnl_cny: number
  net_dividend_income_cny: number
  total_realized_return: number
  total_realized_return_rate: number
  sold_cost_cny: number
  [key: string]: unknown
}

export interface AccountReturn {
  total_return: number
  total_return_rate: number
  annualized_return_rate: number | null
  net_invested_principal_cny: number
  current_market_value_cny: number
  realized_trading_pnl_cny: number
  unrealized_pnl_cny: number
  net_dividend_income_cny: number
  [key: string]: unknown
}

export interface CurvePoint {
  date: string
  cumulative_return_rate?: number | string | null
  drawdown_rate?: number | string | null
  [key: string]: unknown
}

export interface AnalyticsMetrics {
  annualized_return_rate?: number | null
  max_drawdown_rate?: number | null
  sharpe_ratio?: number | null
  sortino_ratio?: number | null
  calmar_ratio?: number | null
  [key: string]: unknown
}

export interface TradeSkill {
  win_rate?: number | null
  payoff_ratio?: number | null
  profit_factor?: number | null
  [key: string]: unknown
}

export interface RangeSummary {
  realized_pnl_cny?: number | null
  dividend_net_cny?: number | null
  xirr_annualized_rate?: number | null
  [key: string]: unknown
}

export interface BenchmarkComparison {
  benchmark_total_return_rate?: number | null
  excess_return_rate?: number | null
  benchmark_max_drawdown_rate?: number | null
  [key: string]: unknown
}

export interface BenchmarkBlock {
  code: string
  name: string
  status: string
  alignment?: string
  points?: CurvePoint[]
  total_return_rate?: number | null
  comparison?: BenchmarkComparison | null
  [key: string]: unknown
}

export interface PerformanceAnalytics {
  calculation_level: string
  curve: CurvePoint[]
  benchmarks?: BenchmarkBlock[]
  metrics: AnalyticsMetrics
  trade_skill: TradeSkill
  range_summary?: RangeSummary
  date_range?: { start_date?: string; end_date?: string; clamped?: boolean } | null
  data_quality?: { warnings?: string[] }
  [key: string]: unknown
}

export interface HistorySyncJob {
  id?: number
  status?: string
  progress_percent?: number | string | null
  completed?: number
  total?: number
  current_symbol?: string | null
  current_market?: string | null
  success_count?: number
  skipped_count?: number
  failed_count?: number
  error?: string | null
  [key: string]: unknown
}

export interface PriceInputRow {
  symbol: string
  name?: string | null
  market: string
  avg_cost: number
  current_price: number | null
  quantity: number
}

export function formatNullableNumber(value: number | string | null | undefined, precision = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '--'
  return formatNumber(Number(value), precision)
}

export function formatNullablePercent(value: number | string | null | undefined, precision = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '--'
  return `${formatNumber(Number(value), precision)}%`
}
