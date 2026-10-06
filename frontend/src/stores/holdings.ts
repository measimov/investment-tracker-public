import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'
import { paramsKey } from '../utils/cacheKey'
import { dataEpoch, isDataEpochCurrent, onLedgerEvent } from '../utils/ledgerEvents'
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
    cache[key] = cache[key].map((holding) => {
      if (holding.id === updated.id) return updated
      // 后端按 user + symbol + market 更新该标的在所有账户的持仓价格：兄弟行只同步价格字段
      // （价格/写库时刻/行情日/来源是一组，#217）
      if (holding.symbol === updated.symbol && holding.market === updated.market) {
        return {
          ...holding,
          current_price: updated.current_price,
          price_updated_at: updated.price_updated_at,
          price_as_of: updated.price_as_of,
          price_source: updated.price_source
        }
      }
      return holding
    })
  })
}

export const useHoldingsStore = defineStore('holdings', () => {
  const cache = ref<Record<string, Holding[]>>({})
  const loadingKeys = ref<Record<string, boolean>>({})
  // 同一标的连续改价：只让**最后一次**请求的响应回填缓存。先发的请求可能后返回，
  // 按到达顺序回填会把缓存改回旧价，下次读缓存时输入框就退回旧值（PR #216 评审 P2）
  const priceRequestSeq = new Map<string, number>()
  let fetchSequence = 0
  const fetchRequests = new Map<string, number>()
  const pendingFetches = new Map<string, Promise<Holding[]>>()

  async function fetchHoldings(
    params: Record<string, unknown> = {},
    options: FetchOptions = {}
  ): Promise<Holding[]> {
    const key = paramsKey(params)
    if (!options.force && pendingFetches.has(key)) return pendingFetches.get(key)!
    if (!options.force && cache.value[key]) {
      return cache.value[key]
    }

    const request = ++fetchSequence
    fetchRequests.set(key, request)
    loadingKeys.value[key] = true
    const epoch = dataEpoch()
    const snapshot = { ...params }
    const promise = api
      .getHoldings(snapshot)
      .then((response) => {
        // 请求在途期间发生过账本写入或换了用户：响应可能是旧数据/上一个用户的，不写回缓存
        if (isDataEpochCurrent(epoch) && fetchRequests.get(key) === request)
          cache.value[key] = response.data
        return response.data
      })
      .finally(() => {
        if (fetchRequests.get(key) === request) {
          loadingKeys.value[key] = false
          pendingFetches.delete(key)
        }
      })
    pendingFetches.set(key, promise)
    return promise
  }

  function isLoading(params: Record<string, unknown> = {}): boolean {
    return loadingKeys.value[paramsKey(params)] === true
  }

  function invalidate() {
    cache.value = {}
    loadingKeys.value = {}
    fetchRequests.clear()
    pendingFetches.clear()
  }

  // 账本写入与登出/换用户由 api 拦截器、auth store 统一发信号（#268），不再靠各页面零散失效
  onLedgerEvent('ledger-mutated', invalidate)
  onLedgerEvent('session-changed', () => {
    invalidate()
    priceRequestSeq.clear()
  })

  async function updateHoldingPrice(
    holdingId: number | string,
    price: number | string,
    securityKey?: string
  ) {
    const seq = (priceRequestSeq.get(securityKey ?? String(holdingId)) ?? 0) + 1
    priceRequestSeq.set(securityKey ?? String(holdingId), seq)
    const response = await api.updateHoldingPrice(holdingId, price)
    if (priceRequestSeq.get(securityKey ?? String(holdingId)) === seq) {
      patchHolding(cache.value, response.data)
    }
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
