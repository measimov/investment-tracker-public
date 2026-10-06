/**
 * 业绩摘要 feature（issue #140：五件事之"业绩摘要"）：五个数据块 + 装载。
 * GET 走服务端权威定价；传 prices 是价格弹窗的 what-if 路径（POST）。
 */

import { getCurrentScope, onScopeDispose, reactive } from 'vue'
import api from '@/api'
import type {
  AccountReturn,
  CurrentPerformance,
  DividendSummary,
  RealizedPnL,
  TotalRealizedReturn
} from './types'
import { serverPriceMap } from './priceRows'
import type { PriceFreshnessInfo } from './warnings'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { showApiError } from '@/utils/showApiError'
import type { ReceivableReturn, PerformanceSummary } from '@/types'

export function usePerformanceSummary() {
  let requestSequence = 0
  if (getCurrentScope()) onScopeDispose(() => ++requestSequence)
  const state = reactive({
    loaded: false,
    error: '',
    receivableReturn: null as ReceivableReturn | null,
    currentPerformance: {
      unrealized_pnl_cny: null,
      current_holdings_cost_cny: null,
      unrealized_pnl_rate: null,
      current_market_value_cny: null,
      holdings_detail: []
    } as CurrentPerformance,
    realizedPnL: {
      realized_pnl: null,
      sold_cost: null,
      realized_pnl_rate: null,
      trades_detail: [],
      data_quality: { warnings: [] }
    } as RealizedPnL,
    dividendSummary: {
      total_dividend_gross: null,
      total_tax: null,
      total_dividend_net: null,
      by_symbol: []
    } as DividendSummary,
    totalRealizedReturn: {
      realized_trading_pnl_cny: null,
      net_dividend_income_cny: null,
      total_realized_return: null,
      total_realized_return_rate: null,
      sold_cost_cny: null
    } as TotalRealizedReturn,
    accountReturn: {
      total_return: null,
      total_return_rate: null,
      annualized_return_rate: null,
      net_invested_principal_cny: null,
      current_market_value_cny: null,
      realized_trading_pnl_cny: null,
      unrealized_pnl_cny: null,
      net_dividend_income_cny: null
    } as AccountReturn
  })

  // 服务端定价（GET）附带的价格新鲜度与实际估值价：试算（POST）时不适用，
  // 置空而不是沿用上一次的——否则手工价下还在报「陈价」
  const pricing = reactive({
    priceFreshness: null as Record<string, PriceFreshnessInfo> | null,
    serverPrices: {} as Record<string, number>
  })

  function apply(data: PerformanceSummary) {
    if (
      !data.current_performance ||
      !data.realized_pnl ||
      !data.dividend_summary ||
      !data.total_realized_return ||
      !data.account_return
    )
      throw new Error('返回的业绩摘要不完整')
    state.currentPerformance = data.current_performance
    state.realizedPnL = data.realized_pnl
    state.dividendSummary = data.dividend_summary
    state.totalRealizedReturn = data.total_realized_return
    state.accountReturn = data.account_return
    state.receivableReturn = data.receivable_return ?? null
  }

  async function load(prices: Record<string, number> | null = null) {
    // null -> server-side authoritative prices (GET); a prices object is the
    // manual what-if path from the price dialog (POST).
    const sequence = ++requestSequence
    try {
      const response = await api.getPerformanceSummary(prices)
      if (sequence !== requestSequence) return false
      apply(response.data)
      state.loaded = true
      state.error = ''
      if (prices) {
        pricing.priceFreshness = null
      } else {
        pricing.priceFreshness = response.data?.price_freshness ?? null
        pricing.serverPrices = serverPriceMap(response.data?.current_performance?.holdings_detail)
      }
      return true
    } catch (error) {
      if (sequence !== requestSequence) return false
      state.error = getApiErrorMessage(error, '加载业绩摘要失败')
      showApiError(error, { prefix: '加载业绩摘要失败' })
      return false
    }
  }

  return reactive({ state, pricing, load })
}

export type PerformanceSummaryFeature = ReturnType<typeof usePerformanceSummary>
