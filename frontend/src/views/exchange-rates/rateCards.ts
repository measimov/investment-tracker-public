/**
 * 汇率页「当前汇率」卡片的纯函数（#220）：逐币种的生效日期/来源与过期判定。
 *
 * 此前所有卡片共用全表最新一条的日期与来源——SGD 三个月没更新也显示「今天 · api」。
 * 后端 `/exchange-rates/latest` 现在按币种返回 `details`；旧后端没有该字段时
 * 退回顶层日期/来源（兼容），但不做过期判定以免误报。
 */

import type { ExchangeRateLatest } from '@/types'
import { parseLocalDate } from '@/utils/dateRange'

/** 超过这么多天没更新即标黄「过期」（与持仓价格 PRICE_STALE_DAYS 同口径） */
export const RATE_STALE_DAYS = 7

export interface RateCard {
  currency: string
  rate: number
  effectiveDate: string | null
  source: string | null
  /** 距今天数；无日期时为 null */
  ageDays: number | null
  stale: boolean
}

const DAY_MS = 24 * 60 * 60 * 1000

export function rateAgeDays(
  effectiveDate: string | null | undefined,
  today: string
): number | null {
  if (!effectiveDate) return null
  const diff = parseLocalDate(today).getTime() - parseLocalDate(effectiveDate).getTime()
  // 夏令时切换日的 23/25 小时用 round 吸收
  return Math.round(diff / DAY_MS)
}

export function buildRateCards(
  latest: Pick<ExchangeRateLatest, 'rates' | 'base_currency' | 'effective_date' | 'source'> & {
    details?: ExchangeRateLatest['details']
  },
  today: string,
  staleDays = RATE_STALE_DAYS
): RateCard[] {
  const base = latest.base_currency || 'CNY'
  const details = latest.details || {}
  const hasDetails = Object.keys(details).length > 0
  return Object.entries(latest.rates || {})
    .filter(([currency]) => currency !== base)
    .map(([currency, rate]) => {
      const detail = details[currency]
      const effectiveDate = detail?.effective_date ?? (hasDetails ? null : latest.effective_date)
      const source = detail?.source ?? (hasDetails ? null : latest.source)
      const ageDays = detail ? rateAgeDays(detail.effective_date, today) : null
      return {
        currency,
        rate: Number(rate),
        effectiveDate,
        source,
        ageDays,
        stale: ageDays !== null && ageDays > staleDays
      }
    })
}
