/**
 * 业绩摘要 feature（issue #140：五件事之"业绩摘要"）：五个数据块 + 装载。
 * GET 走服务端权威定价；传 prices 是价格弹窗的 what-if 路径（POST）。
 */

import { reactive } from 'vue'
import api from '@/api'
import type {
  AccountReturn,
  CurrentPerformance,
  DividendSummary,
  RealizedPnL,
  TotalRealizedReturn
} from './types'

export function usePerformanceSummary() {
  const state = reactive({
    currentPerformance: {
      unrealized_pnl: 0,
      current_holdings_cost: 0,
      unrealized_pnl_rate: 0,
      current_market_value: 0,
      holdings_detail: []
    } as CurrentPerformance,
    realizedPnL: {
      realized_pnl: 0,
      sold_cost: 0,
      realized_pnl_rate: 0,
      trades_detail: [],
      data_quality: { warnings: [] }
    } as RealizedPnL,
    dividendSummary: {
      total_dividend_gross: 0,
      total_tax: 0,
      total_dividend_net: 0,
      by_symbol: []
    } as DividendSummary,
    totalRealizedReturn: {
      realized_trading_pnl_cny: 0,
      net_dividend_income_cny: 0,
      total_realized_return: 0,
      total_realized_return_rate: 0,
      sold_cost_cny: 0
    } as TotalRealizedReturn,
    accountReturn: {
      total_return: 0,
      total_return_rate: 0,
      annualized_return_rate: null,
      net_invested_principal_cny: 0,
      current_market_value_cny: 0,
      realized_trading_pnl_cny: 0,
      unrealized_pnl_cny: 0,
      net_dividend_income_cny: 0
    } as AccountReturn
  })

  function apply(data: {
    current_performance?: CurrentPerformance
    realized_pnl?: RealizedPnL
    dividend_summary?: DividendSummary
    total_realized_return?: TotalRealizedReturn
    account_return?: AccountReturn
  }) {
    state.currentPerformance = data.current_performance || state.currentPerformance
    state.realizedPnL = data.realized_pnl || state.realizedPnL
    state.dividendSummary = data.dividend_summary || state.dividendSummary
    state.totalRealizedReturn = data.total_realized_return || state.totalRealizedReturn
    state.accountReturn = data.account_return || state.accountReturn
  }

  async function load(prices: Record<string, number> | null = null) {
    // null -> server-side authoritative prices (GET); a prices object is the
    // manual what-if path from the price dialog (POST).
    const response = await api.getPerformanceSummary(prices)
    apply(response.data)
  }

  return reactive({ state, load })
}

export type PerformanceSummaryFeature = ReturnType<typeof usePerformanceSummary>
