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

// 写接口的请求体（#284：api 层写方法此前一律 Record<string, unknown>）
export type BrokerAccountCreate = components['schemas']['BrokerAccountCreate']
export type BrokerAccountUpdate = components['schemas']['BrokerAccountUpdate']
export type CashEventCreate = components['schemas']['CashEventCreate']
export type CashEventUpdate = components['schemas']['CashEventUpdate']
export type CorporateActionCreate = components['schemas']['CorporateActionCreate']
export type CorporateActionUpdate = components['schemas']['CorporateActionUpdate']
export type ExchangeRateCreate = components['schemas']['ExchangeRateCreate']
export type ExchangeRateUpdate = components['schemas']['ExchangeRateUpdate']
export type OpeningPositionCostUpdate = components['schemas']['OpeningPositionCostUpdate']
export type ReconciliationSnapshotCreate = components['schemas']['ReconciliationSnapshotCreate']
export type ReconciliationSnapshotUpdate = components['schemas']['ReconciliationSnapshotUpdate']
export type SecurityRuleCreate = components['schemas']['SecurityRuleCreate']
export type SuggestionAccept = components['schemas']['SuggestionAccept']
export type TransactionCreate = components['schemas']['TransactionCreate']
export type TransactionUpdate = components['schemas']['TransactionUpdate']
export type TransferCreate = components['schemas']['TransferCreate']
export type UserCreate = components['schemas']['UserCreate']
export type UserUpdate = components['schemas']['UserUpdate']
export type WatchlistItemCreate = components['schemas']['WatchlistItemCreate']
export type WatchlistItemUpdate = components['schemas']['WatchlistItemUpdate']
