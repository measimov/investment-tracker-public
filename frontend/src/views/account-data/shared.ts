/**
 * 账户数据页的页面私有共享层（issue #140：AccountData.vue 按 tab 边界拆分）。
 *
 * 行类型、下拉常量与三个 CRUD 工厂在五个 tab 组件间共用；只服务本页面，
 * 不是全局共享组件层的一部分。
 */

import { ElMessage, ElMessageBox, type FormInstance } from 'element-plus'
import { MARKETS } from '@/utils/securities'
import type { Ref } from 'vue'
import type {
  BrokerAccount,
  CashEvent,
  ImportBatch,
  ReconciliationSnapshot,
  SecurityRule
} from '@/types'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { formatLocalDate } from '@/utils/dateRange'
import { todayLocalISODate } from '@/utils/helpers'

// 生成类型为准；旧别名字段（历史模板回退读多种键名）以交集补充（下同）
export type AccountRow = BrokerAccount & {
  name?: string
  broker_name?: string
  [key: string]: unknown
}

export type CashEventRow = CashEvent & {
  account_id?: number | null
  occurred_at?: string
  [key: string]: unknown
}

export type ImportBatchRow = ImportBatch & {
  account_id?: number | null
  imported_at?: string
  statement_start_date?: string
  statement_end_date?: string
  original_filename?: string | null
  file_sha256?: string | null
  total_rows?: number | null
  [key: string]: unknown
}

export interface DiffDetail {
  positions?: Array<Record<string, unknown>>
  cash?: Array<Record<string, unknown>>
  summary?: { cash_compared?: boolean; [key: string]: unknown }
  replay_inconsistent?: Array<{ symbol?: string; market?: string; reason?: string }>
  methodology_notes?: string[]
  [key: string]: unknown
}

export type SnapshotRow = ReconciliationSnapshot & {
  account_id?: number | null
  reported_cash?: unknown
  reported_positions?: unknown
  diff_detail?: DiffDetail | null
  [key: string]: unknown
}

export type SecurityRuleRow = SecurityRule

export interface DialogState {
  visible: boolean
  id: number | null
  saving: boolean
}

export const brokerOptions = ['招商证券', '东方财富证券', 'IBKR', '汇丰香港']
export const currencyOptions = ['CNY', 'HKD', 'USD', 'SGD']
// 与后端 VALID_MARKETS 对齐（快照持仓行与特例规则表单共用）；唯一权威在 utils/securities
export const marketOptions: readonly string[] = MARKETS

export const today = () => todayLocalISODate()
export const monthEnd = () => {
  const now = new Date()
  return formatLocalDate(new Date(now.getFullYear(), now.getMonth(), 0))
}

export const accountName = (account: AccountRow | null | undefined) =>
  account?.account_name ||
  account?.name ||
  [account?.broker, account?.account_number_masked].filter(Boolean).join(' ') ||
  '未命名账户'

export const accountLabelIn = (accounts: AccountRow[], id: unknown) => {
  const account = accounts.find((item) => String(item.id) === String(id))
  return account ? accountName(account) : '未关联账户'
}

export async function confirmDelete(title: string, message: string) {
  await ElMessageBox.confirm(message, title, {
    type: 'warning',
    confirmButtonText: '删除',
    cancelButtonText: '取消'
  })
}

// 表单保存工厂：校验 → 创建/更新 → 按分支提示 → 关窗重载（快照另有专属校验，不并入）
export function makeSaver({
  formRef,
  dialog,
  buildPayload,
  update,
  create,
  messages,
  reload
}: {
  formRef: Ref<FormInstance | undefined>
  dialog: DialogState
  buildPayload: () => Record<string, unknown>
  update: (id: number, payload: Record<string, unknown>) => Promise<unknown>
  create: (payload: Record<string, unknown>) => Promise<unknown>
  messages: { updated: string; created: string; failure: string }
  reload: () => Promise<unknown>
}) {
  return async () => {
    if (!(await formRef.value?.validate().catch(() => false))) return
    dialog.saving = true
    try {
      const payload = buildPayload()
      if (dialog.id) await update(dialog.id, payload)
      else await create(payload)
      ElMessage.success(dialog.id ? messages.updated : messages.created)
      dialog.visible = false
      await reload()
    } catch (error) {
      ElMessage.error(getApiErrorMessage(error, messages.failure))
    } finally {
      dialog.saving = false
    }
  }
}

// 删除处理工厂：确认 → 删除 → 成功提示 → 重载；message 支持函数以插入行数据
export function makeRemover<T>({
  title,
  message,
  request,
  successMessage,
  failureMessage,
  reload
}: {
  title: string
  message: string | ((row: T) => string)
  request: (row: T) => Promise<unknown>
  successMessage: string
  failureMessage: string
  reload: () => Promise<unknown>
}) {
  return async (row: T) => {
    try {
      await confirmDelete(title, typeof message === 'function' ? message(row) : message)
      await request(row)
      ElMessage.success(successMessage)
      await reload()
    } catch (error) {
      if (error !== 'cancel' && error !== 'close')
        ElMessage.error(getApiErrorMessage(error, failureMessage))
    }
  }
}
