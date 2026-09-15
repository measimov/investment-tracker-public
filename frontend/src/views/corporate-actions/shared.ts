/**
 * 公司行动页两个 tab 共用的小件（issue #140）：行动类型文案/tag 映射与
 * 券商账户标签。类型映射同时服务记录表、移动卡片与分红建议表。
 */

import type { BrokerAccount } from '@/types'

export const actionTypeNames: Record<string, string> = {
  CASH_DIVIDEND: '现金股息',
  STOCK_DIVIDEND: '股票股息',
  RIGHTS_ISSUE: '配股',
  STOCK_SPLIT: '拆股',
  REVERSE_SPLIT: '合股',
  BONUS_ISSUE: '送股'
}

export type ElTagType = 'success' | 'warning' | 'info' | 'primary' | 'danger'

export const actionTypeTags: Record<string, ElTagType> = {
  CASH_DIVIDEND: 'success',
  STOCK_DIVIDEND: 'warning',
  RIGHTS_ISSUE: 'info',
  STOCK_SPLIT: 'primary',
  REVERSE_SPLIT: 'primary',
  BONUS_ISSUE: 'warning'
}

export function getActionTypeName(type: string) {
  return actionTypeNames[type] || type
}

export function getActionTypeTag(type: string): ElTagType | undefined {
  // 兜底 undefined = el-tag 默认样式（与此前 '' 的呈现一致，且类型合法）
  return actionTypeTags[type]
}

export function brokerAccountLabel(account: BrokerAccount) {
  const suffix = account.account_number_masked ? ` · ${account.account_number_masked}` : ''
  return `${account.account_name}${suffix}`
}

export function brokerAccountLabelById(
  accounts: BrokerAccount[],
  accountId: number | null | undefined
) {
  if (!accountId) return '未分配'
  const account = accounts.find((item) => item.id === accountId)
  return account ? brokerAccountLabel(account) : '已删除账户'
}
