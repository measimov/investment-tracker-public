import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'
import { paramsKey } from '../utils/cacheKey'
import { dataEpoch, isDataEpochCurrent, onLedgerEvent } from '../utils/ledgerEvents'
import { useHoldingsStore } from './holdings'
import type {
  Transaction as GeneratedTransaction,
  TransactionCreate,
  TransactionUpdate,
  TransferCreate
} from '../types'

// 后端 TransactionResponse schema 为准（PR #172 复审）
export type Transaction = GeneratedTransaction

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
    const epoch = dataEpoch()
    try {
      const response = await api.getTransactions(params)
      if (isDataEpochCurrent(epoch)) listCache.value[key] = response.data
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

    const epoch = dataEpoch()
    const response = await api.getTransactionsCount(params)
    const total = response.data.total || 0
    if (isDataEpochCurrent(epoch)) countCache.value[key] = total
    return total
  }

  function invalidate() {
    listCache.value = {}
    countCache.value = {}
    loadingKeys.value = {}
  }

  onLedgerEvent('ledger-mutated', invalidate)
  onLedgerEvent('session-changed', invalidate)

  function invalidateDependentData() {
    invalidate()
    useHoldingsStore().invalidate()
  }

  async function createTransaction(data: TransactionCreate) {
    const response = await api.createTransaction(data)
    invalidateDependentData()
    return response
  }

  async function updateTransaction(id: number | string, data: TransactionUpdate) {
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
  async function createTransfer(data: TransferCreate) {
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
