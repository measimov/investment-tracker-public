/**
 * 观察清单「现价」与「加入以来」两列的展示口径（纯函数，可直接单测）。
 *
 * 加入以来涨跌幅 = 现价 / 基准价 − 1，由后端算好（change_since_added_pct，小数）；
 * 同币种价格比、不含分红。基准价口径见 BASIS_LABELS。
 */

import { formatDate, formatPercent, formatPrice } from '@/utils/helpers'
import { parseLocalDate } from '@/utils/dateRange'

export const ADDED_PRICE_BASIS_LABELS: Record<string, string> = {
  quote: '加入时报价',
  close_on_add: '加入日收盘',
  close_after_add: '加入后首个收盘（加入日前后没有行情）',
  first_quote: '加入后首次报价'
}

export interface ChangeSinceAddedInput {
  change_since_added_pct?: number | null
  added_price?: string | number | null
  added_price_date?: string | null
  added_price_basis?: string | null
  current_price?: string | number | null
}

export interface ChangeSinceAddedInfo {
  /** 百分比文案，如 +5.20% */
  text: string
  /** 涨跌方向：up / down / flat（着色用） */
  direction: 'up' | 'down' | 'flat'
  tooltip: string[]
}

export function addedPriceBasisLabel(basis: string | null | undefined): string {
  if (!basis) return '未知口径'
  return ADDED_PRICE_BASIS_LABELS[basis] ?? basis
}

/** 缺现价或基准价时返回 null（页面显示「—」并提示原因） */
export function describeChangeSinceAdded(item: ChangeSinceAddedInput): ChangeSinceAddedInfo | null {
  const pct = item.change_since_added_pct
  if (pct === null || pct === undefined || !Number.isFinite(pct)) return null
  const direction = pct > 0 ? 'up' : pct < 0 ? 'down' : 'flat'
  const basisDate = item.added_price_date
    ? formatDate(parseLocalDate(item.added_price_date))
    : '日期未知'
  return {
    text: formatPercent(pct * 100),
    direction,
    tooltip: [
      `基准价 ${formatPrice(item.added_price)}（${addedPriceBasisLabel(item.added_price_basis)}，${basisDate}）`,
      `现价 ${formatPrice(item.current_price)}`,
      '同币种价格涨跌，不含分红'
    ]
  }
}

/** 没有涨跌幅时的说明：还没取到现价 / 还没有基准价 */
export function missingChangeReason(item: ChangeSinceAddedInput): string {
  if (item.current_price === null || item.current_price === undefined) {
    return '尚未取到现价：交易时段内每 15 分钟自动刷新，也可在持仓页手动刷新价格'
  }
  if (item.added_price_basis === 'pending_quote') {
    return '尚无基准价：加入日附近没有历史行情，下一次成功刷新的报价将作为基准'
  }
  if (item.added_price_basis === null || item.added_price_basis === undefined) {
    return '尚无基准价：正在按加入日收盘补齐（日线同步每小时一次），或等下一次成功刷新'
  }
  return '尚无基准价：下一次成功刷新后补上'
}
