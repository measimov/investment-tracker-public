/**
 * 统计页顶部警告的合成（#218，纯函数）。
 *
 * 来源：已实现盈亏与持仓表现的 data_quality.warnings、四块数据的缺汇率币种。此前只取已实现
 * 那一块，仪表盘对同一份数据会报警、统计页却一声不吭。陈价/缺价由 PriceIssuesAlert 以摘要
 * 展示（#286），这里只用 price_freshness 判断「有没有具体缺价清单」。
 */

import type { PriceFreshnessEntry } from '@/utils/priceIssues'

export type { PriceFreshnessEntry as PriceFreshnessInfo } from '@/utils/priceIssues'

// 后端 aggregates.UNPRICED_POSITIONS_WARNING：有具体缺价清单时它是同一批标的的泛泛重述
export const UNPRICED_POSITIONS_WARNING = '部分当前持仓缺少可用估值价格，其成本与市值未计入汇总。'

export function missingRateWarning(currencies: string[]): string {
  return (
    `缺少 ${currencies.join('/')} 对 CNY 的汇率，这些币种的金额未计入 CNY 汇总（不会按原值混入）。` +
    '请在「汇率管理」补录后重新查看。'
  )
}

const RATE_WARNING_RE = /缺少\s*([A-Z]{3}(?:\/[A-Z]{3})*)\s*对 CNY 的汇率/

/** 后端缺汇率提示里的币种（"缺少 HKD/USD 对 CNY 的汇率…" → ['HKD','USD']）；非此类提示返回 null */
export function parseRateWarningCurrencies(text: string): string[] | null {
  const match = RATE_WARNING_RE.exec(text)
  return match ? match[1].split('/') : null
}

export function buildSummaryWarnings(input: {
  realizedWarnings?: string[] | null
  currentWarnings?: string[] | null
  missingRateCurrencies?: string[] | null
  priceFreshness?: Record<string, PriceFreshnessEntry> | null
}): string[] {
  const hasMissingPriceList = Object.values(input.priceFreshness || {}).some(
    (entry) => entry?.source === 'missing'
  )

  const warnings: string[] = []
  const push = (text: string) => {
    if (text && !warnings.includes(text)) warnings.push(text)
  }
  // 缺汇率：已实现/持仓表现各自带一条、其余块只给币种列表——全部并成一条，
  // 同一币种不会在两条提示里各出现一次
  const currencies = new Set(input.missingRateCurrencies || [])
  const sources = [...(input.realizedWarnings || []), ...(input.currentWarnings || [])]
  for (const text of sources) {
    const rateCurrencies = parseRateWarningCurrencies(text)
    if (rateCurrencies) {
      rateCurrencies.forEach((currency) => currencies.add(currency))
      continue
    }
    // 有具体缺价清单时，持仓表现那句泛泛的缺价提示说的是同一批标的
    if (hasMissingPriceList && text === UNPRICED_POSITIONS_WARNING) continue
    // 已实现与持仓表现共用同一份 FIFO 质量信号，逐字相同的由 push 去重
    push(text)
  }
  if (currencies.size) push(missingRateWarning(Array.from(currencies).sort()))

  return warnings
}
