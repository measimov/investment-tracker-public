/**
 * 统计页的页面私有类型与小工具（issue #140：Statistics.vue 按 feature 拆分）。
 *
 * 这些都是后端 Dict[str, Any] 统计端点的前端形状（无 Pydantic model），
 * 按 #141 的约定属于"跨组件共享的手写形状"——本页面内唯一权威副本。
 */

export interface TimeStat {
  period: string
  buy_amount: number
  sell_amount: number
  [key: string]: unknown
}

/** 持仓排行（后端按标的合并各账户行、按 CNY 成本降序） */
export interface ProfitLossItem {
  symbol: string
  name?: string | null
  market: string
  quantity: number
  avg_cost: number
  total_cost: number
  currency: string
  /** 最新汇率折 CNY；缺汇率时为 null */
  total_cost_cny?: number | null
  missing_rate?: boolean
  account_count?: number
  [key: string]: unknown
}

export interface SummaryStats {
  total_invested_cny?: number
  missing_rate_currencies?: string[]
  [key: string]: unknown
}

export interface CurrentPerformance {
  unrealized_pnl_cny: number
  current_holdings_cost_cny: number
  unrealized_pnl_rate: number
  current_market_value_cny: number
  holdings_detail: Array<Record<string, unknown>>
  missing_rate_currencies?: string[]
  data_quality?: { warnings?: string[]; unpriced_position_count?: number }
  [key: string]: unknown
}

export interface RealizedPnL {
  realized_pnl: number
  sold_cost: number
  realized_pnl_rate: number
  trades_detail: Array<Record<string, unknown>>
  missing_rate_currencies?: string[]
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
  /** 收益率分母口径：净投入为正时用净投入，否则（清仓后）用峰值投入 */
  rate_denominator?: string
  peak_invested_principal_cny?: number
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
  observation_span_days?: number
  risk_free_rate?: number
  max_drawdown_rate?: number | null
  sharpe_ratio?: number | null
  sortino_ratio?: number | null
  calmar_ratio?: number | null
  [key: string]: unknown
}

export interface TradeSkill {
  /** 无有效平仓样本时为 null（不是 0%） */
  win_rate?: number | null
  sample_count?: number
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

/** 夏普/索提诺所用无风险利率的口径（#200） */
export interface RiskFreeInfo {
  /** series = 参考利率日序列；constant = 请求指定常量；none = 无数据按 0 */
  basis: 'series' | 'constant' | 'none'
  series?: string | null
  label?: string | null
  currency?: string
  /** 各收益点所用年化利率（%）的均值 */
  average?: number | null
  first_date?: string
  last_date?: string
  published_points?: number
  missing_points?: number
  note?: string
}

export interface PerformanceAnalytics {
  calculation_level: string
  risk_free?: RiskFreeInfo
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

/** 价格弹窗行：按 `symbol:market` 合并各账户持仓（见 priceRows.ts） */
export interface PriceInputRow {
  /** `symbol:market`——POST 价格映射的键 */
  key: string
  symbol: string
  name?: string | null
  market: string
  currency: string
  avg_cost: number
  current_price: number | null
  quantity: number
  account_count: number
}
