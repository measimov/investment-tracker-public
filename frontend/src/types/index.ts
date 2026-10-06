/**
 * 前端类型层唯一入口（issue #141）。
 *
 * 两类来源，谁是权威分得很清楚：
 *
 * 1. **生成类型**（`api.generated.ts`，来自后端 OpenAPI）：凡后端有
 *    Pydantic response_model 的端点一律用这里的别名，禁止再逐 view 手写
 *    ——此前 BrokerAccount 手写了 3 份且形状各不相同。重新生成：
 *    `npm run generate:api-types`（CI 有漂移检查，schema 变了不重新生成
 *    会红）。
 *
 * 2. **手写共享形状**：后端声明为 `Dict[str, Any]` 的端点（statistics 全家、
 *    portfolio-snapshot 等）在 OpenAPI 里没有结构，前端形状以本文件为唯一
 *    权威副本——跨页共享的放这里（如 MarketStat 此前 Dashboard/Statistics
 *    各写一份），仅单页使用的仍留在 view 内。
 */

import type { components } from './api.generated'

// ---------------------------------------------------------------------------
// 生成类型别名（后端 Pydantic schema 是权威）
// ---------------------------------------------------------------------------

export type BrokerAccount = components['schemas']['BrokerAccountResponse']
export type Capabilities = components['schemas']['CapabilitiesResponse']
export type Transaction = components['schemas']['TransactionResponse']
export type CorporateAction = components['schemas']['CorporateActionResponse']
export type HoldingResponse = components['schemas']['HoldingResponse']
export type AdminHolding = components['schemas']['AdminHoldingResponse']
export type CashEvent = components['schemas']['CashEventResponse']
export type ImportBatch = components['schemas']['ImportBatchResponse']
export type ReconciliationSnapshot = components['schemas']['ReconciliationSnapshotResponse']
export type SecurityRule = components['schemas']['SecurityRuleResponse']
export type WatchlistItem = components['schemas']['WatchlistItemResponse']
export type WatchlistMembership = components['schemas']['WatchlistMembershipResponse']
export type CollectorStatus = components['schemas']['CollectorStatusResponse']
export type CollectorAuthor = components['schemas']['CollectorAuthorResponse']
export type CollectorAuthorCreate = components['schemas']['CollectorAuthorCreate']
export type CollectorAuthorUpdate = components['schemas']['CollectorAuthorUpdate']
export type CollectorCookieStatus = components['schemas']['CollectorCookieStatus']
export type CollectorCube = components['schemas']['CollectorCubeResponse']
export type CollectorCubeCreate = components['schemas']['CollectorCubeCreate']
export type CollectorCubeUpdate = components['schemas']['CollectorCubeUpdate']
export type CollectorSymbolsStatus = components['schemas']['CollectorSymbolsStatus']
export type XueqiuCookieAdminStatus = components['schemas']['XueqiuCookieAdminStatus']
export type XueqiuCookiePrimaryFact = components['schemas']['XueqiuCookiePrimaryFact']
export type XueqiuCookieUpdateRequest = components['schemas']['XueqiuCookieUpdateRequest']
export type XueqiuCookieUpdateResponse = components['schemas']['XueqiuCookieUpdateResponse']
export type XueqiuFeedPost = components['schemas']['XueqiuFeedPost']
export type XueqiuSymbolFeed = components['schemas']['XueqiuSymbolFeedResponse']
export type SecurityEvent = components['schemas']['SecurityEventResponse']
export type DividendSuggestion = components['schemas']['SuggestionResponse']
export type AlertList = components['schemas']['AlertListResponse']
export type AlertItem = components['schemas']['AlertItem']
export type NotifyChannelSummary = components['schemas']['NotifyChannelSummary']
export type NotifyResult = components['schemas']['NotifyResult']
export type NotificationEventList = components['schemas']['NotificationEventListResponse']
// 官方公告（#306）：同日同类文件合并的组
export type AnnouncementGroup = components['schemas']['AnnouncementGroup']
export type SecurityAnnouncements = components['schemas']['SecurityAnnouncementsResponse']
export type RecentAnnouncements = components['schemas']['RecentAnnouncementsResponse']
export type NotificationEventItem = components['schemas']['NotificationEventItem']
export type BrokerImportResult = components['schemas']['BrokerImportResult']
export type SuspectedDuplicateSample = components['schemas']['SuspectedDuplicateSample']
export type ExchangeRate = components['schemas']['ExchangeRate']
export type ExchangeRateCheck = components['schemas']['ExchangeRateCheck']
export type ExchangeRateLatest = components['schemas']['ExchangeRateLatest']
export type User = components['schemas']['User']
export type LoginResponse = components['schemas']['LoginResponse']
export type LlmReportAskResponse = components['schemas']['LlmReportAskResponse']
export type LlmReportListItem = components['schemas']['LlmReportListItem']
export type LlmReportDetail = components['schemas']['LlmReportDetail']
export type LlmReportMessage = components['schemas']['LlmReportMessageResponse']
export type LlmReportSchedule = components['schemas']['LlmReportScheduleResponse']
export type SecuritySearchItem = components['schemas']['SecuritySearchItem']
export type SecuritySearchResponse = components['schemas']['SecuritySearchResponse']
export type SecurityResolveResponse = components['schemas']['SecurityResolveResponse']
export type SecurityIndustryItem = components['schemas']['SecurityIndustryItem']

// ---------------------------------------------------------------------------
// 手写共享形状（后端 Dict[str, Any] 端点；本文件是前端形状的唯一权威副本）
// ---------------------------------------------------------------------------

/** 标准 CSV/Excel 导入响应（后端为 dict，无 Pydantic model） */
export interface StandardImportResult {
  message: string
  count: number
  affected_symbols?: number
}

/** GET /statistics/by-market 单项（Dashboard 与 Statistics 共用） */
/** 服务端累计收益双口径。待收按已知税前金额估算，缺口不冒充零。 */
export interface ReceivableReturn {
  as_of: string
  cash_basis_return_cny: number | null
  known_pending_gross_cny: number
  known_pending_gross_by_currency: Record<string, number>
  estimated_return_cny: number | null
  included_count: number
  unresolved_count: number
  overdue_count: number
  pending_overdue_count: number
  review_counts: {
    received: number
    possible_receipt: number
    entitlement: number
    amount: number
    zero_remaining: number
  }
  received_review_reasons: {
    net_amount_only: number
    currency_mismatch: number
    payout_currency_unverified: number
    other: number
  }
  missing_rate_currencies: string[]
  is_partial: boolean
}

export interface MarketStat {
  market: string
  total_cost: number
  total_cost_cny?: number
  total_cost_usd?: number
  total_cost_by_currency?: Record<string, number>
  holdings_count?: number
  missing_rate_currencies?: string[]
  [key: string]: unknown
}

// ---------------------------------------------------------------------------
// 雪球观点摘要（后端 Dict[str, Any] 端点，无 OpenAPI schema，手写唯一权威副本；
// 跨页共享：Holdings 角标 + 观点页 + 标的详情页）
// ---------------------------------------------------------------------------

export interface OpinionAuthorStance {
  author: string
  stance: string // 看多 | 看空 | 中性 | 不明
  recent_change: string // 转多 | 转空 | 新增 | 无
  evidence: string
}

export interface OpinionFreshness {
  available: boolean
  latest_scan_at: string | null
  latest_utterance_at: string | null
  stale: boolean
}

export interface OpinionSummaryRow {
  id: number | null // null = 有匹配发言但尚未生成摘要
  symbol: string
  market: string
  name: string | null
  tags: string[]
  summary: string | null
  author_stances: OpinionAuthorStance[]
  utterance_count: number | null
  recent_utterance_count: number | null
  recent_days: number | null
  latest_utterance_at: string | null
  created_at: string | null
  origin: string // holding | watchlist | both
  matched_count: number | null // 数据源不可用时为 null
  new_utterance_count: number | null
}

export interface OpinionSummariesResponse {
  source_available: boolean
  freshness: OpinionFreshness
  recent_days: number
  items: OpinionSummaryRow[]
}

export interface OpinionFeedItem {
  date: string | null
  kind: string
  text: string
  context: string | null
  post_url: string | null
  symbols: Array<{ symbol: string; market: string }>
}

export interface OpinionFeedAuthor {
  author: string
  total: number
  items: OpinionFeedItem[]
}

// ---------------------------------------------------------------------------
// GET /statistics/period-pnl（后端 Dict[str, Any] 端点，形状以 statistics/period_pnl.py 为准）
// ---------------------------------------------------------------------------
export type PeriodPnlKey = 'daily' | 'mtd' | 'ytd'

export interface PeriodPnlBasis {
  symbol: string
  market: string
  basis_date: string
  /** history = 收盘价；transaction = 只能用成交价估值 */
  basis_source: string
}

/** 期间损益 = 实收损益 + 期末待收 - 期初待收；两端按各自日期汇率折算。 */
export interface PeriodReceivablePnl {
  cash_basis_pnl_cny: number | null
  opening_receivable_cny: number
  closing_receivable_cny: number
  receivable_change_cny: number
  estimated_pnl_cny: number | null
  is_partial: boolean
  unresolved_count: number
  missing_rate_currencies: string[]
}

export interface PeriodPnlSummary {
  label: string
  start_date: string
  end_date: string
  /** exact 可靠；estimated 期初基准陈旧（含此前累积涨跌）或期末估值价早于期初基准；
   *  unavailable 期初有持仓完全无价，不给数 */
  status: 'exact' | 'estimated' | 'unavailable'
  /** unavailable 时为 null */
  pnl_cny: number | null
  /** 区间时间加权收益率（%）；区间内无有效估值点或 unavailable 时为 null */
  return_rate: number | null
  stale_opening_basis: PeriodPnlBasis[]
  /** 期末估值价（持仓现价）的行情日期早于期初基准：期末按旧价计（#267） */
  stale_closing_prices?: Array<{
    symbol: string
    market: string
    price_date: string
    basis_date: string
  }>
  opening_unpriced_positions: Array<{ symbol: string; market: string }>
  opening_market_value_cny: number
  closing_market_value_cny: number
  cash_in_cny: number
  cash_out_cny: number
  dividend_income_cny: number
  points: number
  unpriced_positions: Array<{ symbol: string; market: string }>
  stale_price_positions: Array<{ symbol: string; market: string }>
  estimated_inflow_events?: number
  /** 本月 / 本年补充待收口径；当日仍按实收计算。 */
  receivable_pnl?: PeriodReceivablePnl
}

export interface PeriodPnlResponse {
  base_currency: string
  as_of: string
  methodology: { scope: string; method: string; status: string; description: string }
  periods: Record<PeriodPnlKey, PeriodPnlSummary>
  data_quality: { warnings: string[]; [key: string]: unknown }
}

// ---------------------------------------------------------------------------- 研究端点与后台任务
// 这些端点后端是 Dict[str, Any]（无 OpenAPI schema）：形状手写在这里，api 层据此给响应挂泛型
// （#284：此前定义在各 view 目录，api 层拿不到，于是 view 里 `response.data as X` 强转）。
// 批量 job 字段全部可选 + index signature：进度是渐进回写的，任何字段都可能暂缺。

export interface OpinionBatchJob {
  id?: string
  type?: string
  status?: string
  total?: number
  completed?: number
  progress_percent?: number | string | null
  success_count?: number
  failed_count?: number
  skipped_count?: number
  current_symbol?: string | null
  current_market?: string | null
  current_stage?: string | null
  results?: Array<{
    symbol: string
    market: string
    status: string
    tags?: string[]
    error?: string | null
    reason?: string | null
  }>
  abort_reason?: string | null
  cancelled?: boolean
  started_at?: string | null
  [key: string]: unknown
}

export interface AnalysisSummaryRow {
  symbol: string
  market: string
  tags: string[]
  risk_level: string
  summary: string
  /** 分析生成时刻（带时区 ISO） */
  created_at?: string | null
  /** 最新一次财报摘要生成/报表抽取时刻（与详情页同一判定）；晚于 created_at = 可能过期 */
  latest_data_at?: string | null
}

export interface BatchResultRow {
  symbol: string
  market: string
  status: string
  error?: string | null
  reason?: string | null
}

export interface AnalysisBatchJob {
  id?: string
  status?: string
  total?: number
  completed?: number
  progress_percent?: number | string | null
  success_count?: number
  failed_count?: number
  skipped_count?: number
  current_symbol?: string | null
  current_market?: string | null
  current_stage?: string | null
  results?: BatchResultRow[]
  abort_reason?: string | null
  cancelled?: boolean
  started_at?: string | null
  [key: string]: unknown
}

export interface DigestBatchJob {
  id: string
  type?: string
  status: string
  total?: number
  completed?: number
  progress_percent?: number
  success_count?: number
  failed_count?: number
  digests_generated?: number
  digests_blocked?: number
  symbols_with_remaining?: number
  // 港股顺带的三张报表抽取（其他市场不计）
  statements_generated?: number
  statements_blocked?: number
  statements_suspect?: number
  current_symbol?: string | null
  current_market?: string | null
  cancelled?: boolean
  abort_reason?: string | null
  started_at?: string | null
  [key: string]: unknown
}

export interface OpinionSummaryDetail {
  id: number
  symbol: string
  market: string
  name: string | null
  tags: string[]
  summary: string
  author_stances: OpinionAuthorStance[]
  content: string
  model: string
  total_tokens: number | null
  utterance_count: number
  recent_utterance_count: number
  recent_days: number
  lookback_days: number
  latest_utterance_at: string | null
  created_at: string
  previous: { tags: string[]; summary: string; created_at: string | null } | null
}

export interface AnalysisJob {
  id?: string
  status?: string
  stage?: string | null
  stage_label?: string | null
  completed?: number
  total?: number
  progress_percent?: number | string | null
  error?: string | null
  [key: string]: unknown
}

export interface OpinionJob {
  id: string
  status: string
  stage?: string | null
  stage_label?: string | null
  completed?: number
  total?: number
  error?: string | null
  summary_id?: number | null
  [key: string]: unknown
}

/** /securities/active-analysis-jobs 的一行：分析家族任意一种任务（按 type 区分），可直接交给
 *  AnalysisBatchJob / DigestBatchJob / OpinionBatchJob 的消费方——其余字段都是可选的。 */
export interface ActiveAnalysisJob {
  id: string
  type: string
  status: string
  [key: string]: unknown
}

/** 入队接口的返回：任务一定带 id（进度形状里 id 可选，是因为状态对象也用于本地占位） */
export type StartedJob<T> = T & { id: string }

export interface RiskLevelAdjustment {
  from: string
  to: string
  reason?: string | null
}

export interface OutputAdjustment {
  type: string
  tag?: string
  from?: string
  to?: string
  reason?: string
  dropped?: string[]
  sections?: string[]
  tags?: string[]
  reasons?: string[]
}

export interface AnalysisDetail {
  id: number
  name?: string | null
  tags: string[]
  risk_level: string
  /** 风险等级按市场下限上调的记录（港股 low→medium）；未上调或旧分析行为 null */
  risk_level_adjusted?: RiskLevelAdjustment | null
  /** 解析层对模型输出的调整记录（标签归一/丢弃/截断、补免责声明）；无调整或旧分析行为 null */
  output_adjustments?: OutputAdjustment[] | null
  summary: string
  content: string
  model?: string
  total_tokens?: number | null
  created_at?: string | null
  data_fetched_at?: string | null
}

// 档案端点是 Dict[str, Any]（无 OpenAPI schema），行形状随数据集而变
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type ProfileRow = Record<string, any>

export interface OpinionBatchTarget {
  symbol: string
  market: string
  origin: string
  matched_count: number
  recent_count: number
  latest_matched_at?: string | null
}

/** 研究 API 的固定响应外壳；数据集内部仍保留实际动态形状。 */
export interface SecurityProfileResponse {
  symbol: string
  market: string
  datasets: Record<string, ProfileRow[]>
  fetched_at: string | null
  row_counts: Record<string, number>
  latest_periods: Record<string, string | null>
  events: ProfileRow[]
  supported: boolean
  capabilities: ProfileRow
  report_digests: ProfileRow[]
  digest_progress: { digested: number; failed_capped: number }
  statement_progress: ProfileRow | null
  latest_data_at: string | null
  business: ProfileRow
  earnings_quality: ProfileRow
  graham_screen: ProfileRow
}

export interface OpinionFeedResponse {
  source_available: boolean
  freshness: OpinionFreshness
  authors: OpinionFeedAuthor[]
}

export interface AnalysisBatchTargetsResponse {
  total: number
  targets: Array<{ symbol: string; market: string }>
}

export interface OpinionBatchTargetsResponse {
  targets: OpinionBatchTarget[]
  source_available: boolean
  freshness: OpinionFreshness
}

export interface DigestBackfillPreview {
  targets_total: number
  targets_without_digest: number
  digests_existing: number
  per_symbol_budget: number
}

/** result 在排队时为 null；失败任务也可能保留部分回填结果。 */
export interface ReportBackfillJob {
  id: string
  status: string
  result: ProfileRow | null
  error?: string | null
  [key: string]: unknown
}

/** report_id 仅在报告成功落库后写入，排队/运行/失败时可能仍为 null。 */
export interface LlmReportJob {
  id: string
  status: string
  report_id: number | null
  error?: string | null
  [key: string]: unknown
}

// 写接口的请求体（#284：api 层写方法此前一律 Record<string, unknown>）
export type BrokerAccountCreate = components['schemas']['BrokerAccountCreate']
export type BrokerAccountUpdate = components['schemas']['BrokerAccountUpdate']
export type CashEventCreate = components['schemas']['CashEventCreate']
export type CashEventUpdate = components['schemas']['CashEventUpdate']
export type DividendTaxAllocationsUpdate = components['schemas']['DividendTaxAllocationsUpdate']
export type CorporateActionCreate = components['schemas']['CorporateActionCreate']
export type CorporateActionUpdate = components['schemas']['CorporateActionUpdate']
export type ExchangeRateCreate = components['schemas']['ExchangeRateCreate']
export type ExchangeRateUpdate = components['schemas']['ExchangeRateUpdate']
export type OpeningPositionCostUpdate = components['schemas']['OpeningPositionCostUpdate']
export type ReconciliationSnapshotCreate = components['schemas']['ReconciliationSnapshotCreate']
export type ReconciliationSnapshotUpdate = components['schemas']['ReconciliationSnapshotUpdate']
export type SecurityRuleCreate = components['schemas']['SecurityRuleCreate']
export type SuggestionReceiptsUpdate = components['schemas']['SuggestionReceiptsUpdate']
export type SuggestionAccept = components['schemas']['SuggestionAccept']
export type TransactionCreate = components['schemas']['TransactionCreate']
export type TransactionUpdate = components['schemas']['TransactionUpdate']
export type TransferCreate = components['schemas']['TransferCreate']
export type UserCreate = components['schemas']['UserCreate']
export type UserUpdate = components['schemas']['UserUpdate']
export type WatchlistItemCreate = components['schemas']['WatchlistItemCreate']
export type WatchlistItemUpdate = components['schemas']['WatchlistItemUpdate']

// 统计 Dict API 响应的唯一权威形状。
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
  current_market_value_usd?: number | null
  unrealized_pnl_cny: number | null
  current_holdings_cost_cny: number | null
  unrealized_pnl_rate: number | null
  current_market_value_cny: number | null
  holdings_detail: Array<Record<string, unknown>>
  missing_rate_currencies?: string[]
  data_quality?: { warnings?: string[]; unpriced_position_count?: number }
  [key: string]: unknown
}

export interface RealizedPnL {
  realized_pnl: number | null
  sold_cost: number | null
  realized_pnl_rate: number | null
  trades_detail: Array<Record<string, unknown>>
  missing_rate_currencies?: string[]
  data_quality?: { warnings?: string[] }
  [key: string]: unknown
}

export interface DividendSummary {
  legacy_unreviewed_count?: number
  amounts_incomplete_count?: number
  forecast?: {
    pending_count: number
    overdue_count: number
    unknown_amount_count: number
    pending_gross_by_currency: Record<string, number | string>
    announced_gross_by_currency: Record<string, number | string>
  }
  total_dividend_gross: number | null
  total_tax: number | null
  total_dividend_net: number | null
  unallocated_tax_cny?: number
  unallocated_tax_count?: number
  by_symbol: Array<Record<string, unknown>>
  missing_rate_currencies?: string[]
  [key: string]: unknown
}

export interface TotalRealizedReturn {
  total_realized_return_cny?: number | null
  realized_trading_pnl_cny: number | null
  net_dividend_income_cny: number | null
  total_realized_return: number | null
  total_realized_return_rate: number | null
  sold_cost_cny: number | null
  [key: string]: unknown
}

export interface AccountReturn {
  total_return_cny?: number | null
  total_return: number | null
  total_return_rate: number | null
  annualized_return_rate: number | null
  net_invested_principal_cny: number | null
  current_market_value_cny: number | null
  realized_trading_pnl_cny: number | null
  unrealized_pnl_cny: number | null
  net_dividend_income_cny: number | null
  /** 收益率分母口径：净投入为正时用净投入，否则（清仓后）用峰值投入 */
  rate_denominator?: string
  peak_invested_principal_cny?: number | null
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
  id: string
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

/** POST 返回共同六块；GET 另附价格新鲜度。 */
export interface PerformanceSummary {
  current_performance: CurrentPerformance
  realized_pnl: RealizedPnL
  dividend_summary: DividendSummary
  total_realized_return: TotalRealizedReturn
  account_return: AccountReturn
  receivable_return?: ReceivableReturn
  price_freshness?: Record<string, PriceFreshnessEntry>
}
export interface BenchmarkCatalogItem {
  code: string
  name: string
  currency: string
}
export interface ReconciliationBadge {
  status?: string
  all_scoped?: boolean
  [key: string]: unknown
}

interface AccountBadge {
  id: number
  account_name: string
  latest_reconciliation?: ReconciliationBadge | null
  [key: string]: unknown
}

export type RecentTransaction = Pick<
  Transaction,
  | 'symbol'
  | 'name'
  | 'market'
  | 'transaction_type'
  | 'quantity'
  | 'price'
  | 'transaction_date'
  | 'currency'
>

export interface PortfolioSnapshot {
  performance: PerformanceSummary
  prices: {
    missing_keys?: string[]
    stale_keys?: string[]
    freshness?: Record<string, PriceFreshnessEntry>
  }
  markets: MarketStat[]
  recent_transactions: RecentTransaction[]
  accounts: AccountBadge[]
  data_quality?: { warnings?: string[] }
  [key: string]: unknown
}

export interface PriceFreshnessEntry {
  source?: string
  price_as_of?: string | null
  price_date?: string | null
  stale?: boolean
  name?: string | null
}
