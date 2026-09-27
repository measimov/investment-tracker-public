/**
 * 统计页的展示小工具（#218，纯函数）。
 *
 * 正号规则全站一条：有方向的量（收益率、超额收益、年化）带 +，与 helpers.formatPercent
 * 一致；无方向的比例（胜率、占比、回撤幅度）不带 +。缺值一律 EMPTY。
 */

import { EMPTY, formatNumber, formatPercent } from '@/utils/helpers'
import { parseLocalDate } from '@/utils/dateRange'

type Numeric = number | string | null | undefined

export function formatNullableNumber(value: Numeric, precision = 2): string {
  return formatNumber(value, precision)
}

/** 有方向的百分比：+12.34% / -5.00% / — */
export function formatNullablePercent(value: Numeric, precision = 2): string {
  return formatPercent(value, precision)
}

/** 无方向的百分比（胜率、占比、回撤）：12.34% / — */
export function formatPlainPercent(value: Numeric, precision = 2): string {
  const text = formatNumber(value, precision)
  return text === EMPTY ? EMPTY : `${text}%`
}

/** el-statistic 的 formatter：带正号的数值（后缀 % 由组件加），规则同 formatPercent */
export function signedNumber(value: Numeric, precision = 2): string {
  const text = formatNumber(value, precision)
  if (text === EMPTY) return EMPTY
  return Number(value) >= 0 ? `+${text}` : text
}

// 年化在短区间严重失真：近 1 月涨 5% 年化约 +79%。不改后端数值，只在前端标注
export const SHORT_RANGE_DAYS = 180

export function rangeSpanDays(
  start: string | null | undefined,
  end: string | null | undefined
): number | null {
  if (!start || !end) return null
  const diff = parseLocalDate(end).getTime() - parseLocalDate(start).getTime()
  if (Number.isNaN(diff)) return null
  return Math.round(diff / 86400000)
}

export function isShortRange(spanDays: number | null | undefined): boolean {
  return spanDays !== null && spanDays !== undefined && spanDays >= 0 && spanDays < SHORT_RANGE_DAYS
}
