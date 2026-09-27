/**
 * 公司行动页两个 tab 共用的小件（issue #140）：行动类型文案/tag 映射与
 * 券商账户标签。类型映射同时服务记录表、移动卡片与分红建议表。
 */

import type { BrokerAccount } from '@/types'

import {
  ACTION_TYPE_LABELS,
  ACTION_TYPE_TAGS,
  DELETED_ACCOUNT_LABEL,
  UNASSIGNED_ACCOUNT_LABEL,
  type TagKind
} from '@/utils/labels'

export const actionTypeNames = ACTION_TYPE_LABELS
export type ElTagType = TagKind
export const actionTypeTags = ACTION_TYPE_TAGS

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
  if (!accountId) return UNASSIGNED_ACCOUNT_LABEL
  const account = accounts.find((item) => item.id === accountId)
  return account ? brokerAccountLabel(account) : DELETED_ACCOUNT_LABEL
}

/** 期初建仓的成本状态：两个成本字段都空 = 成本未知（派生状态，与后端一致） */
export function openingPositionCostKnown(row: {
  adjusted_cost_per_share?: unknown
  cost_basis_adjustment?: unknown
}): boolean {
  const has = (value: unknown) => value !== null && value !== undefined && value !== ''
  return has(row.adjusted_cost_per_share) || has(row.cost_basis_adjustment)
}

type Amount = number | string | null | undefined

const present = (value: Amount) => value !== null && value !== undefined && value !== ''

/**
 * 现金股息金额归一：与后端 `semantics.cash_dividend_amounts` 同一口径——
 * 显式 net_dividend（含 0）优先，否则 gross − tax；缺失的总额/税额按 0。
 */
export function cashDividendAmounts(row: {
  total_dividend?: Amount
  tax_withheld?: Amount
  net_dividend?: Amount
}): { gross: number; tax: number; net: number } {
  const gross = present(row.total_dividend) ? Number(row.total_dividend) : 0
  const tax = present(row.tax_withheld) ? Number(row.tax_withheld) : 0
  const net = present(row.net_dividend) ? Number(row.net_dividend) : gross - tax
  return { gross, tax, net }
}

/**
 * 按税率辅助算预扣税额（表单用）：总额 × 税率%，保留 2 位。
 * 任一缺失返回 null（不改用户已填的税额）。与后端「只给税率时推导税额」同一算式。
 */
export function taxFromRate(
  total: number | null | undefined,
  ratePercent: number | null | undefined
): number | null {
  if (total === null || total === undefined || ratePercent === null || ratePercent === undefined)
    return null
  if (Number.isNaN(total) || Number.isNaN(ratePercent)) return null
  return Math.round(total * ratePercent) / 100
}
