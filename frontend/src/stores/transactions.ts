import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'
import { paramsKey } from '../utils/cacheKey'
import { useHoldingsStore } from './holdings'

/** 后端交易行：字段随后端演进，这里只声明前端点名使用的字段。 */
export interface Transaction {
  id: number
  broker_account_id?: number | null
  symbol: string
  name?: string | null
  market: string
  transaction_type: string
  quantity: number | string
  price: number | string
  fee: number | string
  transaction_date: string
  currency: string
  notes?: string | null
  import_batch_id?: number | null
  [key: string]: unknown
}

interface FetchOptions {
  force?: boolean
}

export const useTransactionsStore = defineStore('transactions', () => {
  const listCache = ref<Record<string, Transaction[]>>({})
  const countCache = ref<Record<string, number>>({})
  const loadingKeys = ref<Record<string, boolean>>({})

  async function fetchTransactions(
    params: Record<string, unknown> = {},
    options: FetchOptions = {}
  ): Promise<Transaction[]> {
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

  async function fetchTransactionsCount(
    params: Record<string, unknown> = {},
    options: FetchOptions = {}
  ): Promise<number> {
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

  async function createTransaction(data: Record<string, unknown>) {
    const response = await api.createTransaction(data)
    invalidateDependentData()
    return response
  }

  async function updateTransaction(id: number | string, data: Record<string, unknown>) {
    const response = await api.updateTransaction(id, data)
    invalidateDependentData()
    return response
  }

  async function deleteTransaction(id: number | string) {
    const response = await api.deleteTransaction(id)
    invalidateDependentData()
    return response
  }

  // 转仓创建 TRANSFER_OUT/IN 交易对并重算持仓：交易列表与持仓缓存都要失效
  async function createTransfer(data: Record<string, unknown>) {
    const response = await api.createTransfer(data)
    invalidateDependentData()
    return response
  }

  function isLoading(params: Record<string, unknown> = {}): boolean {
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
