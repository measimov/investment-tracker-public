/**
 * 持仓页拆分（issue #140）私有类型。
 * 批量 job 的行结构以后端 background_job payload 为准，字段全部可选 +
 * index signature：job 进度是渐进回写的，任何字段都可能暂缺。
 */

export interface TransferForm {
  symbol: string
  market: string
  from_broker_account_id: number | null | undefined
  to_broker_account_id: number | 'unassigned' | null | undefined
  quantity: number
  max_quantity: number
  transfer_date: string
  notes: string
}

export interface AnalysisSummaryRow {
  symbol: string
  market: string
  tags: string[]
  risk_level: string
  summary: string
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
  current_symbol?: string | null
  current_market?: string | null
  cancelled?: boolean
  abort_reason?: string | null
  started_at?: string | null
  [key: string]: unknown
}
