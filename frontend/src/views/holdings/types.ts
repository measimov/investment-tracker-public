/**
 * 持仓页拆分（issue #140）私有类型。
 * 批量 job 的行结构以后端 background_job payload 为准，字段全部可选 +
 * index signature：job 进度是渐进回写的，任何字段都可能暂缺。
 */

import type { UnassignedAccount } from '@/utils/labels'

export interface TransferForm {
  symbol: string
  market: string
  from_broker_account_id: number | null | undefined
  to_broker_account_id: number | UnassignedAccount | null | undefined
  quantity: number
  max_quantity: number
  transfer_date: string
  notes: string
}
