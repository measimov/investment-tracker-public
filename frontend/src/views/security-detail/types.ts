// 标的详情页页内类型（跨页共享形状见 types/index.ts）
import type { AnalysisJob, OpinionAuthorStance } from '@/types'
import type { AnalysisDetail, ProfileRow } from '@/types'
export type { AnalysisDetail, ProfileRow } from '@/types'

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
  /** 换算比来源：用户特例规则 / 年报（20-F、10-K）封面自动解析（ads_ratio_service） */
  share_ratio_source?: 'rule' | '20-F' | '10-K'
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
  analysisLoading: boolean
  profileLoading: boolean
  analysisError: string
  profileError: string
  analysisHasLoaded: boolean
  profileHasLoaded: boolean
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
