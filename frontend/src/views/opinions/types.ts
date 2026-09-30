// 观点页页内类型（跨页共享形状见 types/index.ts）

export interface OpinionBatchTarget {
  symbol: string
  market: string
  origin: string
  matched_count: number
  recent_count: number
  latest_matched_at?: string | null
}

// OpinionFeedItem / OpinionFeedAuthor 已上移 types/index.ts（security-detail
// 也要用，跨页共享形状的唯一权威副本在那边）
