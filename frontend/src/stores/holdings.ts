import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'
import { paramsKey } from '../utils/cacheKey'

/** 后端持仓行：字段较多且随后端演进，这里只声明前端需要点名的字段。 */
export interface Holding {
  id: number
  symbol: string
  name?: string | null
  market: string
  quantity: number | string
  total_cost?: number | string | null
  avg_cost?: number | string | null
  currency?: string
  broker_account_id?: number | null
  current_price?: number | string | null
  [key: string]: unknown
}

interface FetchOptions {
  force?: boolean
}

function patchHoldingPrice(
  cache: Record<string, Holding[]>,
  holdingId: number | string,
  price: number | string
) {
  Object.keys(cache).forEach((key) => {
    cache[key] = cache[key].map((holding) =>
      holding.id === holdingId ? { ...holding, current_price: price } : holding
    )
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
    patchHoldingPrice(cache.value, holdingId, price)
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
