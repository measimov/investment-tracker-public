import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'
import { paramsKey } from '../utils/cacheKey'

function patchHoldingPrice(cache, holdingId, price) {
  Object.keys(cache).forEach((key) => {
    cache[key] = cache[key].map((holding) =>
      holding.id === holdingId ? { ...holding, current_price: price } : holding
    )
  })
}

export const useHoldingsStore = defineStore('holdings', () => {
  const cache = ref({})
  const loadingKeys = ref({})

  async function fetchHoldings(params = {}, options = {}) {
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

  function isLoading(params = {}) {
    return loadingKeys.value[paramsKey(params)] === true
  }

  function invalidate() {
    cache.value = {}
    loadingKeys.value = {}
  }

  async function updateHoldingPrice(holdingId, price) {
    const response = await api.updateHoldingPrice(holdingId, price)
    patchHoldingPrice(cache.value, holdingId, price)
    return response
  }

  async function batchUpdatePrices(updates) {
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
