/**
 * 公司行动页两个 tab 共用的小件（issue #140）。账户显示名统一在 utils/labels（#284）。
 * 行动类型文案/tag 直接用 `utils/labels` 的 actionTypeLabel / actionTypeTag。
 */

import type { CorporateActionCreate } from '@/types'

/** 说明自动核对为何停下，不把所有问题都称为“权益待核对”。 */
export function dividendReviewReason(reason: string | null | undefined): string {
  const descriptions: Record<string, string> = {
    currency_unverified: '公告的实际派息币种待核实，暂不能判断是否收齐。',
    receipt_currency_mismatch: '公告与到账币种不同，对账单缺少可核对的原币金额或换汇依据。',
    ambiguous_receipt: '同一到账记录可能对应多份公告，需要确认属于哪一次派息。',
    receipt_period_unverified: '历史到账关联与本次派息日期不符，请重新核对所属期次。',
    entitlement_unverified: '账户或权益日持股数量不明确，暂不能核对全部应收金额。',
    announced_amount_unknown: '公告的应收总额尚不明确，暂不能判断是否收齐。',
    statement_evidence_missing: '已有到账记录，但缺少可验证的对账单明细；可凭实际收款人工确认。',
    receipt_amount_unresolved: '对账单金额与公告应收总额尚未对齐，请核对税费、分次到账或漏记。',
    manual_review_pending: '此前已人工核对关联，并保留为未收齐；系统不会覆盖这一判断。'
  }
  return reason ? descriptions[reason] || '到账依据尚待核对，暂不能自动确认收齐。' : ''
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

/** EF003（可選擇以股份代替）的代息股份信息，后端 parse_scrip_option */
interface HkScripOption {
  default_option?: string | null
  default_cash?: boolean | null
  price?: { amount: string; currency: string } | null
  election_deadline?: string | null
}

/** EF002（可選擇貨幣）的可选币种，后端 parse_currency_options */
interface HkCurrencyOptions {
  options?: {
    currency?: string | null
    amount?: string | null
    exchange_rate?: { from: string; to: string; rate: string } | null
    pending?: boolean
  }[]
  election_deadline?: string | null
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
  scrip?: HkScripOption | null
  currency_options?: HkCurrencyOptions | null
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
  for (const c of components) {
    const scrip = scripNote(c.scrip)
    if (scrip) lines.push(scrip)
    const currency = currencyOptionsNote(c.currency_options)
    if (currency) lines.push(currency)
  }
  return lines
}

function deadlineText(deadline: string | null | undefined): string {
  return deadline ? `，选择截止 ${deadline}` : ''
}

function scripNote(scrip: HkScripOption | null | undefined): string | null {
  if (!scrip) return null
  const price = scrip.price ? `代息股份价格 ${scrip.price.amount} ${scrip.price.currency}` : ''
  if (scrip.default_cash === false) {
    // 金额仍按现金口径展示，但不作选择的股东收到的是新股
    return `预设选项为「${scrip.default_option}」：不作选择将收到代息股份${price ? `（${price}）` : ''}${deadlineText(scrip.election_deadline)}`
  }
  if (!price && !scrip.election_deadline) return null
  return `以股代息：${price || '代息股份价格未公布'}${deadlineText(scrip.election_deadline)}`
}

function currencyOptionsNote(options: HkCurrencyOptions | null | undefined): string | null {
  const items = (options?.options || []).map((o) => {
    if (!o.amount) return `${o.currency ?? '其他币种'} 金额有待公布`
    const rate = o.exchange_rate
      ? `，1 ${o.exchange_rate.from} = ${o.exchange_rate.rate} ${o.exchange_rate.to}`
      : ''
    return `每股 ${o.amount} ${o.currency ?? ''}${rate}`.trim()
  })
  if (!items.length) return null
  return `可选币种：${items.join('；')}${deadlineText(options?.election_deadline)}`
}

// ---------------------------------------------------------------------------- 新增/编辑表单
// 行 ↔ 表单 ↔ 请求体的映射是纯函数（#284：此前内联在 RecordsTab 的 handleEdit/handleSubmit 里，
// 税率 ×100、编辑时税额原样提交这类语义没有测试）。

export interface CorporateActionForm {
  id?: number
  broker_account_id: number | null
  symbol: string
  name: string
  market: string
  action_type: string
  ex_date: string
  payment_date: string
  receipt_confirmed: boolean
  amount_basis: 'GROSS_NET' | 'NET_ONLY'
  net_dividend: number | null
  dividend_per_share: number | null
  total_dividend: number | null
  /** 实际预扣税额（统计与对账只读税额）；税率只是辅助，可按「总额×税率」填入税额 */
  tax_withheld: number | null
  /** 界面上的税率百分数（10 = 10%），提交时换成小数 */
  tax_rate_percent: number | null
  shares_received: number | null
  distribution_ratio: string
  subscription_price: number | null
  subscription_quantity: number | null
  split_ratio: string
  /** 期初建仓（#174）：数量必填，两个成本可选，都空 = 成本未知 */
  opening_quantity: number | null
  opening_cost_per_share: number | null
  opening_total_cost: number | null
  currency: string
  notes: string
}

/** 换类型时清空的类型专属字段 */
export const TYPE_SPECIFIC_FIELDS_EMPTY = {
  payment_date: '',
  receipt_confirmed: false,
  amount_basis: 'GROSS_NET' as const,
  net_dividend: null,
  dividend_per_share: null,
  total_dividend: null,
  tax_withheld: null,
  tax_rate_percent: null,
  shares_received: null,
  distribution_ratio: '',
  subscription_price: null,
  subscription_quantity: null,
  split_ratio: '',
  opening_quantity: null,
  opening_cost_per_share: null,
  opening_total_cost: null
} satisfies Partial<CorporateActionForm>

export function emptyActionForm(): CorporateActionForm {
  return {
    broker_account_id: null,
    symbol: '',
    name: '',
    market: '',
    action_type: '',
    ex_date: '',
    ...TYPE_SPECIFIC_FIELDS_EMPTY,
    currency: 'CNY',
    notes: ''
  }
}

type Numeric = number | string | null | undefined
const numberOrNull = (value: Numeric) => (value ? Number(value) : null)

/** 编辑回填：Decimal 串转数字；税率小数 → 百分数（保留两位），税率为 null 不回填
 *  （此前回填 10%，保存会悄悄写入 0.1）。 */
export function formFromAction(row: {
  id: number
  broker_account_id?: number | null
  symbol: string
  name?: string | null
  market: string
  action_type: string
  ex_date: string
  payment_date?: string | null
  amount_basis?: string
  net_dividend?: Numeric
  dividend_per_share?: Numeric
  total_dividend?: Numeric
  tax_withheld?: Numeric
  tax_rate?: Numeric
  shares_received?: Numeric
  distribution_ratio?: string | null
  subscription_price?: Numeric
  subscription_quantity?: Numeric
  split_ratio?: string | null
  adjusted_quantity?: Numeric
  adjusted_cost_per_share?: Numeric
  cost_basis_adjustment?: Numeric
  currency?: string | null
  notes?: string | null
}): CorporateActionForm {
  return {
    id: row.id,
    broker_account_id: row.broker_account_id || null,
    symbol: row.symbol,
    name: row.name || '',
    market: row.market,
    action_type: row.action_type,
    ex_date: row.ex_date,
    payment_date: row.payment_date || '',
    receipt_confirmed: false,
    amount_basis: row.amount_basis === 'NET_ONLY' ? 'NET_ONLY' : 'GROSS_NET',
    net_dividend: row.net_dividend == null ? null : Number(row.net_dividend),
    dividend_per_share: numberOrNull(row.dividend_per_share),
    total_dividend: numberOrNull(row.total_dividend),
    tax_withheld:
      row.tax_withheld !== null && row.tax_withheld !== undefined ? Number(row.tax_withheld) : null,
    tax_rate_percent:
      row.tax_rate !== null && row.tax_rate !== undefined
        ? Math.round(Number(row.tax_rate) * 10000) / 100
        : null,
    shares_received: numberOrNull(row.shares_received),
    distribution_ratio: row.distribution_ratio || '',
    subscription_price: numberOrNull(row.subscription_price),
    subscription_quantity: numberOrNull(row.subscription_quantity),
    split_ratio: row.split_ratio || '',
    opening_quantity: numberOrNull(row.adjusted_quantity),
    opening_cost_per_share: numberOrNull(row.adjusted_cost_per_share),
    opening_total_cost: numberOrNull(row.cost_basis_adjustment),
    currency: row.currency || 'CNY',
    notes: row.notes || ''
  }
}

/** 表单 → 请求体：通用字段 + 该类型自己的字段。 */
export function payloadFromForm(
  form: CorporateActionForm,
  _options: { isEdit: boolean }
): CorporateActionCreate {
  const payload: Record<string, unknown> = {
    broker_account_id: form.broker_account_id || null,
    symbol: form.symbol,
    name: form.name,
    market: form.market,
    action_type: form.action_type,
    ex_date: form.ex_date,
    currency: form.currency,
    notes: form.notes
  }
  switch (form.action_type) {
    case 'CASH_DIVIDEND':
      payload.payment_date = form.payment_date || null
      payload.receipt_confirmed = form.receipt_confirmed
      payload.amount_basis = form.amount_basis
      payload.net_dividend = form.amount_basis === 'NET_ONLY' ? form.net_dividend : null
      payload.dividend_per_share = form.dividend_per_share
      payload.total_dividend = form.total_dividend
      // 未知税额保持为空，明确免税才提交 0。
      payload.tax_withheld = form.tax_withheld
      payload.tax_rate =
        form.tax_rate_percent === null ? null : Math.round(form.tax_rate_percent * 100) / 10000
      break
    case 'STOCK_DIVIDEND':
    case 'BONUS_ISSUE':
      payload.shares_received = form.shares_received
      payload.distribution_ratio = form.distribution_ratio
      break
    case 'RIGHTS_ISSUE':
      payload.subscription_price = form.subscription_price
      payload.subscription_quantity = form.subscription_quantity
      payload.distribution_ratio = form.distribution_ratio
      break
    case 'STOCK_SPLIT':
    case 'REVERSE_SPLIT':
      payload.split_ratio = form.split_ratio
      break
    case 'OPENING_POSITION':
      payload.adjusted_quantity = form.opening_quantity
      payload.adjusted_cost_per_share = form.opening_cost_per_share
      payload.cost_basis_adjustment = form.opening_total_cost
      break
  }
  // 表单校验（按类型动态必填）保证 action_type 等必填项：已校验表单 → 请求体的唯一断言点
  return payload as CorporateActionCreate
}
