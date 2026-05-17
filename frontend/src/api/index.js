import axios from 'axios'

const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_URL || '/api',
  timeout: 120000, // Increased to 120 seconds for batch operations
  headers: {
    'Content-Type': 'application/json'
  }
})

// Add request interceptor for authentication and logging
apiClient.interceptors.request.use(
  (config) => {
    // Add Authorization header if token exists
    const token = localStorage.getItem('token')
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }

    if (import.meta.env.DEV && (config.url.includes('refresh') || config.url.includes('batch'))) {
      console.log(`[API Request] ${config.method.toUpperCase()} ${config.url}`)
    }
    return config
  },
  (error) => {
    return Promise.reject(error)
  }
)

// Add response interceptor for better error handling and authentication
apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    // Handle 401 Unauthorized - clear auth and redirect to login
    if (error.response?.status === 401) {
      // Clear authentication
      localStorage.removeItem('token')
      localStorage.removeItem('user')

      // Redirect to login page if not already there
      if (window.location.pathname !== '/login') {
        window.location.href = '/login'
      }
    }

    // Better error messages
    if (error.code === 'ECONNABORTED') {
      error.message = '请求超时，请稍后重试'
    } else if (!error.response) {
      error.message = '网络连接失败，请检查网络'
    } else if (error.response.status >= 500) {
      error.message = '服务器错误，请稍后重试'
    }
    return Promise.reject(error)
  }
)

export default {
  // Authentication
  login(username, password) {
    return apiClient.post('/auth/login', { username, password })
  },
  getUserInfo() {
    return apiClient.get('/auth/me')
  },
  changePassword(oldPassword, newPassword) {
    return apiClient.post('/auth/change-password', {
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
  getProfitLoss() {
    return apiClient.get('/statistics/profit-loss')
  },

  // Import/Export
  importCSV(file) {
    const formData = new FormData()
    formData.append('file', file)
    return apiClient.post('/import/csv', formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    })
  },
  importExcel(file) {
    const formData = new FormData()
    formData.append('file', file)
    return apiClient.post('/import/excel', formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    })
  },
  previewCmbFundFlows(file) {
    const formData = new FormData()
    formData.append('file', file)
    return apiClient.post('/import/cmb-fund-flows/preview', formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    })
  },
  importCmbFundFlows(file) {
    const formData = new FormData()
    formData.append('file', file)
    return apiClient.post('/import/cmb-fund-flows', formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    })
  },
  previewIbkrActivity(file) {
    const formData = new FormData()
    formData.append('file', file)
    return apiClient.post('/import/ibkr-activity/preview', formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    })
  },
  importIbkrActivity(file) {
    const formData = new FormData()
    formData.append('file', file)
    return apiClient.post('/import/ibkr-activity', formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    })
  },
  previewEastmoneyStatement(file) {
    const formData = new FormData()
    formData.append('file', file)
    return apiClient.post('/import/eastmoney-statement/preview', formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    })
  },
  importEastmoneyStatement(file) {
    const formData = new FormData()
    formData.append('file', file)
    return apiClient.post('/import/eastmoney-statement', formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
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

  // Performance Statistics (New)
  getCurrentHoldingsPerformance(currentPrices) {
    return apiClient.post('/statistics/current-holdings-performance', currentPrices)
  },
  getRealizedPnLFifo() {
    return apiClient.get('/statistics/realized-pnl-fifo')
  },
  getDividendSummary() {
    return apiClient.get('/statistics/dividend-summary')
  },
  getTotalRealizedReturn() {
    return apiClient.get('/statistics/total-realized-return')
  },
  getAccountTotalReturn(currentPrices) {
    return apiClient.post('/statistics/account-total-return', currentPrices)
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
