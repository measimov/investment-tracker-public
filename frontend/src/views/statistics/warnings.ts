/**
 * 统计页顶部警告的合成（#218，纯函数）。
 *
 * 来源：已实现盈亏与持仓表现的 data_quality.warnings、四块数据的缺汇率币种、
 * GET 业绩摘要附带的 price_freshness（陈价/缺价）。此前只取已实现那一块，
 * 仪表盘对同一份数据会报警、统计页却一声不吭。
 */

export interface PriceFreshnessInfo {
  source?: string
  price_as_of?: string | null
  stale?: boolean
}

// 与后端 pricing.PRICE_STALE_DAYS 同值；freshness.stale 已由后端判定，这里只用于文案
export const PRICE_STALE_DAYS = 7

// 后端 aggregates.UNPRICED_POSITIONS_WARNING：有具体缺价清单时它是同一批标的的泛泛重述
export const UNPRICED_POSITIONS_WARNING = '部分当前持仓缺少可用估值价格，其成本与市值未计入汇总。'

/** "600000:A股" → "600000（A股）" */
export function describePriceKey(key: string): string {
  const index = key.indexOf(':')
  if (index <= 0) return key
  return `${key.slice(0, index)}（${key.slice(index + 1)}）`
}

export function freshnessWarnings(
  freshness: Record<string, PriceFreshnessInfo> | null | undefined
): string[] {
  if (!freshness) return []
  const stale: string[] = []
  const missing: string[] = []
  for (const [key, info] of Object.entries(freshness)) {
    if (info?.source === 'missing') missing.push(key)
    else if (info?.stale) stale.push(key)
  }
  const warnings: string[] = []
  if (stale.length) {
    warnings.push(
      `以下标的估值价格超过 ${PRICE_STALE_DAYS} 天未更新：${stale.sort().map(describePriceKey).join('、')}`
    )
  }
  if (missing.length) {
    warnings.push(
      `以下标的缺少可用估值价格，未计入市值：${missing.sort().map(describePriceKey).join('、')}`
    )
  }
  return warnings
}

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
  priceFreshness?: Record<string, PriceFreshnessInfo> | null
}): string[] {
  const priceWarnings = freshnessWarnings(input.priceFreshness)
  const hasMissingPriceList = priceWarnings.some((text) => text.includes('缺少可用估值价格'))

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

  for (const text of priceWarnings) push(text)
  return warnings
}
