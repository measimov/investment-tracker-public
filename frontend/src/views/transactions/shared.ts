/**
 * 交易页拆分（issue #140）共用小件：交易类型文案/tag 映射与券商账户标签。
 * 账户标签格式是本页口径（名称 · 券商 · 尾号，null 显示「未指定账户」），与
 * 公司行动页的 shared 刻意不合并——两页的文案与空值语义不同。
 */

import type { BrokerAccount } from '@/types'
import { DELETED_ACCOUNT_LABEL, UNASSIGNED_ACCOUNT_LABEL } from '@/utils/labels'
import type { Transaction } from '@/stores/transactions'

export {
  TRANSACTION_TYPE_LABELS as TYPE_LABELS,
  TRANSACTION_TYPE_TAGS as TYPE_TAG_KINDS
} from '@/utils/labels'

export function isTransfer(row: Transaction) {
  return row.transaction_type === 'TRANSFER_OUT' || row.transaction_type === 'TRANSFER_IN'
}

export {
  transactionTypeLabel as typeLabel,
  transactionTypeTag as typeTagKind
} from '@/utils/labels'

export const brokerAccountLabel = (account: BrokerAccount) =>
  [account.account_name, account.broker, account.account_number_masked].filter(Boolean).join(' · ')

export const brokerAccountLabelById = (
  accounts: BrokerAccount[],
  id: number | null | undefined
) => {
  if (id == null) return UNASSIGNED_ACCOUNT_LABEL
  const account = accounts.find((item) => String(item.id) === String(id))
  return account ? brokerAccountLabel(account) : DELETED_ACCOUNT_LABEL
}
