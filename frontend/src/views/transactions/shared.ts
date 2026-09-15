/**
 * 交易页拆分（issue #140）共用小件：交易类型文案/tag 映射与券商账户标签。
 * 账户标签格式是本页口径（名称 · 券商 · 尾号，null 显示"待分配"），与
 * 公司行动页的 shared 刻意不合并——两页的文案与空值语义不同。
 */

import type { BrokerAccount } from '@/types'
import type { Transaction } from '@/stores/transactions'

export const TYPE_LABELS: Record<string, string> = {
  BUY: '买入',
  SELL: '卖出',
  TRANSFER_OUT: '转出',
  TRANSFER_IN: '转入'
}

export const TYPE_TAG_KINDS: Record<string, 'success' | 'danger' | 'warning' | 'info'> = {
  BUY: 'success',
  SELL: 'danger',
  TRANSFER_OUT: 'warning',
  TRANSFER_IN: 'info'
}

export function isTransfer(row: Transaction) {
  return row.transaction_type === 'TRANSFER_OUT' || row.transaction_type === 'TRANSFER_IN'
}

export function typeLabel(type: string) {
  return TYPE_LABELS[type] || type
}

export function typeTagKind(type: string) {
  return TYPE_TAG_KINDS[type] || 'info'
}

export const brokerAccountLabel = (account: BrokerAccount) =>
  [account.account_name, account.broker, account.account_number_masked].filter(Boolean).join(' · ')

export const brokerAccountLabelById = (
  accounts: BrokerAccount[],
  id: number | null | undefined
) => {
  if (id == null) return '待分配'
  const account = accounts.find((item) => String(item.id) === String(id))
  return account ? brokerAccountLabel(account) : '已删除账户'
}
