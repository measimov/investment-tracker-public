/**
 * 标的检索（/securities/search）与按需解析（/securities/resolve）的取数层。
 *
 * - 缓存存储在 utils/securitySearchCache（按认证身份命名空间 + 失效代际）；这里只在
 *   请求发起时抓取 epoch，响应回来 epoch 仍有效才写缓存、才回调。
 * - **最新胜出的所有权令牌**（同 `useOpinionFeed` 的 op 模式）：el-autocomplete 的
 *   `getData` 没有过期响应守卫——谁的回调后到谁赢。只让最新一次检索调 `cb`；被超越的
 *   请求返回 null 且不动任何状态。最新那次必须调 `cb`，否则组件的 loading 会卡住。
 * - 失败静默（`skipGlobalErrorNotification`）：检索是锦上添花，不拦手工输入。
 * - 失效不在这里：api 客户端响应拦截器对所有账本写端点统一 invalidate。
 */
import { ref } from 'vue'
import type { AutocompleteFetchSuggestions } from 'element-plus'
import api from '@/api'
import { paramsKey } from '@/utils/cacheKey'
import { normalizeSymbolInput } from '@/utils/securities'
import {
  cacheEpoch,
  isEpochCurrent,
  resolveCacheGet,
  resolveCacheSet,
  searchCacheGet,
  searchCacheSet
} from '@/utils/securitySearchCache'
import type { SecurityResolveResponse, SecuritySearchItem } from '@/types'

export { invalidateSecuritySearchCache } from '@/utils/securitySearchCache'

export function useSecuritySearch(options: {
  isUnmounted: () => boolean
  /** 检索时限定市场；返回空/undefined 表示不限 */
  market?: () => string | null | undefined
  limit?: number
  now?: () => number
}) {
  const now = options.now ?? (() => Date.now())
  const loading = ref(false)
  const lastItems = ref<SecuritySearchItem[]>([])
  let searchOp = 0
  let resolveOp = 0

  /** 返回 null = 本次请求已被更新的请求超越 / 组件已卸载 / 身份或代际已变，调用方不得再写状态 */
  async function search(query: string): Promise<SecuritySearchItem[] | null> {
    const current = ++searchOp
    const epoch = cacheEpoch()
    const owns = () => !options.isUnmounted() && current === searchOp && isEpochCurrent(epoch)
    const params = {
      q: normalizeSymbolInput(query),
      market: options.market?.() || undefined,
      limit: options.limit ?? 20
    }
    const key = paramsKey(params)
    const cached = searchCacheGet(key, now())
    if (cached) {
      lastItems.value = cached
      return cached
    }
    loading.value = true
    try {
      const response = await api.searchSecurities(params)
      const items = (response.data?.items || []) as SecuritySearchItem[]
      searchCacheSet(key, items, now(), epoch) // 身份/代际已变则拒写
      if (!owns()) return null
      lastItems.value = items
      return items
    } catch {
      return owns() ? [] : null
    } finally {
      if (owns()) loading.value = false
    }
  }

  // el-autocomplete 的契约是同步 void + 回调；async 处理器返回 Promise<void>
  // 过不了 vue-tsc 的模板 prop 检查（CI 实锤）。只有仍持有所有权的请求才调 cb。
  const fetchSuggestions: AutocompleteFetchSuggestions = (query, callback) => {
    void search(query).then((items) => {
      if (items !== null) callback(items)
    })
  }

  async function resolve(symbol: string, market: string): Promise<SecurityResolveResponse | null> {
    const normalized = normalizeSymbolInput(symbol)
    if (!normalized || !market) return null
    const key = `${market}|${normalized}`
    const cached = resolveCacheGet(key)
    if (cached) return cached
    const current = ++resolveOp
    const epoch = cacheEpoch()
    const owns = () => !options.isUnmounted() && current === resolveOp && isEpochCurrent(epoch)
    try {
      const response = await api.resolveSecurity({ symbol: normalized, market })
      const result = response.data as SecurityResolveResponse
      if (result?.name) resolveCacheSet(key, result, epoch) // 解析不到的不缓存：下次再试
      return owns() ? result : null
    } catch {
      return null
    }
  }

  return { search, fetchSuggestions, resolve, loading, lastItems }
}
