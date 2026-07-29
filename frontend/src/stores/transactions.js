import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'
import { paramsKey } from '../utils/cacheKey'
import { useHoldingsStore } from './holdings'

export const useTransactionsStore = defineStore('transactions', () => {
  const listCache = ref({})
  const countCache = ref({})
  const loadingKeys = ref({})

  async function fetchTransactions(params = {}, options = {}) {
    const key = paramsKey(params)
    if (!options.force && listCache.value[key]) {
      return listCache.value[key]
    }

    loadingKeys.value[key] = true
    try {
      const response = await api.getTransactions(params)
      listCache.value[key] = response.data
      return response.data
    } finally {
      loadingKeys.value[key] = false
    }
  }

  async function fetchTransactionsCount(params = {}, options = {}) {
    const key = paramsKey(params)
    if (!options.force && countCache.value[key] !== undefined) {
      return countCache.value[key]
    }

    const response = await api.getTransactionsCount(params)
    const total = response.data.total || 0
    countCache.value[key] = total
    return total
  }

  function invalidate() {
    listCache.value = {}
    countCache.value = {}
    loadingKeys.value = {}
  }

  function invalidateDependentData() {
    invalidate()
    useHoldingsStore().invalidate()
  }

  async function createTransaction(data) {
    const response = await api.createTransaction(data)
    invalidateDependentData()
    return response
  }

  async function updateTransaction(id, data) {
    const response = await api.updateTransaction(id, data)
    invalidateDependentData()
    return response
  }

  async function deleteTransaction(id) {
    const response = await api.deleteTransaction(id)
    invalidateDependentData()
    return response
  }

  // 转仓创建 TRANSFER_OUT/IN 交易对并重算持仓：交易列表与持仓缓存都要失效
  async function createTransfer(data) {
    const response = await api.createTransfer(data)
    invalidateDependentData()
    return response
  }

  function isLoading(params = {}) {
    return loadingKeys.value[paramsKey(params)] === true
  }

  return {
    listCache,
    countCache,
    fetchTransactions,
    fetchTransactionsCount,
    createTransaction,
    updateTransaction,
    deleteTransaction,
    invalidate,
    invalidateDependentData,
    createTransfer,
    isLoading
  }
})
