import axios from 'axios'
import { ElNotification } from 'element-plus'
import { useAppStatusStore } from '../stores/appStatus'
import { getApiErrorMessage, normalizeApiError } from '../utils/apiErrors'

const CSRF_COOKIE_NAME = 'investment_csrf'
const SAFE_METHODS = new Set(['get', 'head', 'options'])

function getCookie(name) {
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

function maybeRenewSession(config) {
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

function getStatusStore() {
  try {
    return useAppStatusStore()
  } catch (error) {
    if (import.meta.env.DEV) {
      console.warn('Failed to access app status store:', error)
    }
    return null
  }
}

function notifyGlobalError(error) {
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
  (error) => {
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

function uploadFile(endpoint, file, fields = {}) {
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
  login(username, password) {
    return apiClient.post('/auth/login', { username, password })
  },
  logout() {
    return apiClient.post('/auth/logout')
  },
  getUserInfo() {
    return apiClient.get('/auth/me')
  },
  changePassword(oldPassword, newPassword) {
    return apiClient.put('/auth/me/password', {
      old_password: oldPassword,
      new_password: newPassword
    })
  },

  // Transactions
  getTransactions(params) {
    return apiClient.get('/transactions', { params })
  },
  getTransactionsCount(params) {
    return apiClient.get('/transactions/count', { params })
  },
  getTransaction(id) {
    return apiClient.get(`/transactions/${id}`)
  },
  createTransaction(data) {
    return apiClient.post('/transactions', data)
  },
  updateTransaction(id, data) {
    return apiClient.put(`/transactions/${id}`, data)
  },
  deleteTransaction(id) {
    return apiClient.delete(`/transactions/${id}`)
  },
  // 账户间转仓：创建 TRANSFER_OUT/TRANSFER_IN 互指交易对，成本基础跟随迁移
  createTransfer(data) {
    return apiClient.post('/transactions/transfer', data)
  },

  // Broker accounts
  getBrokerAccounts(params) {
    return apiClient.get('/broker-accounts', { params })
  },
  getBrokerAccount(id) {
    return apiClient.get(`/broker-accounts/${id}`)
  },
  createBrokerAccount(data) {
    return apiClient.post('/broker-accounts', data)
  },
  updateBrokerAccount(id, data) {
    return apiClient.put(`/broker-accounts/${id}`, data)
  },
  deleteBrokerAccount(id) {
    return apiClient.delete(`/broker-accounts/${id}`)
  },

  // Import traceability
  getImportBatches(params) {
    return apiClient.get('/import-batches', { params })
  },
  getImportBatch(id) {
    return apiClient.get(`/import-batches/${id}`)
  },

  // Account cash events
  getCashEvents(params) {
    return apiClient.get('/cash-events', { params })
  },
  getCashEvent(id) {
    return apiClient.get(`/cash-events/${id}`)
  },
  createCashEvent(data) {
    return apiClient.post('/cash-events', data)
  },
  updateCashEvent(id, data) {
    return apiClient.put(`/cash-events/${id}`, data)
  },
  deleteCashEvent(id) {
    return apiClient.delete(`/cash-events/${id}`)
  },

  // 现金管理标的排除清单：导入只归档不入账，对账比对双侧忽略
  getExcludedSecurities() {
    return apiClient.get('/excluded-securities')
  },
  createExcludedSecurity(data) {
    return apiClient.post('/excluded-securities', data)
  },
  deleteExcludedSecurity(id) {
    return apiClient.delete(`/excluded-securities/${id}`)
  },

  // Month-end reconciliation snapshots
  getReconciliationSnapshots(params) {
    return apiClient.get('/reconciliation-snapshots', { params })
  },
  getReconciliationSnapshot(id) {
    return apiClient.get(`/reconciliation-snapshots/${id}`)
  },
  createReconciliationSnapshot(data) {
    return apiClient.post('/reconciliation-snapshots', data)
  },
  updateReconciliationSnapshot(id, data) {
    return apiClient.put(`/reconciliation-snapshots/${id}`, data)
  },
  deleteReconciliationSnapshot(id) {
    return apiClient.delete(`/reconciliation-snapshots/${id}`)
  },
  // 手动触发快照自动比对（账本变化后刷新红绿状态与 diff 明细）
  compareReconciliationSnapshot(id) {
    return apiClient.post(`/reconciliation-snapshots/${id}/compare`)
  },

  // Holdings
  getHoldings(params) {
    return apiClient.get('/holdings', { params })
  },
  getHolding(symbol, market) {
    return apiClient.get(`/holdings/${symbol}`, { params: { market } })
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
  importCSV(file, brokerAccountId = null) {
    return uploadFile('/import/csv', file, { broker_account_id: brokerAccountId })
  },
  importExcel(file, brokerAccountId = null) {
    return uploadFile('/import/excel', file, { broker_account_id: brokerAccountId })
  },
  importCorporateActionsCSV(file, brokerAccountId = null) {
    return uploadFile('/import/corporate-actions/csv', file, {
      broker_account_id: brokerAccountId
    })
  },
  importCorporateActionsExcel(file, brokerAccountId = null) {
    return uploadFile('/import/corporate-actions/excel', file, {
      broker_account_id: brokerAccountId
    })
  },
  previewCmbFundFlows(file, brokerAccountId = null) {
    return uploadFile('/import/cmb-fund-flows/preview', file, {
      broker_account_id: brokerAccountId
    })
  },
  importCmbFundFlows(file, brokerAccountId = null) {
    return uploadFile('/import/cmb-fund-flows', file, {
      broker_account_id: brokerAccountId
    })
  },
  previewIbkrActivity(file, brokerAccountId = null) {
    return uploadFile('/import/ibkr-activity/preview', file, {
      broker_account_id: brokerAccountId
    })
  },
  importIbkrActivity(file, brokerAccountId = null) {
    return uploadFile('/import/ibkr-activity', file, {
      broker_account_id: brokerAccountId
    })
  },
  previewEastmoneyStatement(file, brokerAccountId = null) {
    return uploadFile('/import/eastmoney-statement/preview', file, {
      broker_account_id: brokerAccountId
    })
  },
  importEastmoneyStatement(file, brokerAccountId = null) {
    return uploadFile('/import/eastmoney-statement', file, {
      broker_account_id: brokerAccountId
    })
  },
  exportCSV() {
    return apiClient.get('/export/csv', { responseType: 'blob' })
  },
  exportExcel() {
    return apiClient.get('/export/excel', { responseType: 'blob' })
  },

  // Corporate Actions
  getCorporateActions(params) {
    return apiClient.get('/corporate-actions', { params })
  },
  getCorporateActionsCount(params) {
    return apiClient.get('/corporate-actions/count', { params })
  },
  getCorporateAction(id) {
    return apiClient.get(`/corporate-actions/${id}`)
  },
  createCorporateAction(data) {
    return apiClient.post('/corporate-actions', data)
  },
  createCashDividend(data) {
    return apiClient.post('/corporate-actions/cash-dividend', data)
  },
  createStockDividend(data) {
    return apiClient.post('/corporate-actions/stock-dividend', data)
  },
  updateCorporateAction(id, data) {
    return apiClient.put(`/corporate-actions/${id}`, data)
  },
  deleteCorporateAction(id) {
    return apiClient.delete(`/corporate-actions/${id}`)
  },
  getCorporateActionsSummary(params) {
    return apiClient.get('/corporate-actions/statistics/summary', { params })
  },

  // AI 复盘报告（LLM）
  getLlmReports() {
    return apiClient.get('/llm-reports')
  },
  getLlmReport(id) {
    return apiClient.get(`/llm-reports/${id}`)
  },
  deleteLlmReport(id) {
    return apiClient.delete(`/llm-reports/${id}`)
  },
  generateLlmReport() {
    return apiClient.post('/llm-reports/generate')
  },
  getLlmReportJob(jobId) {
    return apiClient.get(`/llm-reports/jobs/${jobId}`)
  },
  // 追问为同步 LLM 调用，单独放宽超时
  askLlmReport(id, content) {
    return apiClient.post(`/llm-reports/${id}/messages`, { content }, { timeout: 180000 })
  },
  getLlmReportSchedule() {
    return apiClient.get('/llm-reports/schedule')
  },
  updateLlmReportSchedule(cadence) {
    return apiClient.put('/llm-reports/schedule', { cadence })
  },

  // Portfolio snapshot：一次调用返回看板全量数据（表现/新鲜度/市场/近期交易/对账状态）
  getPortfolioSnapshot() {
    return apiClient.get('/statistics/portfolio-snapshot')
  },

  // Performance Statistics
  // Without prices the server values holdings from its own authority (GET);
  // passing prices is the manual what-if path (POST).
  getPerformanceSummary(currentPrices = null) {
    return currentPrices
      ? apiClient.post('/statistics/performance-summary', currentPrices)
      : apiClient.get('/statistics/performance-summary')
  },
  getPerformanceAnalytics(currentPrices = null, params = {}) {
    return currentPrices
      ? apiClient.post('/statistics/performance-analytics', currentPrices, { params })
      : apiClient.get('/statistics/performance-analytics', { params })
  },
  startPerformanceHistorySync(params = {}) {
    return apiClient.post('/statistics/performance-history-sync', null, { params })
  },
  getPerformanceHistorySyncJob(jobId) {
    return apiClient.get(`/statistics/performance-history-sync/${jobId}`)
  },

  // Exchange Rates
  getLatestRates() {
    return apiClient.get('/exchange-rates/latest')
  },
  getExchangeRates(params) {
    return apiClient.get('/exchange-rates/', { params })
  },
  getExchangeRate(fromCurrency, toCurrency) {
    return apiClient.get(`/exchange-rates/${fromCurrency}/${toCurrency}`)
  },
  createOrUpdateExchangeRate(data) {
    return apiClient.post('/exchange-rates', data)
  },
  updateExchangeRate(id, data) {
    return apiClient.put(`/exchange-rates/${id}`, data)
  },
  deleteExchangeRate(id) {
    return apiClient.delete(`/exchange-rates/${id}`)
  },
  convertCurrency(data) {
    return apiClient.post('/exchange-rates/convert', data)
  },
  refreshRatesFromAPI() {
    return apiClient.post('/exchange-rates/refresh-from-api')
  },

  // Stock Price Updates
  updateHoldingPrice(holdingId, price) {
    return apiClient.put(`/holdings/${holdingId}/price`, {
      current_price: price
    })
  },
  batchUpdatePrices(updates) {
    // updates format: [{ symbol, market, price }, ...]
    return apiClient.post('/holdings/prices/batch-update', updates)
  },
  refreshAllPrices() {
    return apiClient.post('/holdings/prices/refresh-from-api')
  },
  getPriceRefreshJob(jobId) {
    return apiClient.get(`/holdings/prices/refresh-jobs/${jobId}`)
  },

  // User Management (Admin)
  getUsers() {
    return apiClient.get('/users')
  },
  createUser(userData) {
    return apiClient.post('/users', userData)
  },
  updateUser(userId, userData) {
    return apiClient.put(`/users/${userId}`, userData)
  },
  deleteUser(userId) {
    return apiClient.delete(`/users/${userId}`)
  },
  resetUserPassword(userId, newPassword) {
    return apiClient.put(`/users/${userId}/password`, {
      new_password: newPassword
    })
  },

  // Admin Holdings
  getAllHoldingsAdmin() {
    return apiClient.get('/holdings/admin/all')
  },
  getUserHoldingsAdmin(userId) {
    return apiClient.get(`/holdings/admin/users/${userId}`)
  }
}

export default api
