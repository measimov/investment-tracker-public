/** 统计：概览、分布、看板快照、期间损益、业绩分析与历史行情同步。 */
import { apiClient, type QueryParams } from './client'
import type { PeriodPnlResponse } from '@/types'

export const statisticsApi = {
  // Statistics
  getSummary() {
    return apiClient.get('/statistics/summary')
  },
  getStatsByMarket() {
    return apiClient.get('/statistics/by-market')
  },
  getStatsByTime(groupBy = 'month') {
    return apiClient.get('/statistics/by-time', { params: { group_by: groupBy } })
  },
  getHoldingsCostBreakdown() {
    return apiClient.get('/statistics/holdings-cost-breakdown')
  },

  // Portfolio snapshot：一次调用返回看板全量数据（表现/新鲜度/市场/近期交易/对账状态）
  getPortfolioSnapshot() {
    return apiClient.get('/statistics/portfolio-snapshot')
  },

  // 当日 / 本月 / 本年损益（权益仓口径，与收益曲线同一算法；服务端定价）
  getPeriodPnl() {
    return apiClient.get<PeriodPnlResponse>('/statistics/period-pnl')
  },

  // Performance Statistics
  // Without prices the server values holdings from its own authority (GET);
  // passing prices is the manual what-if path (POST).
  getPerformanceSummary(currentPrices: Record<string, number | string> | null = null) {
    return currentPrices
      ? apiClient.post('/statistics/performance-summary', currentPrices)
      : apiClient.get('/statistics/performance-summary')
  },
  getPerformanceAnalytics(
    currentPrices: Record<string, number | string> | null = null,
    params: QueryParams = {}
  ) {
    return currentPrices
      ? apiClient.post('/statistics/performance-analytics', currentPrices, { params })
      : apiClient.get('/statistics/performance-analytics', { params })
  },
  getBenchmarkCatalog() {
    return apiClient.get('/statistics/benchmarks')
  },
  startPerformanceHistorySync(params: QueryParams = {}) {
    return apiClient.post('/statistics/performance-history-sync', null, { params })
  },
  getPerformanceHistorySyncJob(jobId: number | string) {
    return apiClient.get(`/statistics/performance-history-sync/${jobId}`)
  }
}
