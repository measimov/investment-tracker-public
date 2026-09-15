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
