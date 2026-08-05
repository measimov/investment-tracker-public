import axios, { type AxiosError, type AxiosRequestConfig } from 'axios'
import { ElNotification } from 'element-plus'
import { useAppStatusStore } from '../stores/appStatus'
import { getApiErrorMessage, normalizeApiError, type NormalizedApiError } from '../utils/apiErrors'

const CSRF_COOKIE_NAME = 'investment_csrf'
const SAFE_METHODS = new Set(['get', 'head', 'options'])

function getCookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`
  const item = document.cookie.split('; ').find((value) => value.startsWith(prefix))
  return item ? decodeURIComponent(item.slice(prefix.length)) : null
}

let lastGlobalErrorKey = ''
let lastGlobalErrorAt = 0

// 滑动会话续期：有 API 活动时每隔一段时间静默轮换会话 Cookie，
// 活跃用户不会被 30 分钟令牌过期打断；闲置用户按原有效期自然登出。
const SESSION_RENEW_INTERVAL_MS = 10 * 60 * 1000
let lastSessionRenewAt = 0

function maybeRenewSession(config: AxiosRequestConfig | undefined): void {
  const url = config?.url || ''
  if (url.startsWith('/auth/')) {
    // 登录/续期本身就是最新会话，登出后无会话可续
    if (url === '/auth/login' || url === '/auth/refresh') {
      lastSessionRenewAt = Date.now()
    }
    return
  }
  if (!localStorage.getItem('user')) return
  if (Date.now() - lastSessionRenewAt < SESSION_RENEW_INTERVAL_MS) return
  // 先记时间戳：失败也等满间隔再试，避免后端异常时逐请求重试
  lastSessionRenewAt = Date.now()
  apiClient.post('/auth/refresh', null, { skipGlobalErrorNotification: true }).catch(() => {
    // 静默失败：会话真过期时常规 401 流程会接管
  })
}

function getStatusStore(): ReturnType<typeof useAppStatusStore> | null {
  try {
    return useAppStatusStore()
  } catch (error) {
    if (import.meta.env.DEV) {
      console.warn('Failed to access app status store:', error)
    }
    return null
  }
}

function notifyGlobalError(error: NormalizedApiError): void {
  if (error.config?.skipGlobalErrorNotification) return

  const status = error.response?.status
  const shouldNotify =
    !error.response ||
    error.code === 'ECONNABORTED' ||
    status === 403 ||
    status === 500 ||
    status === 503

  if (!shouldNotify) return

  const message = getApiErrorMessage(error)
  const key = `${status || error.code || 'network'}:${message}`
  const now = Date.now()
  if (key === lastGlobalErrorKey && now - lastGlobalErrorAt < 5000) return

  lastGlobalErrorKey = key
  lastGlobalErrorAt = now

  ElNotification.error({
    title: status === 503 ? '服务不可用' : '请求失败',
    message,
    duration: 4500
  })
}

const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_URL || '/api',
  timeout: 120000, // Increased to 120 seconds for batch operations
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json'
  }
})

// Add request interceptor for authentication and logging
apiClient.interceptors.request.use(
  (config) => {
    config.metadata = {
      ...config.metadata,
      startedAt: Date.now()
    }

    const method = config.method?.toLowerCase() || 'get'
    if (!SAFE_METHODS.has(method)) {
      const csrfToken = getCookie(CSRF_COOKIE_NAME)
      if (csrfToken) {
        config.headers['X-CSRF-Token'] = csrfToken
      }
    }

    if (import.meta.env.DEV && (config.url?.includes('refresh') || config.url?.includes('batch'))) {
      console.log(`[API Request] ${config.method?.toUpperCase()} ${config.url}`)
    }
    return config
  },
  (error) => {
    return Promise.reject(error)
  }
)

// Add response interceptor for better error handling and authentication
apiClient.interceptors.response.use(
  (response) => {
    const statusStore = getStatusStore()
    if (statusStore?.shouldClearForRequest(response.config?.metadata?.startedAt)) {
      statusStore.clear()
    }
    maybeRenewSession(response.config)
    return response
  },
  (error: AxiosError) => {
    const normalizedError = normalizeApiError(error)
    const statusStore = getStatusStore()

    // Handle 401 Unauthorized - clear auth and redirect to login
    if (normalizedError.response?.status === 401) {
      // Clear authentication
      localStorage.removeItem('user')

      // Redirect to login page if not already there
      if (window.location.pathname !== '/login') {
        window.location.href = '/login'
      }
    }

    if (!normalizedError.response || normalizedError.code === 'ECONNABORTED') {
      statusStore?.markConnectionLost(normalizedError.userMessage)
    } else if (normalizedError.response.status === 503) {
      statusStore?.markMaintenance(normalizedError.userMessage)
    }

    notifyGlobalError(normalizedError)
    return Promise.reject(normalizedError)
  }
)

type QueryParams = Record<string, unknown>
type RequestData = Record<string, unknown>
type UploadFields = Record<string, string | number | null | undefined>

function uploadFile(endpoint: string, file: File | Blob, fields: UploadFields = {}) {
  const formData = new FormData()
  formData.append('file', file)
  Object.entries(fields).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== '') {
      formData.append(key, String(value))
    }
  })
  return apiClient.post(endpoint, formData, {
    headers: { 'Content-Type': 'multipart/form-data' }
  })
}

const api = {
  // Authentication
  login(username: string, password: string) {
    return apiClient.post('/auth/login', { username, password })
  },
  logout() {
    return apiClient.post('/auth/logout')
  },
  getUserInfo() {
    return apiClient.get('/auth/me')
  },
  // intentionally unused：后端 PUT /auth/me/password 为真实端点，改密 UI 尚未内建
  changePassword(oldPassword: string, newPassword: string) {
    return apiClient.put('/auth/me/password', {
      old_password: oldPassword,
      new_password: newPassword
    })
  },

  // Transactions
  getTransactions(params?: QueryParams) {
    return apiClient.get('/transactions', { params })
  },
  getTransactionsCount(params?: QueryParams) {
    return apiClient.get('/transactions/count', { params })
  },
  createTransaction(data: RequestData) {
    return apiClient.post('/transactions', data)
  },
  updateTransaction(id: number | string, data: RequestData) {
    return apiClient.put(`/transactions/${id}`, data)
  },
  deleteTransaction(id: number | string) {
    return apiClient.delete(`/transactions/${id}`)
  },
  // 账户间转仓：创建 TRANSFER_OUT/TRANSFER_IN 互指交易对，成本基础跟随迁移
  createTransfer(data: RequestData) {
    return apiClient.post('/transactions/transfer', data)
  },

  // Broker accounts
  getBrokerAccounts(params?: QueryParams) {
    return apiClient.get('/broker-accounts', { params })
  },
  createBrokerAccount(data: RequestData) {
    return apiClient.post('/broker-accounts', data)
  },
  updateBrokerAccount(id: number | string, data: RequestData) {
    return apiClient.put(`/broker-accounts/${id}`, data)
  },
  deleteBrokerAccount(id: number | string) {
    return apiClient.delete(`/broker-accounts/${id}`)
  },

  // Import traceability
  getImportBatches(params?: QueryParams) {
    return apiClient.get('/import-batches', { params })
  },
  getImportBatch(id: number | string) {
    return apiClient.get(`/import-batches/${id}`)
  },

  // Account cash events
  getCashEvents(params?: QueryParams) {
    return apiClient.get('/cash-events', { params })
  },
  createCashEvent(data: RequestData) {
    return apiClient.post('/cash-events', data)
  },
  updateCashEvent(id: number | string, data: RequestData) {
    return apiClient.put(`/cash-events/${id}`, data)
  },
  deleteCashEvent(id: number | string) {
    return apiClient.delete(`/cash-events/${id}`)
  },

  // 现金管理标的排除清单：导入只归档不入账，对账比对双侧忽略
  // （EXCLUDE 类型特例规则的兼容门面，UI 已迁移到 security-rules）
  getExcludedSecurities() {
    return apiClient.get('/excluded-securities')
  },
  createExcludedSecurity(data: RequestData) {
    return apiClient.post('/excluded-securities', data)
  },
  deleteExcludedSecurity(id: number | string) {
    return apiClient.delete(`/excluded-securities/${id}`)
  },

  // 账本特例规则（issue #82）：排除/现金管理/转板映射/名称覆盖/行情缺口豁免/招商现金业务
  getSecurityRules(params?: QueryParams) {
    return apiClient.get('/security-rules', { params })
  },
  createSecurityRule(data: RequestData) {
    return apiClient.post('/security-rules', data)
  },
  deleteSecurityRule(id: number | string) {
    return apiClient.delete(`/security-rules/${id}`)
  },

  // Month-end reconciliation snapshots
  getReconciliationSnapshots(params?: QueryParams) {
    return apiClient.get('/reconciliation-snapshots', { params })
  },
  createReconciliationSnapshot(data: RequestData) {
    return apiClient.post('/reconciliation-snapshots', data)
  },
  updateReconciliationSnapshot(id: number | string, data: RequestData) {
    return apiClient.put(`/reconciliation-snapshots/${id}`, data)
  },
  deleteReconciliationSnapshot(id: number | string) {
    return apiClient.delete(`/reconciliation-snapshots/${id}`)
  },
  // 手动触发快照自动比对（账本变化后刷新红绿状态与 diff 明细）
  compareReconciliationSnapshot(id: number | string) {
    return apiClient.post(`/reconciliation-snapshots/${id}/compare`)
  },

  // Holdings
  getHoldings(params?: QueryParams) {
    return apiClient.get('/holdings', { params })
  },

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

  // Import/Export
  importCSV(file: File | Blob, brokerAccountId: number | string | null = null) {
    return uploadFile('/import/csv', file, { broker_account_id: brokerAccountId })
  },
  importExcel(file: File | Blob, brokerAccountId: number | string | null = null) {
    return uploadFile('/import/excel', file, { broker_account_id: brokerAccountId })
  },
  importCorporateActionsCSV(file: File | Blob, brokerAccountId: number | string | null = null) {
    return uploadFile('/import/corporate-actions/csv', file, {
      broker_account_id: brokerAccountId
    })
  },
  importCorporateActionsExcel(file: File | Blob, brokerAccountId: number | string | null = null) {
    return uploadFile('/import/corporate-actions/excel', file, {
      broker_account_id: brokerAccountId
    })
  },
  previewCmbFundFlows(file: File | Blob, brokerAccountId: number | string | null = null) {
    return uploadFile('/import/cmb-fund-flows/preview', file, {
      broker_account_id: brokerAccountId
    })
  },
  importCmbFundFlows(file: File | Blob, brokerAccountId: number | string | null = null) {
    return uploadFile('/import/cmb-fund-flows', file, {
      broker_account_id: brokerAccountId
    })
  },
  previewIbkrActivity(file: File | Blob, brokerAccountId: number | string | null = null) {
    return uploadFile('/import/ibkr-activity/preview', file, {
      broker_account_id: brokerAccountId
    })
  },
  importIbkrActivity(file: File | Blob, brokerAccountId: number | string | null = null) {
    return uploadFile('/import/ibkr-activity', file, {
      broker_account_id: brokerAccountId
    })
  },
  previewEastmoneyStatement(file: File | Blob, brokerAccountId: number | string | null = null) {
    return uploadFile('/import/eastmoney-statement/preview', file, {
      broker_account_id: brokerAccountId
    })
  },
  importEastmoneyStatement(file: File | Blob, brokerAccountId: number | string | null = null) {
    return uploadFile('/import/eastmoney-statement', file, {
      broker_account_id: brokerAccountId
    })
  },
  // intentionally unused：后端 CSV 导出为真实端点，前端当前只用 Excel 导出
  exportCSV() {
    return apiClient.get('/export/csv', { responseType: 'blob' })
  },
  exportExcel() {
    return apiClient.get('/export/excel', { responseType: 'blob' })
  },

  // Corporate Actions
  getCorporateActions(params?: QueryParams) {
    return apiClient.get('/corporate-actions', { params })
  },
  getCorporateActionsCount(params?: QueryParams) {
    return apiClient.get('/corporate-actions/count', { params })
  },
  createCorporateAction(data: RequestData) {
    return apiClient.post('/corporate-actions', data)
  },
  updateCorporateAction(id: number | string, data: RequestData) {
    return apiClient.put(`/corporate-actions/${id}`, data)
  },
  deleteCorporateAction(id: number | string) {
    return apiClient.delete(`/corporate-actions/${id}`)
  },
  getCorporateActionsSummary(params?: QueryParams) {
    return apiClient.get('/corporate-actions/statistics/summary', { params })
  },

  // 分红公告建议（Tushare 同步；仅 A/B 股）与标的事件
  startDividendSyncJob() {
    return apiClient.post('/corporate-actions/dividend-sync-jobs')
  },
  getDividendSyncJob(id: string) {
    return apiClient.get(`/corporate-actions/dividend-sync-jobs/${id}`)
  },
  listDividendSuggestions(params?: QueryParams) {
    return apiClient.get('/corporate-actions/suggestions', { params })
  },
  countDividendSuggestions() {
    return apiClient.get('/corporate-actions/suggestions/count')
  },
  acceptDividendSuggestion(id: number | string, data: RequestData = {}) {
    return apiClient.post(`/corporate-actions/suggestions/${id}/accept`, data)
  },
  ignoreDividendSuggestion(id: number | string) {
    return apiClient.post(`/corporate-actions/suggestions/${id}/ignore`)
  },
  restoreDividendSuggestion(id: number | string) {
    return apiClient.post(`/corporate-actions/suggestions/${id}/restore`)
  },
  getSecurityEvents(params?: QueryParams) {
    return apiClient.get('/corporate-actions/security-events', { params })
  },

  // 标的档案（基本面数据 + LLM 分析；A股/美股/港股）
  listSecurityAnalyses() {
    return apiClient.get('/securities/analyses')
  },
  getSecurityAnalysis(market: string, symbol: string) {
    return apiClient.get(
      `/securities/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}/analysis`
    )
  },
  getSecurityProfile(market: string, symbol: string) {
    return apiClient.get(
      `/securities/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}/profile`
    )
  },
  startSecurityAnalysisJob(market: string, symbol: string) {
    return apiClient.post(
      `/securities/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}/analysis-jobs`
    )
  },
  getSecurityAnalysisJob(id: string) {
    return apiClient.get(`/securities/analysis-jobs/${id}`)
  },
  getReportSections(market: string, symbol: string) {
    return apiClient.get(
      `/securities/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}/report-sections`
    )
  },
  startReportBackfillJob(market: string, symbol: string) {
    return apiClient.post(
      `/securities/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}/report-backfill-jobs`
    )
  },
  // 批量分析（持仓页一键分析；每用户单活跃任务，可能运行数十分钟到数小时）
  startSecurityAnalysisBatchJob(params?: QueryParams) {
    return apiClient.post('/securities/analysis-batch-jobs', null, { params })
  },
  // 目标预览：确认框的数量/耗时估算必须与后端真实目标一致
  getSecurityAnalysisBatchTargets() {
    return apiClient.get('/securities/analysis-batch-targets')
  },
  getSecurityAnalysisBatchJob(jobId: string) {
    return apiClient.get(`/securities/analysis-batch-jobs/${jobId}`)
  },
  cancelSecurityAnalysisBatchJob(jobId: string) {
    return apiClient.post(`/securities/analysis-batch-jobs/${jobId}/cancel`)
  },
  // 无活跃任务时后端返回 200 + 空数组（刷新页面后恢复进度显示用）
  listActiveAnalysisJobs() {
    return apiClient.get('/securities/active-analysis-jobs')
  },
  getReportBackfillJob(id: string) {
    return apiClient.get(`/securities/report-backfill-jobs/${id}`)
  },
  // 批量财报摘要回填（持仓页：一次给全部持仓补摘要，可重复触发续跑加深）
  getDigestBackfillPreview() {
    return apiClient.get('/securities/digest-backfill-preview')
  },
  startDigestBackfillJob() {
    return apiClient.post('/securities/digest-backfill-jobs')
  },
  getDigestBackfillJob(jobId: string) {
    return apiClient.get(`/securities/digest-backfill-jobs/${jobId}`)
  },
  cancelDigestBackfillJob(jobId: string) {
    return apiClient.post(`/securities/digest-backfill-jobs/${jobId}/cancel`)
  },

  // AI 复盘报告（LLM）
  getLlmReports() {
    return apiClient.get('/llm-reports')
  },
  getLlmReport(id: number | string) {
    return apiClient.get(`/llm-reports/${id}`)
  },
  deleteLlmReport(id: number | string) {
    return apiClient.delete(`/llm-reports/${id}`)
  },
  generateLlmReport() {
    return apiClient.post('/llm-reports/generate')
  },
  getLlmReportJob(jobId: number | string) {
    return apiClient.get(`/llm-reports/jobs/${jobId}`)
  },
  // 追问为同步 LLM 调用，单独放宽超时
  askLlmReport(id: number | string, content: string) {
    return apiClient.post(`/llm-reports/${id}/messages`, { content }, { timeout: 180000 })
  },
  getLlmReportSchedule() {
    return apiClient.get('/llm-reports/schedule')
  },
  updateLlmReportSchedule(cadence: string) {
    return apiClient.put('/llm-reports/schedule', { cadence })
  },

  // Portfolio snapshot：一次调用返回看板全量数据（表现/新鲜度/市场/近期交易/对账状态）
  getPortfolioSnapshot() {
    return apiClient.get('/statistics/portfolio-snapshot')
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
  },

  // Exchange Rates
  getLatestRates() {
    return apiClient.get('/exchange-rates/latest')
  },
  getExchangeRates(params?: QueryParams) {
    return apiClient.get('/exchange-rates/', { params })
  },
  createOrUpdateExchangeRate(data: RequestData) {
    return apiClient.post('/exchange-rates', data)
  },
  updateExchangeRate(id: number | string, data: RequestData) {
    return apiClient.put(`/exchange-rates/${id}`, data)
  },
  deleteExchangeRate(id: number | string) {
    return apiClient.delete(`/exchange-rates/${id}`)
  },
  refreshRatesFromAPI() {
    return apiClient.post('/exchange-rates/refresh-from-api')
  },

  // Stock Price Updates
  updateHoldingPrice(holdingId: number | string, price: number | string) {
    return apiClient.put(`/holdings/${holdingId}/price`, {
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
  },

  // User Management (Admin)
  getUsers() {
    return apiClient.get('/users')
  },
  createUser(userData: RequestData) {
    return apiClient.post('/users', userData)
  },
  updateUser(userId: number | string, userData: RequestData) {
    return apiClient.put(`/users/${userId}`, userData)
  },
  deleteUser(userId: number | string) {
    return apiClient.delete(`/users/${userId}`)
  },
  resetUserPassword(userId: number | string, newPassword: string) {
    return apiClient.put(`/users/${userId}/password`, {
      new_password: newPassword
    })
  },

  // Admin Holdings
  getAllHoldingsAdmin() {
    return apiClient.get('/holdings/admin/all')
  },
  getUserHoldingsAdmin(userId: number | string) {
    return apiClient.get(`/holdings/admin/users/${userId}`)
  }
}

export default api
