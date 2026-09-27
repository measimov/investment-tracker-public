// 标的详情页页内类型（跨页共享形状见 types/index.ts）
import type { OpinionAuthorStance } from '@/types'

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

export interface AnalysisDetail {
  id: number
  name?: string | null
  tags: string[]
  risk_level: string
  summary: string
  content: string
  model?: string
  total_tokens?: number | null
  created_at?: string | null
  data_fetched_at?: string | null
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

// 档案端点是 Dict[str, Any]（无 OpenAPI schema），行形状随数据集而变
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type ProfileRow = Record<string, any>

export type WatchState = 'unknown' | 'watching' | 'not-watching'

/** 格雷厄姆估值依据（后端 graham_screen 的 criteria[].basis；港股/美股 = 行情价 ÷ 报表 TTM） */
export interface GrahamBasis {
  /** snapshot = 数据源直接给出（A股 Tushare 快照）；estimated = 行情价 ÷ 报表推算 */
  valuation_method?: 'snapshot' | 'estimated'
  label?: string
  price?: number | null
  price_currency?: string
  price_date?: string
  price_stale?: boolean
  price_age_days?: number
  price_source?: string
  share_ratio?: number
  share_ratio_note?: string
  /** 换算比来源：用户特例规则 / 20-F 封面自动解析（ads_ratio_service） */
  share_ratio_source?: 'rule' | '20-F'
  eps_ttm?: number
  method?: 'ttm' | 'annual'
  components?: Array<{
    period: string
    sign: '+' | '-'
    eps: number
    currency?: string | null
    eps_in_price_currency: number
  }>
  bvps?: number
  bvps_period?: string
  note?: string
}

/** 年报口径参考值（不参与判定） */
export interface GrahamSupplement {
  static_pe?: number | null
  static_basis?: string
  graham_avg3_pe?: number | null
  avg3_basis?: string
  basis?: string
}

export interface GrahamCriterion {
  criterion: string
  verdict: 'pass' | 'fail' | 'indeterminate'
  reason: string
  value?: number | null
  basis?: GrahamBasis
  supplement?: GrahamSupplement
}

/** 详情页全部数据状态（useSecurityProfile 持有，tab 子组件只读） */
export interface SecurityProfileState {
  analysis: AnalysisDetail | null
  datasets: Record<string, ProfileRow[]>
  latestPeriods: Record<string, string | null>
  events: ProfileRow[]
  supported: boolean
  capabilities: ProfileRow
  business: ProfileRow
  reportDigests: ProfileRow[]
  digestProgress: { digested: number; failed_capped: number }
  /** 港股「报表抽取」进度（其他市场 null） */
  statementProgress: ProfileRow | null
  earningsQuality: ProfileRow
  grahamScreen: ProfileRow
  /** 最新摘要生成/报表抽取时间（后端展示字段），判分析「可能过期」 */
  latestDataAt: string | null
  generating: boolean
  analysisJob: AnalysisJob | null
  backfilling: boolean
  backfillResult: ProfileRow | null
  watchState: WatchState
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
