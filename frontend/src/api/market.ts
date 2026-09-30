/** 行情：汇率、现价更新与刷新任务。 */
import { apiClient, type QueryParams } from './client'
import type {
  ExchangeRate,
  ExchangeRateCheck,
  ExchangeRateCreate,
  ExchangeRateLatest,
  ExchangeRateUpdate,
  HoldingResponse
} from '@/types'

export const marketApi = {
  // Exchange Rates
  getLatestRates() {
    return apiClient.get<ExchangeRateLatest>('/exchange-rates/latest')
  },
  getExchangeRates(params?: QueryParams) {
    return apiClient.get<ExchangeRate[]>('/exchange-rates', { params })
  },
  createOrUpdateExchangeRate(data: ExchangeRateCreate) {
    return apiClient.post<ExchangeRate>('/exchange-rates', data)
  },
  updateExchangeRate(id: number | string, data: ExchangeRateUpdate) {
    return apiClient.put<ExchangeRate>(`/exchange-rates/${id}`, data)
  },
  deleteExchangeRate(id: number | string) {
    return apiClient.delete<void>(`/exchange-rates/${id}`)
  },
  refreshRatesFromAPI() {
    return apiClient.post('/exchange-rates/refresh-from-api')
  },
  getExchangeRateSourceChecks(days = 30) {
    return apiClient.get<ExchangeRateCheck[]>('/exchange-rates/source-checks', {
      params: { days }
    })
  },

  // Stock Price Updates
  updateHoldingPrice(holdingId: number | string, price: number | string) {
    return apiClient.put<HoldingResponse>(`/holdings/${holdingId}/price`, {
      current_price: price
    })
  },
  batchUpdatePrices(updates: Array<{ symbol: string; market: string; price: number | string }>) {
    // updates format: [{ symbol, market, price }, ...]
    return apiClient.post('/holdings/prices/batch-update', updates)
  },
  refreshAllPrices() {
    return apiClient.post('/holdings/prices/refresh-from-api')
  },
  getPriceRefreshJob(jobId: number | string) {
    return apiClient.get(`/holdings/prices/refresh-jobs/${jobId}`)
  }
}
