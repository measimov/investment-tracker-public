/** 统计：概览、分布、看板快照、期间损益、业绩分析与历史行情同步。 */
import { apiClient, type QueryParams } from './client'
import type {
  PeriodPnlResponse,
  PerformanceSummary,
  BenchmarkCatalogItem,
  ProfitLossItem,
  TimeStat,
  PerformanceAnalytics,
  SummaryStats,
  HistorySyncJob,
  PortfolioSnapshot,
  MarketStat
} from '@/types'

export const statisticsApi = {
  // Statistics
  getSummary() {
    return apiClient.get<SummaryStats>('/statistics/summary')
  },
  getStatsByMarket() {
    return apiClient.get<MarketStat[]>('/statistics/by-market')
  },
  getStatsByTime(groupBy = 'month') {
    return apiClient.get<TimeStat[]>('/statistics/by-time', { params: { group_by: groupBy } })
  },
  getHoldingsCostBreakdown() {
    return apiClient.get<ProfitLossItem[]>('/statistics/holdings-cost-breakdown')
  },

  // Portfolio snapshot：一次调用返回看板全量数据（表现/新鲜度/市场/近期交易/对账状态）
  getPortfolioSnapshot() {
    return apiClient.get<PortfolioSnapshot>('/statistics/portfolio-snapshot')
  },

  // 当日 / 本月 / 本年损益；手工试价与业绩摘要使用同一组价格。
  getPeriodPnl(currentPrices: Record<string, number> | null = null) {
    return currentPrices
      ? apiClient.post<PeriodPnlResponse>('/statistics/period-pnl', currentPrices)
      : apiClient.get<PeriodPnlResponse>('/statistics/period-pnl')
  },

  // Performance Statistics
  // Without prices the server values holdings from its own authority (GET);
  // passing prices is the manual what-if path (POST).
  getPerformanceSummary(currentPrices: Record<string, number | string> | null = null) {
    return currentPrices
      ? apiClient.post<PerformanceSummary>('/statistics/performance-summary', currentPrices)
      : apiClient.get<PerformanceSummary>('/statistics/performance-summary')
  },
  getPerformanceAnalytics(
    currentPrices: Record<string, number | string> | null = null,
    params: QueryParams = {}
  ) {
    return currentPrices
      ? apiClient.post<PerformanceAnalytics>('/statistics/performance-analytics', currentPrices, {
          params
        })
      : apiClient.get<PerformanceAnalytics>('/statistics/performance-analytics', { params })
  },
  getBenchmarkCatalog() {
    return apiClient.get<BenchmarkCatalogItem[]>('/statistics/benchmarks')
  },
  startPerformanceHistorySync(params: QueryParams = {}) {
    return apiClient.post<HistorySyncJob>('/statistics/performance-history-sync', null, { params })
  },
  getPerformanceHistorySyncJob(jobId: number | string) {
    return apiClient.get<HistorySyncJob>(`/statistics/performance-history-sync/${jobId}`)
  }
}
