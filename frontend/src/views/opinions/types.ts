// 观点页页内类型（跨页共享形状见 types/index.ts）

export interface OpinionBatchTarget {
  symbol: string
  market: string
  origin: string
  matched_count: number
  recent_count: number
  latest_matched_at?: string | null
}

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

// OpinionFeedItem / OpinionFeedAuthor 已上移 types/index.ts（security-detail
// 也要用，跨页共享形状的唯一权威副本在那边）
