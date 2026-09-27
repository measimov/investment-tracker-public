/**
 * 跨页面共用的枚举文案（#219/#220）：交易类型、公司行动类型、现金事件类型与
 * 「没有归属账户」的叫法此前各页各写一份，已经漂移（待分配/未分配/未关联账户/
 * 未指定…；「股票股息/红股」vs「股票股息」）。选项列表一律从这里的映射生成。
 */

export type TagKind = 'success' | 'warning' | 'info' | 'primary' | 'danger'

/** 没有归属账户的持仓/交易/行动（后端 broker_account_id IS NULL）——与后端文案一致 */
export const UNASSIGNED_ACCOUNT_LABEL = '未指定账户'
/** 引用了已删除的券商账户 */
export const DELETED_ACCOUNT_LABEL = '已删除账户'

// 证券转仓（TRANSFER_OUT/IN）与现金侧的资金划转是两回事，文案必须能区分
export const TRANSACTION_TYPE_LABELS: Record<string, string> = {
  BUY: '买入',
  SELL: '卖出',
  TRANSFER_OUT: '转仓转出',
  TRANSFER_IN: '转仓转入'
}

export const TRANSACTION_TYPE_TAGS: Record<string, TagKind> = {
  BUY: 'success',
  SELL: 'danger',
  TRANSFER_OUT: 'warning',
  TRANSFER_IN: 'info'
}

export function transactionTypeLabel(type: string): string {
  return TRANSACTION_TYPE_LABELS[type] || type
}

export function transactionTypeTag(type: string): TagKind {
  return TRANSACTION_TYPE_TAGS[type] || 'info'
}

export const ACTION_TYPE_LABELS: Record<string, string> = {
  CASH_DIVIDEND: '现金股息',
  STOCK_DIVIDEND: '股票股息',
  RIGHTS_ISSUE: '配股',
  STOCK_SPLIT: '拆股',
  REVERSE_SPLIT: '合股',
  BONUS_ISSUE: '送股',
  OPENING_POSITION: '期初建仓/转托管转入'
}

export const ACTION_TYPE_TAGS: Record<string, TagKind> = {
  CASH_DIVIDEND: 'success',
  STOCK_DIVIDEND: 'warning',
  RIGHTS_ISSUE: 'info',
  STOCK_SPLIT: 'primary',
  REVERSE_SPLIT: 'primary',
  BONUS_ISSUE: 'warning',
  OPENING_POSITION: 'info'
}

export const CASH_EVENT_TYPE_LABELS: Record<string, string> = {
  DEPOSIT: '入金',
  WITHDRAWAL: '出金',
  INTEREST: '利息',
  FEE: '费用',
  TAX: '税费',
  TRANSFER_IN: '资金转入',
  TRANSFER_OUT: '资金转出',
  FX_IN: '换汇转入',
  FX_OUT: '换汇转出',
  OTHER: '其他'
}

export function cashEventTypeLabel(type: string | null | undefined): string {
  if (!type) return '—'
  return CASH_EVENT_TYPE_LABELS[type] || type
}

/** 映射 → el-option 列表（保持声明顺序）；exclude 用于 CMB 规则这类子集 */
export function optionsOf(
  labels: Record<string, string>,
  exclude: readonly string[] = []
): { label: string; value: string }[] {
  return Object.entries(labels)
    .filter(([value]) => !exclude.includes(value))
    .map(([value, label]) => ({ label, value }))
}
