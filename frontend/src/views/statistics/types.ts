/** API 响应类型由公共类型层定义；价格表单保留本页。 */
export type {
  TimeStat,
  ProfitLossItem,
  SummaryStats,
  CurrentPerformance,
  RealizedPnL,
  DividendSummary,
  TotalRealizedReturn,
  AccountReturn,
  CurvePoint,
  AnalyticsMetrics,
  TradeSkill,
  RangeSummary,
  BenchmarkComparison,
  BenchmarkBlock,
  RiskFreeInfo,
  PerformanceAnalytics,
  HistorySyncJob
} from '@/types'

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
