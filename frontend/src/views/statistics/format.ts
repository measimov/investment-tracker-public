/**
 * 统计页的展示小工具（#218，纯函数）。
 *
 * 正号规则全站一条：有方向的量（收益率、超额收益、年化）带 +，与 helpers.formatPercent
 * 一致；无方向的比例（胜率、占比、回撤幅度）不带 +。缺值一律 EMPTY。
 */

import { EMPTY, formatNumber, formatPercent } from '@/utils/helpers'
import { parseLocalDate } from '@/utils/dateRange'
import type { RiskFreeInfo } from './types'

type Numeric = number | string | null | undefined

/** 曲线缺值保留断点；非法或非有限值也不能成为图上的零收益。 */
export function nullableFiniteNumber(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null
  if (typeof value === 'string' && !value.trim()) return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

export function formatNullableNumber(value: Numeric, precision = 2): string {
  return formatNumber(value, precision)
}

/** 有方向的百分比：+12.34% / -5.00% / — */
export function formatNullablePercent(value: Numeric, precision = 2): string {
  return formatPercent(value, precision)
}

export { formatPlainPercent } from '@/utils/helpers'

/** el-statistic 的 formatter：带正号的数值（后缀 % 由组件加），规则同 formatPercent */
export function signedNumber(value: Numeric, precision = 2): string {
  const text = formatNumber(value, precision)
  if (text === EMPTY) return EMPTY
  if (Number(Number(value).toFixed(precision)) === 0) return formatNumber(0, precision)
  return Number(value) > 0 ? `+${text}` : text
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

/**
 * 夏普/索提诺的无风险利率说明（#200）：默认按 SHIBOR 3M 日序列逐期扣除，
 * 序列尚未同步时后端按 0 计算并给 note——两种情况都要写明，不能笼统说「按 0」。
 */
export function riskFreeText(info: RiskFreeInfo | null | undefined): string {
  if (!info || info.basis === 'none') {
    return info?.note || '无风险利率按 0 计算（未扣除存款/国债收益）'
  }
  const average = formatNumber(info.average, 2)
  if (info.basis === 'constant') return `无风险利率按请求指定的年化 ${average}% 计算`
  const partial = info.missing_points
    ? `；区间开头 ${info.missing_points} 个观测点早于序列首值，按 0 计`
    : ''
  return `无风险利率按 ${info.label || info.series} 日序列逐期扣除，区间均值 ${average}%${partial}`
}

/** 仅压缩图表刻度；tooltip 与明细仍用完整金额格式。 */
export function formatAmountTick(value: number): string {
  const magnitude = Math.abs(value)
  if (magnitude >= 100000000) return `${formatNumber(value / 100000000, 1).replace(/\.0$/, '')}亿`
  if (magnitude >= 10000) return `${formatNumber(value / 10000, 1).replace(/\.0$/, '')}万`
  return formatNumber(value, 0)
}
