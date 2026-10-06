/**
 * 账户数据页的页面私有共享层（issue #140：AccountData.vue 按 tab 边界拆分）。
 *
 * 行类型、下拉常量与三个 CRUD 工厂在五个 tab 组件间共用；只服务本页面，
 * 不是全局共享组件层的一部分。
 */

import { ElMessage, type FormInstance } from 'element-plus'
import { MARKETS } from '@/utils/securities'
import { DELETED_ACCOUNT_LABEL, UNASSIGNED_ACCOUNT_LABEL } from '@/utils/labels'
import { h, type Ref } from 'vue'
import type {
  BrokerAccount,
  CashEvent,
  ImportBatch,
  ReconciliationSnapshot,
  SecurityRule
} from '@/types'
import { formatLocalDate } from '@/utils/dateRange'
import { todayLocalISODate } from '@/utils/helpers'
import { showApiError } from '@/utils/showApiError'

// 行类型即生成类型（此前叠加的一批旧键名与 `[key: string]: unknown` 已删除：
// 那些键后端早已不返回，索引签名还会让拼错的字段名照样通过 typecheck）
export type AccountRow = BrokerAccount
export type CashEventRow = CashEvent
export type ImportBatchRow = ImportBatch

export interface DiffDetail {
  positions?: Array<Record<string, unknown>>
  cash?: Array<Record<string, unknown>>
  summary?: { cash_compared?: boolean; [key: string]: unknown }
  replay_inconsistent?: Array<{ symbol?: string; market?: string; reason?: string }>
  methodology_notes?: string[]
  [key: string]: unknown
}

// diff_detail 在后端是自由 JSON，这里收窄成比对结果的已知形状
export type SnapshotRow = Omit<ReconciliationSnapshot, 'diff_detail'> & {
  diff_detail?: DiffDetail | null
}

export type SecurityRuleRow = SecurityRule

export interface DialogState {
  visible: boolean
  id: number | null
  saving: boolean
}

export const brokerOptions = ['招商证券', '东方财富证券', 'IBKR', '汇丰香港']

// 现金事件 / 导入批次 / 月末核对一次取回的上限（不分页）：满额时界面提示
// 「仅显示最近 N 条」，筛选与汇总只在已取回的数据里进行
export const LIST_LIMIT = 1000

export const isAtListLimit = (rows: readonly unknown[], limit = LIST_LIMIT) => rows.length >= limit

// 月末核对快照的报表范围（statement_scope）：东财普通股票/港股通两份对账单同日各一行
const SCOPE_LABELS: Record<string, string> = {
  stock: '普通股票',
  hk_connect: '港股通'
}

export const statementScopeLabel = (scope: string | null | undefined) =>
  scope ? SCOPE_LABELS[scope] || scope : '全账户'

/** 带符号的差额（+1,234.50 / -3），0 显示为 0；缺值为占位符 */
export function signedDelta(value: unknown, decimals = 2): string {
  if (value === null || value === undefined || value === '') return '—'
  const number = Number(value)
  if (Number.isNaN(number)) return '—'
  const body = Math.abs(number).toLocaleString('zh-CN', {
    minimumFractionDigits: 0,
    maximumFractionDigits: decimals
  })
  if (body === '0') return '0'
  return `${number > 0 ? '+' : '-'}${body}`
}
// 与后端 MANUAL_MARKETS 对齐（快照持仓行与特例规则表单共用）；唯一权威在 utils/securities
export const marketOptions: readonly string[] = MARKETS

export const today = () => todayLocalISODate()
export const monthEnd = () => {
  const now = new Date()
  return formatLocalDate(new Date(now.getFullYear(), now.getMonth(), 0))
}

// 表单保存工厂：校验 → 创建/更新 → 按分支提示 → 关窗重载（快照另有专属校验，不并入）
export function makeSaver<TPayload>({
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
  buildPayload: () => TPayload
  update: (id: number, payload: TPayload) => Promise<unknown>
  create: (payload: TPayload) => Promise<unknown>
  messages: { updated: string; created: string; failure: string }
  reload: () => Promise<unknown>
}) {
  return async () => {
    if (dialog.saving) return
    if (!(await formRef.value?.validate().catch(() => false)) || dialog.saving) return
    dialog.saving = true
    try {
      const payload = buildPayload()
      if (dialog.id) await update(dialog.id, payload)
      else await create(payload)
      ElMessage.success(dialog.id ? messages.updated : messages.created)
      dialog.visible = false
      await reload()
    } catch (error) {
      showApiError(error, messages.failure)
    } finally {
      dialog.saving = false
    }
  }
}

/**
 * 「月末核对」汇总按账户计（#286：此前按快照条数「4/4」，与仪表盘「IBKR、HSBC 未对账」并排时
 * 容易误读成 4 个账户都核对通过）。每个启用账户看它**最近一个快照日**的全部快照（同日可能有
 * 东财普通股票/港股通两份范围），任一不是 MATCHED 即不算通过——与仪表盘徽标同一「最差胜出」规则。
 */
export function reconciledAccountSummary(
  accounts: Array<{ id: number; is_active?: boolean | null }>,
  snapshots: Array<{
    broker_account_id?: number | null
    snapshot_date?: string | null
    status?: string | null
  }>
): { matched: number; total: number } {
  const active = accounts.filter((account) => account.is_active !== false)
  let matched = 0
  for (const account of active) {
    const own = snapshots.filter((row) => row.broker_account_id === account.id && row.snapshot_date)
    if (!own.length) continue
    const latestDate = own
      .map((row) => row.snapshot_date as string)
      .sort()
      .at(-1)
    const latest = own.filter((row) => row.snapshot_date === latestDate)
    if (latest.every((row) => String(row.status).toUpperCase() === 'MATCHED')) matched += 1
  }
  return { matched, total: active.length }
}

/** 只收起本页长备注；完整正文仍在原位置由原生 disclosure 公开。 */
export const isLongNote = (value: string | null | undefined) => Boolean(value && value.length > 80)
export const renderNote = (value: string | null | undefined) =>
  isLongNote(value)
    ? h('details', { class: 'read-details' }, [h('summary', '查看完整备注'), h('p', value!)])
    : value || '—'
