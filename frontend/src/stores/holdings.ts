import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'
import { paramsKey } from '../utils/cacheKey'
import type { HoldingResponse } from '../types'

// 后端 HoldingResponse schema 为准（PR #172 复审：放宽的手写副本会让必填
// 字段删改从 typecheck 手里溜走）。Decimal 序列化为 string，展示经 toNumber。
export type Holding = HoldingResponse

interface FetchOptions {
  force?: boolean
}

// 用服务端返回的整行回填缓存，而不是把用户输入的 price 原样写进去：
// 类型上 current_price 是 Decimal 字符串，语义上 price_updated_at 等
// 兄弟字段也随之更新，本地拼行两头都不对。
function patchHolding(cache: Record<string, Holding[]>, updated: Holding) {
  Object.keys(cache).forEach((key) => {
    cache[key] = cache[key].map((holding) => (holding.id === updated.id ? updated : holding))
  })
}

export const useHoldingsStore = defineStore('holdings', () => {
  const cache = ref<Record<string, Holding[]>>({})
  const loadingKeys = ref<Record<string, boolean>>({})

  async function fetchHoldings(
    params: Record<string, unknown> = {},
    options: FetchOptions = {}
  ): Promise<Holding[]> {
    const key = paramsKey(params)
    if (!options.force && cache.value[key]) {
      return cache.value[key]
    }

    loadingKeys.value[key] = true
    try {
      const response = await api.getHoldings(params)
      cache.value[key] = response.data
      return response.data
    } finally {
      loadingKeys.value[key] = false
    }
  }

  function isLoading(params: Record<string, unknown> = {}): boolean {
    return loadingKeys.value[paramsKey(params)] === true
  }

  function invalidate() {
    cache.value = {}
    loadingKeys.value = {}
  }

  async function updateHoldingPrice(holdingId: number | string, price: number | string) {
    const response = await api.updateHoldingPrice(holdingId, price)
    patchHolding(cache.value, response.data)
    return response
  }

  async function batchUpdatePrices(
    updates: Array<{ symbol: string; market: string; price: number | string }>
  ) {
    const response = await api.batchUpdatePrices(updates)
    invalidate()
    return response
  }

  async function refreshAllPrices() {
    const response = await api.refreshAllPrices()
    invalidate()
    return response
  }

  return {
    cache,
    fetchHoldings,
    isLoading,
    invalidate,
    updateHoldingPrice,
    batchUpdatePrices,
    refreshAllPrices
  }
})
