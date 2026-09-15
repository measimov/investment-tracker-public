/**
 * 标的检索缓存的存储层（纯模块，无 Vue/axios 依赖，便于测试）。
 *
 * 响应里有当前用户的持仓/自选/历史与账本名称币种，所以缓存**必须绑定认证身份**：
 * 显式登出只 router.push('/login')，不重载页面，A 登出、B 登录后同键查询在 5 分钟内
 * 会直接拿到 A 的候选。两层保护：
 * - `scope` = 当前用户 id（auth store 在登录/恢复/登出时设置），键按 scope 命名空间，
 *   scope 变化即清空；
 * - `generation` 随每次失效递增，请求发起时抓取 `cacheEpoch()`，响应回来只有 epoch
 *   仍然有效才允许写缓存——A 的慢响应不能在清空之后再写回。
 *
 * 失效集中在 api 客户端的响应拦截器（`isLedgerMutation`）：凡是会改变交易/持仓/自选
 * 候选的成功写请求都失效，不靠各页面零散调用。
 */
import type { SecurityResolveResponse, SecuritySearchItem } from '@/types'

export const SEARCH_CACHE_TTL_MS = 5 * 60_000
export const SEARCH_CACHE_MAX = 200

export interface CacheEpoch {
  scope: string | null
  generation: number
}

interface SearchEntry {
  at: number
  items: SecuritySearchItem[]
}

let scope: string | null = null
let generation = 0
const searchCache = new Map<string, SearchEntry>()
const resolveCache = new Map<string, SecurityResolveResponse>()

export function cacheEpoch(): CacheEpoch {
  return { scope, generation }
}

export function isEpochCurrent(epoch: CacheEpoch): boolean {
  return epoch.scope === scope && epoch.generation === generation
}

export function invalidateSecuritySearchCache(): void {
  searchCache.clear()
  resolveCache.clear()
  generation += 1
}

/** 认证身份变化（登录 / 恢复会话 / 登出）：换命名空间并整体失效 */
export function setSecuritySearchCacheScope(next: string | number | null | undefined): void {
  const normalized = next === null || next === undefined ? null : String(next)
  if (normalized === scope) return
  scope = normalized
  invalidateSecuritySearchCache()
}

function scoped(key: string): string {
  return `${scope ?? 'anon'}::${key}`
}

export function searchCacheGet(key: string, now: number): SecuritySearchItem[] | null {
  const fullKey = scoped(key)
  const entry = searchCache.get(fullKey)
  if (!entry) return null
  if (now - entry.at > SEARCH_CACHE_TTL_MS) {
    searchCache.delete(fullKey)
    return null
  }
  // Map 保持插入序：命中即重插，最旧的自然排在最前
  searchCache.delete(fullKey)
  searchCache.set(fullKey, entry)
  return entry.items
}

/** 只有请求发起时的 epoch 仍有效才写入；返回是否写入 */
export function searchCacheSet(
  key: string,
  items: SecuritySearchItem[],
  now: number,
  epoch: CacheEpoch
): boolean {
  if (!isEpochCurrent(epoch)) return false
  searchCache.set(scoped(key), { at: now, items })
  while (searchCache.size > SEARCH_CACHE_MAX) {
    const oldest = searchCache.keys().next().value
    if (oldest === undefined) break
    searchCache.delete(oldest)
  }
  return true
}

export function resolveCacheGet(key: string): SecurityResolveResponse | null {
  return resolveCache.get(scoped(key)) ?? null
}

export function resolveCacheSet(
  key: string,
  result: SecurityResolveResponse,
  epoch: CacheEpoch
): boolean {
  if (!isEpochCurrent(epoch)) return false
  resolveCache.set(scoped(key), result)
  return true
}

const SAFE_METHODS = new Set(['get', 'head', 'options'])
// 会改变检索候选（持仓 ∪ 自选 ∪ 历史交易）的写端点；导入预览不落库故排除
const LEDGER_MUTATION_PATTERNS = [
  /^\/transactions(\/|$)/,
  /^\/watchlist(\/|$)/,
  /^\/holdings(\/|$)/,
  /^\/broker-accounts(\/|$)/,
  /^\/import\/(?!.*\/preview$)/
]

export function isLedgerMutation(method: string | undefined, url: string | undefined): boolean {
  if (!method || SAFE_METHODS.has(method.toLowerCase())) return false
  if (!url) return false
  const path = url
    .split('?')[0]
    .replace(/^https?:\/\/[^/]+/, '')
    .replace(/^\/api(?=\/)/, '')
  return LEDGER_MUTATION_PATTERNS.some((pattern) => pattern.test(path))
}
