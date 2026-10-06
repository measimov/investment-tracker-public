/**
 * 仪表盘的纯函数（#218）：卡片色调、区间损益「估算」判定、两路警告的合并去重。
 */

import type { PeriodPnlSummary } from '@/types'

export { cardTone } from '@/utils/helpers'

/**
 * 期间损益是否打「估算」：期初基准陈旧（status=estimated），或区间内有按估值
 * 补记的实物转入（estimated_inflow_events>0：成本未知的期初建仓按市价估流入）。
 */
export function periodIsEstimated(
  period: Pick<PeriodPnlSummary, 'status' | 'estimated_inflow_events'>
): boolean {
  if (period.status === 'unavailable') return false
  return period.status === 'estimated' || (period.estimated_inflow_events ?? 0) > 0
}

interface PositionKey {
  symbol: string
  market: string
}

/**
 * 仪表盘警告 = 组合快照 data_quality.warnings + 区间损益 data_quality.warnings。
 *
 * 两个端点各自对「缺价」报一次：快照列出 `symbol:market` 清单，区间损益的当日段
 * 再报「N 只持仓无可用价格，未计入市值」。同一批标的只留快照那条（清单完整、
 * 与统计页一致）；区间损益里缺价的标的若有快照没覆盖到的，才保留它那条。
 */
export function mergeDashboardWarnings(input: {
  snapshotWarnings?: string[] | null
  snapshotMissingKeys?: string[] | null
  periodWarnings?: string[] | null
  periodUnpriced?: PositionKey[] | null
}): string[] {
  const merged: string[] = []
  const push = (text: string) => {
    if (text && !merged.includes(text)) merged.push(text)
  }
  for (const text of input.snapshotWarnings || []) push(text)

  const missing = new Set(input.snapshotMissingKeys || [])
  const unpriced = input.periodUnpriced || []
  const unpricedCovered =
    unpriced.length > 0 && unpriced.every((p) => missing.has(`${p.symbol}:${p.market}`))
  for (const text of input.periodWarnings || []) {
    if (unpricedCovered && text.includes('无可用价格')) continue
    push(text)
  }
  return merged
}
