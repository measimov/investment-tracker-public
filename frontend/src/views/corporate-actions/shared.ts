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

/** 分红建议来源 → 短标签（后端 corporate_action_suggestions.source） */
export function suggestionSourceLabel(source: string | null | undefined): string {
  if (source === 'hkexnews-dividend') return '披露易'
  if (source === 'tushare-dividend') return 'Tushare'
  return source || ''
}

interface HkWithholding {
  applicable?: boolean | null
  rates_percent?: string[]
  non_resident_enterprise_percent?: string | null
  southbound_individual_percent?: string | null
}

interface HkDividendComponent {
  dividend_type?: string | null
  dividend_nature?: string | null
  amount?: string | null
  currency?: string | null
  declared_amount?: string | null
  declared_currency?: string | null
  exchange_rate?: { from: string; to: string; rate: string } | null
  status?: string | null
  announcement_date?: string | null
  withholding?: HkWithholding | null
}

/** 港股（披露易）建议的 announcement_detail 形状（后端 group_hk_dividends_by_ex_date） */
export interface HkAnnouncementDetail {
  source?: string
  components?: HkDividendComponent[]
  scrip_option?: boolean
  currency_election?: boolean
  withholding_applicable?: boolean | null
}

function withholdingNote(
  components: HkDividendComponent[],
  applicable: boolean | null | undefined
): string | null {
  if (applicable === false) {
    return '公告：发行人不代扣所得税（港股通等渠道仍可能由结算机构代扣，以到账为准）'
  }
  const first = components.find((c) => c.withholding?.applicable)?.withholding
  if (!first) return null
  const parts: string[] = []
  if (first.non_resident_enterprise_percent) {
    parts.push(`非居民企业（含 HKSCC 代理人）${first.non_resident_enterprise_percent}%`)
  }
  if (first.southbound_individual_percent) {
    parts.push(`港股通个人 ${first.southbound_individual_percent}%`)
  }
  if (!parts.length && first.rates_percent?.length) {
    parts.push(`税率 ${first.rates_percent.map((r) => `${r}%`).join('/')}`)
  }
  return parts.length
    ? `公告代扣所得税：${parts.join('、')}（按持有渠道不同，以实际到账为准）`
    : '公告有代扣所得税说明（按持有渠道不同，以实际到账为准）'
}

/**
 * 港股建议的说明行（tooltip 用）：每笔股息的类型/金额/宣派币种与汇率、代扣税、
 * 以股代息与币种选择提示。非披露易建议返回空数组。
 */
export function hkDividendNotes(detail: HkAnnouncementDetail | null | undefined): string[] {
  if (!detail || detail.source !== 'hkexnews') return []
  const components = detail.components || []
  const lines = components.map((c) => {
    const kind = [c.dividend_type, c.dividend_nature].filter(Boolean).join('·')
    let line = `${kind} 每股 ${c.amount ?? '—'} ${c.currency ?? ''}`.trim()
    if (c.declared_currency && c.declared_currency !== c.currency) {
      const rate = c.exchange_rate
        ? `，1 ${c.exchange_rate.from} = ${c.exchange_rate.rate} ${c.exchange_rate.to}`
        : ''
      line += `（宣派 ${c.declared_amount} ${c.declared_currency}${rate}）`
    }
    if (c.status && c.status !== '新公告') line += ` · ${c.status}`
    return line
  })
  const tax = withholdingNote(components, detail.withholding_applicable)
  if (tax) lines.push(tax)
  if (detail.scrip_option) lines.push('可选以股代息：若选择以股代息，实际不收现金')
  if (detail.currency_election) lines.push('可选择派发币种：实际到账币种可能与此不同')
  return lines
}
