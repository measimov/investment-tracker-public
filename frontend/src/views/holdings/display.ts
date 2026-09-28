/**
 * 持仓页展示口径的纯函数（#217）：现价的行情日期/新鲜度、带「缺值沉底」的排序、
 * 分市场小计、AI 标签降噪。不依赖 Vue/Pinia，可直接单测。
 */

import { formatDate, formatDateTime } from '@/utils/helpers'
import { formatLocalDate, parseLocalDate } from '@/utils/dateRange'

/** 与后端 services/statistics/pricing.py 的 PRICE_STALE_DAYS 同口径：超过 7 天视为陈价 */
export const PRICE_STALE_DAYS = 7

export const MANUAL_PRICE_SOURCE = 'manual'

const SOURCE_LABELS: Record<string, string> = {
  manual: '手工录入',
  'tencent-quote': '腾讯行情',
  'xueqiu-quote': '雪球行情'
}

export function priceSourceLabel(source: string | null | undefined): string {
  if (!source) return '未知'
  if (SOURCE_LABELS[source]) return SOURCE_LABELS[source]
  if (source.startsWith('tushare-')) return `Tushare ${source.slice('tushare-'.length)}`
  return source
}

export interface PriceInput {
  price: number | null
  /** 行情所属交易日 YYYY-MM-DD（报价源给出）；null = 未知或手工价 */
  priceAsOf: string | null | undefined
  /** 写库时刻（带时区 ISO） */
  priceUpdatedAt: string | null | undefined
  priceSource: string | null | undefined
}

export interface PriceInfo {
  /** 现价下的日期行：「行情 2026/09/25」或「刷新于 2026/09/26」 */
  label: string
  stale: boolean
  manual: boolean
  /** 距今天数（按行情日，缺行情日按写库日的本地日期）；无从判断为 null */
  ageDays: number | null
  tooltip: string[]
}

/**
 * 现价的日期与新鲜度。有行情日优先显示「行情 …」（周六刷新到的是周五收盘）；
 * 没有（手工价/报价源不给日期/存量数据）退回写库时刻。陈价判据与后端一致：
 * 超过 PRICE_STALE_DAYS 天。
 */
export function describePrice(input: PriceInput, today: string): PriceInfo | null {
  if (input.price === null || !(input.price > 0)) return null
  const manual = input.priceSource === MANUAL_PRICE_SOURCE
  const asOf = input.priceAsOf || null
  const updatedAt = input.priceUpdatedAt || null

  let label = ''
  let basis: string | null = null
  if (asOf) {
    label = `行情 ${formatDate(parseLocalDate(asOf))}`
    basis = asOf.slice(0, 10)
  } else if (updatedAt) {
    label = `${manual ? '录入于' : '刷新于'} ${formatDate(updatedAt)}`
    basis = formatLocalDate(new Date(updatedAt))
  }

  const ageDays =
    basis === null
      ? null
      : Math.round((parseLocalDate(today).getTime() - parseLocalDate(basis).getTime()) / 86400000)
  const stale = ageDays === null || ageDays > PRICE_STALE_DAYS

  const tooltip = [
    asOf ? `行情日期 ${formatDate(parseLocalDate(asOf))}` : '行情日期未知',
    `来源 ${priceSourceLabel(input.priceSource)}`
  ]
  if (updatedAt) tooltip.push(`写入 ${formatDateTime(updatedAt)}`)
  if (stale) tooltip.push(`超过 ${PRICE_STALE_DAYS} 天未更新（陈价），市值与盈亏可能失真`)
  return { label, stale, manual, ageDays, tooltip }
}

export type SortOrder = 'ascending' | 'descending' | null

/** 按数值排序，缺值（null）无论升降序都沉底——el-table 的 sort-method 做不到这点 */
export function sortNullsLast<T>(
  rows: T[],
  valueOf: (row: T) => number | null,
  order: Exclude<SortOrder, null>
): T[] {
  const sign = order === 'ascending' ? 1 : -1
  return [...rows].sort((a, b) => {
    const va = valueOf(a)
    const vb = valueOf(b)
    if (va === null && vb === null) return 0
    if (va === null) return 1
    if (vb === null) return -1
    return (va - vb) * sign
  })
}

export interface MarketSubtotalInput {
  market: string
  /** 标的键 symbol:market（同一标的多账户行只计一只） */
  key: string
  /** 有价且能折人民币；否则不计入市值/盈亏（与汇总卡同口径） */
  priced: boolean
  valueCNY: number
  costCNY: number
}

export interface MarketSubtotal {
  market: string
  count: number
  pricedCount: number
  valueCNY: number
  profitCNY: number
  /** 占全部有价市值的百分比；总市值为 0 时 null */
  share: number | null
}

export function buildMarketSubtotals(items: MarketSubtotalInput[]): MarketSubtotal[] {
  const groups = new Map<
    string,
    { keys: Set<string>; priced: Set<string>; value: number; cost: number }
  >()
  for (const item of items) {
    const group = groups.get(item.market) ?? {
      keys: new Set<string>(),
      priced: new Set<string>(),
      value: 0,
      cost: 0
    }
    group.keys.add(item.key)
    if (item.priced) {
      group.priced.add(item.key)
      group.value += item.valueCNY
      group.cost += item.costCNY
    }
    groups.set(item.market, group)
  }
  const total = [...groups.values()].reduce((sum, group) => sum + group.value, 0)
  return [...groups.entries()]
    .map(([market, group]) => ({
      market,
      count: group.keys.size,
      pricedCount: group.priced.size,
      valueCNY: group.value,
      profitCNY: group.value - group.cost,
      share: total ? (group.value / total) * 100 : null
    }))
    .sort((a, b) => b.valueCNY - a.valueCNY || a.market.localeCompare(b.market))
}

/**
 * 表格里不展示的 AI 标签：「数据不足」是数据面状态而不是观点（ETF 几乎必带），
 * 占着唯一的标签位只是噪音；详情页仍完整展示。
 */
const NOISE_TAGS = new Set(['数据不足', 'ETF'])

export function displayAnalysisTag(tags: string[] | null | undefined): string | null {
  return (tags || []).find((tag) => !NOISE_TAGS.has(tag)) ?? null
}

/** 行业来源（后端 GET /securities/industries 的 source）→ 提示文案：手工 / 官方 / 东方财富 */
const INDUSTRY_SOURCE_LABELS: Record<string, string> = {
  rule: '手工（特例规则）',
  tushare: '官方 · Tushare 行业分类',
  edgar: '官方 · SEC EDGAR 行业代码（SIC）',
  eastmoney: '东方财富 F10（非官方，补缺）'
}

export function industrySourceLabel(source: string | null | undefined): string {
  if (!source) return '未知'
  return INDUSTRY_SOURCE_LABELS[source] ?? source
}

/** 行业的悬浮提示：来源 + 如何覆盖 */
export function industryTooltip(source: string | null | undefined): string {
  const origin = `行业来源：${industrySourceLabel(source)}`
  return source === 'rule' ? origin : `${origin}。可在「账户数据 → 特例规则」用「行业分类」覆盖`
}
