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
export type SecurityEvent = components['schemas']['SecurityEventResponse']
export type DividendSuggestion = components['schemas']['SuggestionResponse']
export type BrokerImportResult = components['schemas']['BrokerImportResult']
export type SuspectedDuplicateSample = components['schemas']['SuspectedDuplicateSample']
export type BrokerImportSample = components['schemas']['BrokerImportSample']
export type ExchangeRate = components['schemas']['ExchangeRate']
export type ExchangeRateLatest = components['schemas']['ExchangeRateLatest']
export type User = components['schemas']['User']
export type LoginResponse = components['schemas']['LoginResponse']
export type ExcludedSecurity = components['schemas']['ExcludedSecurityResponse']
export type LlmReportAskResponse = components['schemas']['LlmReportAskResponse']
export type LlmReportListItem = components['schemas']['LlmReportListItem']
export type LlmReportDetail = components['schemas']['LlmReportDetail']
export type LlmReportMessage = components['schemas']['LlmReportMessageResponse']
export type LlmReportSchedule = components['schemas']['LlmReportScheduleResponse']
export type SecuritySearchItem = components['schemas']['SecuritySearchItem']
export type SecuritySearchResponse = components['schemas']['SecuritySearchResponse']
export type SecurityResolveResponse = components['schemas']['SecurityResolveResponse']
export type CatalogHealth = components['schemas']['CatalogHealth']

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
