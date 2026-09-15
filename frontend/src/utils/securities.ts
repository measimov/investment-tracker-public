/**
 * 标的录入的跨页纯逻辑：市场常量、币种推断、选中/自由文本/按需解析三种表单补丁。
 *
 * 三种补丁语义刻意不同，别合并：
 * - `securityFormPatch`（选中候选）：**完整替换** symbol/market/name/currency——条件赋值
 *   会把上一只标的的 name/currency 静默带给新标的（PR #188 评审 P1）。
 * - `freeTextFormPatch`（手输新代码）：只动 symbol 与"仍是上次自动带出"的 name，
 *   币种按市场重新推断；用户手改过的名称绝不动。
 * - `resolvedFormPatch`（后端按需解析回来）：**只填空**——名称空才填，币种只覆盖
 *   "仍是市场推断值"的那种，用户显式选过的币种不动。
 */
import type { SecurityResolveResponse, SecuritySearchItem } from '@/types'

/** 与后端 `schemas/security_rule.py:VALID_MARKETS` 对齐 */
export const MARKETS = ['A股', 'B股', '港股', '美股', '新加坡股', '加密货币'] as const
export type Market = (typeof MARKETS)[number]

// 市场 → 无歧义默认币种。B股（沪B=USD/深B=HKD）按代码段推断，加密货币推不出。
export const MARKET_DEFAULT_CURRENCY: Record<string, string> = {
  A股: 'CNY',
  港股: 'HKD',
  美股: 'USD',
  新加坡股: 'SGD'
}

export function normalizeSymbolInput(text: string | null | undefined): string {
  return (text || '').trim().toUpperCase()
}

/** 市场（+ B股代码段）能无歧义推出的币种；推不出返回 ''，让必填校验拦住提交 */
export function inferCurrency(market: string | null | undefined, symbol?: string | null): string {
  if (market === 'B股') {
    const code = normalizeSymbolInput(symbol)
    if (code.startsWith('900')) return 'USD'
    if (code.startsWith('200')) return 'HKD'
    return ''
  }
  return (market && MARKET_DEFAULT_CURRENCY[market]) || ''
}

/**
 * 市场变化时的币种：仍是自动推导值（选候选/换市场/解析回填）→ 按新市场重推，**推不出即清空**
 * （加密货币/未知 B 股段），让必填校验逼用户显式选择——保留旧值会把 BTC 当 CNY 入账；
 * 用户手选过一次 → 保留人工币种。
 */
export function followMarketCurrency(
  current: string,
  market: string,
  symbol: string | null | undefined,
  currencyAuto: boolean
): string {
  return currencyAuto ? inferCurrency(market, symbol) : current
}

export interface SecurityFormFields {
  symbol: string
  market: string
  name: string
  currency: string
}

type PickedSecurity = Pick<SecuritySearchItem, 'symbol' | 'market'> &
  Partial<Pick<SecuritySearchItem, 'name' | 'currency'>>

/** 选中候选：完整替换四个字段；无值字段清空（name 留给用户填，currency 先推断） */
export function securityFormPatch(picked: PickedSecurity): SecurityFormFields {
  return {
    symbol: picked.symbol,
    market: picked.market,
    name: picked.name || '',
    currency: picked.currency || inferCurrency(picked.market, picked.symbol)
  }
}

/**
 * 手输新代码（未选候选）：换代码即换标的。仍是上次候选自动带出的名称清空，
 * 用户手改过的保留；币种按当前市场重新推断（推不出则保留原值）。
 */
export function freeTextFormPatch(
  current: Partial<SecurityFormFields>,
  input: { symbol: string; lastPicked: Pick<SecuritySearchItem, 'symbol' | 'name'> | null }
): Partial<SecurityFormFields> {
  const patch: Partial<SecurityFormFields> = { symbol: normalizeSymbolInput(input.symbol) }
  const autoFilledName = input.lastPicked?.name || ''
  if (input.lastPicked && (current.name || '') === autoFilledName) patch.name = ''
  const inferred = inferCurrency(current.market, patch.symbol)
  if (inferred) patch.currency = inferred
  return patch
}

/** 按需解析结果只填空：名称空才填；币种只覆盖"仍等于市场推断值"的那种 */
export function resolvedFormPatch(
  current: Partial<SecurityFormFields>,
  resolved: Pick<SecurityResolveResponse, 'symbol' | 'market' | 'name' | 'currency'>
): Partial<SecurityFormFields> {
  const patch: Partial<SecurityFormFields> = {}
  if (normalizeSymbolInput(current.symbol) !== normalizeSymbolInput(resolved.symbol)) return patch
  if (!(current.name || '') && resolved.name) patch.name = resolved.name
  const inferred = inferCurrency(current.market || resolved.market, current.symbol)
  const replaceable = !(current.currency || '') || current.currency === inferred
  if (resolved.currency && replaceable && resolved.currency !== current.currency) {
    patch.currency = resolved.currency
  }
  return patch
}
