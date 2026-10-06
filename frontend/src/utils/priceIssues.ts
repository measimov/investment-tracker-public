/**
 * 陈价/缺价提示（#286）：仪表盘与统计页共用。此前两页把几十只标的以 `000333:A股, …` 的内部键
 * 平铺成一整段（没有名称、不可折叠），且一页黄色一页红色。改为「一行摘要 + 展开看名称清单」。
 * 输入是后端 GET 业绩端点 / 组合快照带回的 price_freshness（键 `symbol:market`）。
 */

import { formatDate } from './helpers'

import type { PriceFreshnessEntry } from '@/types'
export type { PriceFreshnessEntry } from '@/types'

export interface PriceIssueItem {
  key: string
  symbol: string
  market: string
  name: string | null
  priceDate: string | null
}

export interface PriceIssues {
  stale: PriceIssueItem[]
  missing: PriceIssueItem[]
}

// 与后端 pricing.PRICE_STALE_DAYS 同值；stale 由后端判定，这里只用于文案
export const PRICE_STALE_DAYS = 7

function toItem(key: string, entry: PriceFreshnessEntry): PriceIssueItem {
  const index = key.indexOf(':')
  return {
    key,
    symbol: index > 0 ? key.slice(0, index) : key,
    market: index > 0 ? key.slice(index + 1) : '',
    name: entry.name || null,
    priceDate: entry.price_date || null
  }
}

export function collectPriceIssues(
  freshness: Record<string, PriceFreshnessEntry> | null | undefined
): PriceIssues {
  const stale: PriceIssueItem[] = []
  const missing: PriceIssueItem[] = []
  for (const [key, entry] of Object.entries(freshness || {})) {
    if (entry?.source === 'missing') missing.push(toItem(key, entry))
    else if (entry?.stale) stale.push(toItem(key, entry))
  }
  // 最旧的价格排前面：最需要刷新的先看到
  stale.sort(
    (a, b) => (a.priceDate || '').localeCompare(b.priceDate || '') || a.key.localeCompare(b.key)
  )
  missing.sort((a, b) => a.key.localeCompare(b.key))
  return { stale, missing }
}

export function staleSummary(issues: PriceIssues): string | null {
  if (!issues.stale.length) return null
  const earliest = issues.stale.find((item) => item.priceDate)?.priceDate
  return (
    `${issues.stale.length} 只持仓价格超过 ${PRICE_STALE_DAYS} 天未更新` +
    (earliest ? `，最早 ${formatDate(earliest)}` : '')
  )
}

export function missingSummary(issues: PriceIssues): string | null {
  if (!issues.missing.length) return null
  return `${issues.missing.length} 只持仓缺少可用估值价格，未计入市值`
}

/** 清单里一行：名称（代码 · 市场）价格日期 */
export function describeIssueItem(item: PriceIssueItem): string {
  const label = item.name
    ? `${item.name}（${item.symbol} · ${item.market}）`
    : `${item.symbol} · ${item.market}`
  return item.priceDate ? `${label} ${formatDate(item.priceDate)}` : label
}

// 后端 data_quality.warnings 里同一件事的原文（组合快照的清单式提示）：页面改用摘要组件展示，
// 从通用警告列表里剔除（后端保留原文是给 AI 复盘输入用的）
const PRICE_WARNING_PREFIXES = ['以下标的估值价格超过', '以下标的缺少可用估值价格']

export function isPriceIssueWarning(text: string): boolean {
  return PRICE_WARNING_PREFIXES.some((prefix) => text.startsWith(prefix))
}
