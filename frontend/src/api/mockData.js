// In-memory data store for Frontend Mock Mode
import { jwtDecode } from 'jwt-decode'

// Helper function to safely base64url encode
const base64UrlEncode = (obj) => {
  const str = JSON.stringify(obj)
  return btoa(unescape(encodeURIComponent(str)))
    .replace(/=/g, '')
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
}

// Initial Mock Users (password field for login validation)
const users = [
  { id: '1', username: 'admin', password: 'admin123', is_admin: true, email: 'admin@example.com', name: '超级管理员', is_active: true, created_at: new Date(Date.now() - 30 * 24 * 3600 * 1000).toISOString() },
  { id: '2', username: 'measimov', password: 'user123', is_admin: false, email: 'measimov@example.com', name: 'Measimov', is_active: true, created_at: new Date(Date.now() - 15 * 24 * 3600 * 1000).toISOString() },
  { id: '3', username: 'demo', password: 'demo123', is_admin: false, email: 'demo@example.com', name: '演示用户', is_active: true, created_at: new Date(Date.now() - 5 * 24 * 3600 * 1000).toISOString() }
]

// Initial Exchange Rates (Relative to CNY)
const exchangeRates = {
  CNY: 1.0,
  USD: 7.24,
  HKD: 0.925,
  SGD: 5.38
}

// Initial Exchange Rates History
const exchangeRatesHistory = [
  { id: 'er-1', from_currency: 'USD', to_currency: 'CNY', rate: 7.2400, effective_date: new Date().toISOString().split('T')[0], source: 'api', is_active: true, created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
  { id: 'er-2', from_currency: 'HKD', to_currency: 'CNY', rate: 0.9250, effective_date: new Date().toISOString().split('T')[0], source: 'api', is_active: true, created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
  { id: 'er-3', from_currency: 'SGD', to_currency: 'CNY', rate: 5.3800, effective_date: new Date().toISOString().split('T')[0], source: 'api', is_active: true, created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
  { id: 'er-4', from_currency: 'USD', to_currency: 'CNY', rate: 7.2100, effective_date: new Date(Date.now() - 24 * 3600 * 1000).toISOString().split('T')[0], source: 'api', is_active: true, created_at: new Date(Date.now() - 24 * 3600 * 1000).toISOString(), updated_at: new Date(Date.now() - 24 * 3600 * 1000).toISOString() },
  { id: 'er-5', from_currency: 'HKD', to_currency: 'CNY', rate: 0.9210, effective_date: new Date(Date.now() - 24 * 3600 * 1000).toISOString().split('T')[0], source: 'api', is_active: true, created_at: new Date(Date.now() - 24 * 3600 * 1000).toISOString(), updated_at: new Date(Date.now() - 24 * 3600 * 1000).toISOString() }
]

// Initial Mock Transactions (owner_id links to user)
let transactions = [
  // Admin's portfolio (US/HK/A-share mixed)
  { id: 't1', owner_id: '1', symbol: 'AAPL', name: '苹果公司', market: '美股', transaction_type: 'BUY', quantity: 100, price: 175.20, fee: 1.50, transaction_date: '2024-01-15', currency: 'USD', notes: '首笔定投' },
  { id: 't2', owner_id: '1', symbol: 'NVDA', name: '英伟达', market: '美股', transaction_type: 'BUY', quantity: 20, price: 450.00, fee: 2.00, transaction_date: '2024-02-10', currency: 'USD', notes: 'AI龙头入场' },
  { id: 't3', owner_id: '1', symbol: '00700', name: '腾讯控股', market: '港股', transaction_type: 'BUY', quantity: 500, price: 290.00, fee: 50.00, transaction_date: '2024-03-05', currency: 'HKD', notes: '估值回落分批买入' },
  { id: 't4', owner_id: '1', symbol: '600519', name: '贵州茅台', market: 'A股', transaction_type: 'BUY', quantity: 100, price: 1650.00, fee: 15.00, transaction_date: '2024-03-12', currency: 'CNY', notes: '核心资产' },
  { id: 't5', owner_id: '1', symbol: 'MSFT', name: '微软', market: '美股', transaction_type: 'BUY', quantity: 80, price: 380.00, fee: 1.20, transaction_date: '2024-04-01', currency: 'USD', notes: 'AI与云计算强劲' },
  { id: 't6', owner_id: '1', symbol: 'AAPL', name: '苹果公司', market: '美股', transaction_type: 'SELL', quantity: 30, price: 190.50, fee: 1.50, transaction_date: '2024-05-10', currency: 'USD', notes: '高点部分止盈' },
  { id: 't7', owner_id: '1', symbol: 'TSLA', name: '特斯拉', market: '美股', transaction_type: 'BUY', quantity: 50, price: 180.00, fee: 1.00, transaction_date: '2024-05-15', currency: 'USD', notes: '低位左侧建仓' },
  // Measimov's portfolio (A-share focused retail investor)
  { id: 't8', owner_id: '2', symbol: '000858', name: '五粮液', market: 'A股', transaction_type: 'BUY', quantity: 200, price: 148.50, fee: 8.90, transaction_date: '2024-02-20', currency: 'CNY', notes: '白酒龙头逢低布局' },
  { id: 't9', owner_id: '2', symbol: '601318', name: '中国平安', market: 'A股', transaction_type: 'BUY', quantity: 500, price: 42.80, fee: 6.42, transaction_date: '2024-03-01', currency: 'CNY', notes: '保险板块低估' },
  { id: 't10', owner_id: '2', symbol: '300750', name: '宁德时代', market: 'A股', transaction_type: 'BUY', quantity: 50, price: 195.00, fee: 2.93, transaction_date: '2024-03-18', currency: 'CNY', notes: '新能源长期看好' },
  { id: 't11', owner_id: '2', symbol: 'AAPL', name: '苹果公司', market: '美股', transaction_type: 'BUY', quantity: 15, price: 182.50, fee: 1.00, transaction_date: '2024-04-10', currency: 'USD', notes: '小仓位配置美股' },
  { id: 't12', owner_id: '2', symbol: '000858', name: '五粮液', market: 'A股', transaction_type: 'SELL', quantity: 100, price: 162.30, fee: 4.87, transaction_date: '2024-05-08', currency: 'CNY', notes: '高位减仓一半' }
]

// Initial Mock Holdings (owner_id links to user)
let holdings = [
  // Admin's holdings
  { id: 'h1', owner_id: '1', symbol: 'AAPL', name: '苹果公司', market: '美股', quantity: 70, avg_cost: 175.20, total_cost: 12264.00, current_price: 189.84, currency: 'USD', updated_at: new Date().toISOString() },
  { id: 'h2', owner_id: '1', symbol: 'NVDA', name: '英伟达', market: '美股', quantity: 20, avg_cost: 450.00, total_cost: 9000.00, current_price: 950.02, currency: 'USD', updated_at: new Date().toISOString() },
  { id: 'h3', owner_id: '1', symbol: '00700', name: '腾讯控股', market: '港股', quantity: 500, avg_cost: 290.00, total_cost: 145000.00, current_price: 382.40, currency: 'HKD', updated_at: new Date().toISOString() },
  { id: 'h4', owner_id: '1', symbol: '600519', name: '贵州茅台', market: 'A股', quantity: 100, avg_cost: 1650.00, total_cost: 165000.00, current_price: 1720.00, currency: 'CNY', updated_at: new Date().toISOString() },
  { id: 'h5', owner_id: '1', symbol: 'MSFT', name: '微软', market: '美股', quantity: 80, avg_cost: 380.00, total_cost: 30400.00, current_price: 421.90, currency: 'USD', updated_at: new Date().toISOString() },
  { id: 'h6', owner_id: '1', symbol: 'TSLA', name: '特斯拉', market: '美股', quantity: 50, avg_cost: 180.00, total_cost: 9000.00, current_price: 174.60, currency: 'USD', updated_at: new Date().toISOString() },
  // Measimov's holdings
  { id: 'h7', owner_id: '2', symbol: '000858', name: '五粮液', market: 'A股', quantity: 100, avg_cost: 148.50, total_cost: 14850.00, current_price: 158.20, currency: 'CNY', updated_at: new Date().toISOString() },
  { id: 'h8', owner_id: '2', symbol: '601318', name: '中国平安', market: 'A股', quantity: 500, avg_cost: 42.80, total_cost: 21400.00, current_price: 48.65, currency: 'CNY', updated_at: new Date().toISOString() },
  { id: 'h9', owner_id: '2', symbol: '300750', name: '宁德时代', market: 'A股', quantity: 50, avg_cost: 195.00, total_cost: 9750.00, current_price: 218.40, currency: 'CNY', updated_at: new Date().toISOString() },
  { id: 'h10', owner_id: '2', symbol: 'AAPL', name: '苹果公司', market: '美股', quantity: 15, avg_cost: 182.50, total_cost: 2737.50, current_price: 189.84, currency: 'USD', updated_at: new Date().toISOString() }
]

// Initial Mock Corporate Actions (owner_id links to user)
let corporateActions = [
  // Admin's corporate actions
  { id: 'ca1', owner_id: '1', symbol: 'AAPL', name: '苹果公司', market: '美股', action_type: 'CASH_DIVIDEND', ex_date: '2024-02-15', dividend_per_share: 0.24, total_dividend: 24.00, net_dividend: 21.60, tax_rate: 0.10, currency: 'USD', notes: '2024年第一季度分红' },
  { id: 'ca2', owner_id: '1', symbol: '00700', name: '腾讯控股', market: '港股', action_type: 'CASH_DIVIDEND', ex_date: '2024-05-20', dividend_per_share: 3.40, total_dividend: 1700.00, net_dividend: 1530.00, tax_rate: 0.10, currency: 'HKD', notes: '2023年度末期分红' },
  { id: 'ca3', owner_id: '1', symbol: 'NVDA', name: '英伟达', market: '美股', action_type: 'STOCK_SPLIT', ex_date: '2024-06-07', split_ratio: '1:10', currency: 'USD', notes: '1拆10拆股' },
  // Measimov's corporate actions
  { id: 'ca4', owner_id: '2', symbol: '000858', name: '五粮液', market: 'A股', action_type: 'CASH_DIVIDEND', ex_date: '2024-06-20', dividend_per_share: 5.16, total_dividend: 1032.00, net_dividend: 928.80, tax_rate: 0.10, currency: 'CNY', notes: '2023年度分红 (持有>1年免税)' },
  { id: 'ca5', owner_id: '2', symbol: '601318', name: '中国平安', market: 'A股', action_type: 'CASH_DIVIDEND', ex_date: '2024-07-15', dividend_per_share: 2.42, total_dividend: 1210.00, net_dividend: 1089.00, tax_rate: 0.10, currency: 'CNY', notes: '2023年度末期分红' }
]

// Helper: Convert to CNY
const convertToCNY = (amount, currency) => {
  if (!currency || currency === 'CNY') return amount
  const rate = exchangeRates[currency]
  if (!rate) return amount
  return amount * rate
}

// Helper: Get current logged-in user's ID from localStorage
const getCurrentUserId = () => {
  try {
    const storedUser = localStorage.getItem('user')
    if (storedUser) {
      return JSON.parse(storedUser).id
    }
  } catch (e) { /* ignore */ }
  return '1' // default to admin
}

// Helper: Get user-scoped holdings
const getUserHoldings = () => holdings.filter(h => h.owner_id === getCurrentUserId())

// Helper: Get user-scoped transactions
const getUserTransactions = () => transactions.filter(t => t.owner_id === getCurrentUserId())

// Helper: Get user-scoped corporate actions
const getUserCorporateActions = () => corporateActions.filter(ca => ca.owner_id === getCurrentUserId())

// Helper: Sync holdings list based on transactions list (scoped to current user)
const syncHoldingsFromTransactions = () => {
  const userId = getCurrentUserId()
  const userTxns = transactions.filter(t => t.owner_id === userId)
  const groups = {}
  userTxns.forEach(t => {
    const key = `${t.market}-${t.symbol}`
    if (!groups[key]) {
      groups[key] = {
        symbol: t.symbol,
        name: t.name || t.symbol,
        market: t.market,
        currency: t.currency,
        buys: [],
        sells: []
      }
    }
    if (t.transaction_type === 'BUY') {
      groups[key].buys.push(t)
    } else {
      groups[key].sells.push(t)
    }
  })

  const newUserHoldings = []
  Object.values(groups).forEach(g => {
    g.buys.sort((a, b) => new Date(a.transaction_date) - new Date(b.transaction_date))
    g.sells.sort((a, b) => new Date(a.transaction_date) - new Date(b.transaction_date))

    let totalQuantity = 0
    let totalCost = 0

    g.buys.forEach(b => {
      totalQuantity += b.quantity
      totalCost += b.quantity * b.price + b.fee
    })

    g.sells.forEach(s => {
      if (totalQuantity > 0) {
        const avgPrice = totalCost / totalQuantity
        totalQuantity -= s.quantity
        totalCost = totalQuantity * avgPrice
      }
    })

    if (totalQuantity > 0) {
      const oldHolding = holdings.find(h => h.symbol === g.symbol && h.owner_id === userId)
      const currentPrice = oldHolding ? oldHolding.current_price : g.buys[g.buys.length - 1].price * 1.1

      newUserHoldings.push({
        id: oldHolding ? oldHolding.id : `h-${g.symbol}-${userId}`,
        owner_id: userId,
        symbol: g.symbol,
        name: g.name,
        market: g.market,
        quantity: totalQuantity,
        avg_cost: totalCost / totalQuantity,
        total_cost: totalCost,
        current_price: currentPrice,
        currency: g.currency,
        updated_at: oldHolding ? oldHolding.updated_at : new Date().toISOString()
      })
    }
  })

  // Replace only current user's holdings, keep other users' intact
  holdings = holdings.filter(h => h.owner_id !== userId).concat(newUserHoldings)
}

// In-Memory API Handlers
export const handlers = {
  // Authentication
  login(username, password) {
    const user = users.find(u => u.username === username.toLowerCase())
    if (!user) {
      return Promise.reject({ response: { status: 401, data: { detail: '用户名不存在' } } })
    }
    if (user.password !== password) {
      return Promise.reject({ response: { status: 401, data: { detail: '密码错误' } } })
    }
    const payload = {
      sub: user.username,
      is_admin: user.is_admin,
      exp: Math.floor(Date.now() / 1000) + 3600 * 24 * 365 // 1 Year Token
    }
    const token = `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.${base64UrlEncode(payload)}.signature`
    
    // Store user session info (strip password for security)
    const { password: _, ...safeUser } = user
    localStorage.setItem('user', JSON.stringify(safeUser))
    
    return Promise.resolve({
      data: { access_token: token }
    })
  },
  
  getUserInfo() {
    const storedUser = localStorage.getItem('user')
    const user = storedUser ? JSON.parse(storedUser) : users[0]
    // Ensure no password leaks
    const { password: _, ...safeUser } = user
    return Promise.resolve({ data: safeUser })
  },
  
  changePassword() {
    return Promise.resolve({ data: { message: '密码修改成功（Mock）' } })
  },

  // Transactions CRUD
  getTransactions(params = {}) {
    let list = getUserTransactions()
    
    // Filter
    if (params.symbol) {
      list = list.filter(t => t.symbol.toLowerCase().includes(params.symbol.toLowerCase()))
    }
    if (params.market) {
      list = list.filter(t => t.market === params.market)
    }
    if (params.transaction_type) {
      list = list.filter(t => t.transaction_type === params.transaction_type)
    }
    
    // Sort by date descending
    list.sort((a, b) => new Date(b.transaction_date) - new Date(a.transaction_date))
    
    // Paginate
    const skip = params.skip || 0
    const limit = params.limit || 50
    const paginated = list.slice(skip, skip + limit)
    
    return Promise.resolve({ data: paginated })
  },

  getTransactionsCount(params = {}) {
    let list = getUserTransactions()
    if (params.symbol) {
      list = list.filter(t => t.symbol.toLowerCase().includes(params.symbol.toLowerCase()))
    }
    if (params.market) {
      list = list.filter(t => t.market === params.market)
    }
    if (params.transaction_type) {
      list = list.filter(t => t.transaction_type === params.transaction_type)
    }
    return Promise.resolve({ data: { total: list.length } })
  },

  getTransaction(id) {
    const t = transactions.find(t => t.id === id)
    return t ? Promise.resolve({ data: t }) : Promise.reject({ response: { status: 404, data: { detail: '交易不存在' } } })
  },

  createTransaction(data) {
    const newT = {
      id: `t-${Date.now()}`,
      owner_id: getCurrentUserId(),
      ...data,
      quantity: parseFloat(data.quantity),
      price: parseFloat(data.price),
      fee: parseFloat(data.fee || 0)
    }
    transactions.push(newT)
    syncHoldingsFromTransactions()
    return Promise.resolve({ data: newT })
  },

  updateTransaction(id, data) {
    const idx = transactions.findIndex(t => t.id === id)
    if (idx === -1) return Promise.reject({ response: { status: 404, data: { detail: '交易不存在' } } })
    
    const updated = {
      ...transactions[idx],
      ...data,
      quantity: parseFloat(data.quantity),
      price: parseFloat(data.price),
      fee: parseFloat(data.fee || 0)
    }
    transactions[idx] = updated
    syncHoldingsFromTransactions()
    return Promise.resolve({ data: updated })
  },

  deleteTransaction(id) {
    transactions = transactions.filter(t => t.id !== id)
    syncHoldingsFromTransactions()
    return Promise.resolve({ data: { message: '删除成功' } })
  },

  // Holdings
  getHoldings(params = {}) {
    let list = getUserHoldings()
    if (params.market) {
      list = list.filter(h => h.market === params.market)
    }
    return Promise.resolve({ data: list })
  },

  getHolding(symbol, market) {
    const list = getUserHoldings()
    const h = list.find(h => h.symbol === symbol && (!market || h.market === market))
    return h ? Promise.resolve({ data: h }) : Promise.reject({ response: { status: 404, data: { detail: '持仓不存在' } } })
  },

  updateHoldingPrice(holdingId, price) {
    const userHoldings = getUserHoldings()
    const h = userHoldings.find(item => item.id === holdingId || item.symbol === holdingId)
    if (h) {
      h.current_price = parseFloat(price)
      h.updated_at = new Date().toISOString()
    }
    return Promise.resolve({ data: h })
  },

  batchUpdatePrices(updates) {
    const userHoldings = getUserHoldings()
    let success = 0
    updates.forEach(u => {
      const h = userHoldings.find(item => item.symbol === u.symbol && item.market === u.market)
      if (h) {
        h.current_price = parseFloat(u.price)
        h.updated_at = new Date().toISOString()
        success++
      }
    })
    return Promise.resolve({
      data: {
        success_count: success,
        failed_count: updates.length - success,
        failed_list: []
      }
    })
  },

  refreshAllPrices() {
    // Generate a mock job
    const jobId = `job-${Date.now()}`
    const userHoldings = getUserHoldings()
    userHoldings.forEach(h => {
      h.current_price = h.current_price * (1 + (Math.random() * 0.04 - 0.02))
      h.updated_at = new Date().toISOString()
    })
    return Promise.resolve({
      data: {
        id: jobId,
        status: 'succeeded',
        result: {
          success_count: userHoldings.length,
          skipped_count: 0,
          failed_count: 0,
          success_list: userHoldings.map(h => ({ symbol: h.symbol, market: h.market, price: h.current_price, source: 'Tushare (Mock)' }))
        }
      }
    })
  },

  getPriceRefreshJob(jobId) {
    const userHoldings = getUserHoldings()
    return Promise.resolve({
      data: {
        id: jobId,
        status: 'succeeded',
        result: {
          success_count: userHoldings.length,
          skipped_count: 0,
          failed_count: 0,
          success_list: userHoldings.map(h => ({ symbol: h.symbol, market: h.market, price: h.current_price, source: 'Tushare (Mock)' }))
        }
      }
    })
  },

  getSummary() {
    const userHoldings = getUserHoldings()
    const userTxns = getUserTransactions()
    const totalInvestedCNY = userHoldings.reduce((sum, h) => sum + convertToCNY(h.total_cost, h.currency), 0)
    const markets = new Set(userHoldings.map(h => h.market))
    
    return Promise.resolve({
      data: {
        total_invested: totalInvestedCNY,
        total_invested_cny: totalInvestedCNY,
        total_holdings: userHoldings.length,
        total_transactions: userTxns.length,
        markets_count: markets.size
      }
    })
  },

  getStatsByMarket() {
    const stats = {}
    getUserHoldings().forEach(h => {
      const costCNY = convertToCNY(h.total_cost, h.currency)
      if (!stats[h.market]) {
        stats[h.market] = { market: h.market, holdings_count: 0, total_cost: 0 }
      }
      stats[h.market].holdings_count++
      stats[h.market].total_cost += costCNY
    })
    return Promise.resolve({ data: Object.values(stats) })
  },

  getStatsByTime(groupBy = 'month') {
    // Generate time statistics dynamically from user's transactions
    const userTxns = getUserTransactions()
    const grouped = {}
    userTxns.forEach(t => {
      const period = t.transaction_date.substring(0, 7) // YYYY-MM
      if (!grouped[period]) grouped[period] = { period, buy_amount: 0, sell_amount: 0 }
      const amount = convertToCNY(t.quantity * t.price, t.currency)
      if (t.transaction_type === 'BUY') grouped[period].buy_amount += amount
      else grouped[period].sell_amount += amount
    })
    const stats = Object.values(grouped).sort((a, b) => a.period.localeCompare(b.period))
    return Promise.resolve({ data: stats })
  },

  getProfitLoss() {
    return Promise.resolve({ data: getUserHoldings() })
  },

  getCurrentHoldingsPerformance(currentPrices = {}) {
    const userHoldings = getUserHoldings()
    let cost = 0
    let value = 0
    
    userHoldings.forEach(h => {
      const price = currentPrices[h.symbol] || h.current_price
      const costCNY = convertToCNY(h.total_cost, h.currency)
      const valueCNY = convertToCNY(price * h.quantity, h.currency)
      
      cost += costCNY
      value += valueCNY
    })
    
    const pnl = value - cost
    const rate = cost > 0 ? (pnl / cost) * 100 : 0
    
    return Promise.resolve({
      data: {
        unrealized_pnl: pnl,
        current_holdings_cost: cost,
        unrealized_pnl_rate: rate,
        current_market_value: value,
        holdings_detail: userHoldings.map(h => {
          const p = currentPrices[h.symbol] || h.current_price
          return {
            symbol: h.symbol,
            name: h.name,
            market: h.market,
            quantity: h.quantity,
            avg_cost: h.avg_cost,
            total_cost: h.total_cost,
            current_price: p,
            unrealized_pnl: (p - h.avg_cost) * h.quantity,
            unrealized_pnl_rate: h.avg_cost > 0 ? ((p - h.avg_cost) / h.avg_cost) * 100 : 0,
            currency: h.currency
          }
        })
      }
    })
  },

  getRealizedPnLFifo() {
    const userTxns = getUserTransactions()
    const sells = userTxns.filter(t => t.transaction_type === 'SELL')
    
    let totalProfit = 0
    let totalSoldCost = 0
    const tradesDetail = []
    
    sells.forEach(s => {
      const buys = userTxns.filter(t => t.transaction_type === 'BUY' && t.symbol === s.symbol && t.market === s.market)
      if (buys.length > 0) {
        const avgBuyPrice = buys.reduce((sum, b) => sum + b.price * b.quantity, 0) / buys.reduce((sum, b) => sum + b.quantity, 0)
        const profit = (s.price - avgBuyPrice) * s.quantity - s.fee
        const cost = avgBuyPrice * s.quantity
        totalProfit += convertToCNY(profit, s.currency)
        totalSoldCost += convertToCNY(cost, s.currency)
        
        tradesDetail.push({
          symbol: s.symbol,
          name: s.name,
          market: s.market,
          quantity: s.quantity,
          buy_price: avgBuyPrice,
          sell_price: s.price,
          realized_pnl: profit,
          realized_pnl_rate: cost > 0 ? (profit / cost) * 100 : 0,
          currency: s.currency,
          sell_date: s.transaction_date
        })
      }
    })
    
    return Promise.resolve({
      data: {
        realized_pnl: totalProfit,
        sold_cost: totalSoldCost,
        realized_pnl_rate: totalSoldCost > 0 ? (totalProfit / totalSoldCost) * 100 : 0,
        trades_detail: tradesDetail
      }
    })
  },

  getDividendSummary() {
    const userCA = getUserCorporateActions()
    const divItems = userCA.filter(ca => ca.action_type === 'CASH_DIVIDEND')
    const divGross = divItems.reduce((sum, ca) => sum + convertToCNY(ca.total_dividend, ca.currency), 0)
    const divNet = divItems.reduce((sum, ca) => sum + convertToCNY(ca.net_dividend, ca.currency), 0)
    const tax = divGross - divNet
    
    // Build per-symbol breakdown dynamically
    const bySymbol = {}
    divItems.forEach(ca => {
      if (!bySymbol[ca.symbol]) bySymbol[ca.symbol] = { symbol: ca.symbol, name: ca.name, gross: 0, tax: 0, net: 0, currency: ca.currency }
      bySymbol[ca.symbol].gross += ca.total_dividend
      bySymbol[ca.symbol].net += ca.net_dividend
      bySymbol[ca.symbol].tax += ca.total_dividend - ca.net_dividend
    })
    
    return Promise.resolve({
      data: {
        total_dividend_gross: divGross,
        total_tax: tax,
        total_dividend_net: divNet,
        by_symbol: Object.values(bySymbol)
      }
    })
  },

  getTotalRealizedReturn() {
    const userTxns = getUserTransactions()
    const userCA = getUserCorporateActions()
    
    // Calculate realized P&L from sell transactions (simplified)
    const sells = userTxns.filter(t => t.transaction_type === 'SELL')
    let tradingPnl = 0
    let soldCost = 0
    sells.forEach(s => {
      // Find matching buys
      const buys = userTxns.filter(t => t.transaction_type === 'BUY' && t.symbol === s.symbol && t.market === s.market)
      if (buys.length > 0) {
        const avgBuyPrice = buys.reduce((sum, b) => sum + b.price * b.quantity, 0) / buys.reduce((sum, b) => sum + b.quantity, 0)
        const profit = (s.price - avgBuyPrice) * s.quantity - s.fee
        tradingPnl += convertToCNY(profit, s.currency)
        soldCost += convertToCNY(avgBuyPrice * s.quantity, s.currency)
      }
    })
    
    // Net dividend income
    const divNet = userCA.filter(ca => ca.action_type === 'CASH_DIVIDEND').reduce((sum, ca) => sum + convertToCNY(ca.net_dividend, ca.currency), 0)
    const totalReturn = tradingPnl + divNet
    
    return Promise.resolve({
      data: {
        realized_trading_pnl_cny: tradingPnl,
        net_dividend_income_cny: divNet,
        total_realized_return: totalReturn,
        total_realized_return_rate: soldCost > 0 ? (totalReturn / soldCost) * 100 : 0,
        sold_cost_cny: soldCost
      }
    })
  },

  getAccountTotalReturn(currentPrices = {}) {
    const userHoldings = getUserHoldings()
    const userTxns = getUserTransactions()
    const userCA = getUserCorporateActions()
    let cost = 0
    let value = 0
    
    userHoldings.forEach(h => {
      const price = currentPrices[h.symbol] || h.current_price
      const costCNY = convertToCNY(h.total_cost, h.currency)
      const valueCNY = convertToCNY(price * h.quantity, h.currency)
      
      cost += costCNY
      value += valueCNY
    })
    
    // 已实现 trading P&L (dynamic)
    const sells = userTxns.filter(t => t.transaction_type === 'SELL')
    let tradingPnl = 0
    let soldCostTotal = 0
    sells.forEach(s => {
      const buys = userTxns.filter(t => t.transaction_type === 'BUY' && t.symbol === s.symbol && t.market === s.market)
      if (buys.length > 0) {
        const avgBuyPrice = buys.reduce((sum, b) => sum + b.price * b.quantity, 0) / buys.reduce((sum, b) => sum + b.quantity, 0)
        tradingPnl += convertToCNY((s.price - avgBuyPrice) * s.quantity - s.fee, s.currency)
        soldCostTotal += convertToCNY(avgBuyPrice * s.quantity, s.currency)
      }
    })
    // 股息
    const divNet = userCA.filter(ca => ca.action_type === 'CASH_DIVIDEND').reduce((sum, ca) => sum + convertToCNY(ca.net_dividend, ca.currency), 0)
    // 未实现
    const unrealized = value - cost
    
    const totalReturn = tradingPnl + divNet + unrealized
    const principal = cost + soldCostTotal - (soldCostTotal + tradingPnl)
    
    return Promise.resolve({
      data: {
        total_return: totalReturn,
        total_return_rate: cost > 0 ? (totalReturn / cost) * 100 : 0,
        annualized_return_rate: cost > 0 ? ((totalReturn / cost) * 100 * 0.8) : 0, // Simplified mock XIRR
        net_invested_principal_cny: cost > 0 ? cost : 0,
        current_market_value_cny: value,
        realized_trading_pnl_cny: tradingPnl,
        unrealized_pnl_cny: unrealized,
        net_dividend_income_cny: divNet
      }
    })
  },

  // Corporate Actions CRUD
  getCorporateActions(params = {}) {
    let list = getUserCorporateActions()
    
    if (params.symbol) {
      list = list.filter(ca => ca.symbol.toLowerCase().includes(params.symbol.toLowerCase()))
    }
    if (params.market) {
      list = list.filter(ca => ca.market === params.market)
    }
    if (params.action_type) {
      list = list.filter(ca => ca.action_type === params.action_type)
    }
    
    list.sort((a, b) => new Date(b.ex_date) - new Date(a.ex_date))
    
    const skip = params.skip || 0
    const limit = params.limit || 50
    return Promise.resolve({ data: list.slice(skip, skip + limit) })
  },

  getCorporateActionsCount(params = {}) {
    let list = getUserCorporateActions()
    if (params.symbol) {
      list = list.filter(ca => ca.symbol.toLowerCase().includes(params.symbol.toLowerCase()))
    }
    if (params.market) {
      list = list.filter(ca => ca.market === params.market)
    }
    if (params.action_type) {
      list = list.filter(ca => ca.action_type === params.action_type)
    }
    return Promise.resolve({ data: { total: list.length } })
  },

  getCorporateAction(id) {
    const ca = corporateActions.find(item => item.id === id)
    return ca ? Promise.resolve({ data: ca }) : Promise.reject({ response: { status: 404, data: { detail: '记录不存在' } } })
  },

  createCorporateAction(data) {
    const newCa = {
      id: `ca-${Date.now()}`,
      owner_id: getCurrentUserId(),
      ...data,
      dividend_per_share: data.dividend_per_share ? parseFloat(data.dividend_per_share) : null,
      total_dividend: data.total_dividend ? parseFloat(data.total_dividend) : null,
      net_dividend: data.total_dividend ? parseFloat(data.total_dividend) * (1 - (data.tax_rate || 0.1)) : null,
      shares_received: data.shares_received ? parseFloat(data.shares_received) : null,
      subscription_price: data.subscription_price ? parseFloat(data.subscription_price) : null,
      subscription_quantity: data.subscription_quantity ? parseFloat(data.subscription_quantity) : null
    }
    corporateActions.push(newCa)
    return Promise.resolve({ data: newCa })
  },

  updateCorporateAction(id, data) {
    const idx = corporateActions.findIndex(item => item.id === id)
    if (idx === -1) return Promise.reject({ response: { status: 404, data: { detail: '记录不存在' } } })
    
    const updated = {
      ...corporateActions[idx],
      ...data,
      dividend_per_share: data.dividend_per_share ? parseFloat(data.dividend_per_share) : null,
      total_dividend: data.total_dividend ? parseFloat(data.total_dividend) : null,
      net_dividend: data.total_dividend ? parseFloat(data.total_dividend) * (1 - (data.tax_rate || 0.1)) : null,
      shares_received: data.shares_received ? parseFloat(data.shares_received) : null,
      subscription_price: data.subscription_price ? parseFloat(data.subscription_price) : null,
      subscription_quantity: data.subscription_quantity ? parseFloat(data.subscription_quantity) : null
    }
    corporateActions[idx] = updated
    return Promise.resolve({ data: updated })
  },

  deleteCorporateAction(id) {
    corporateActions = corporateActions.filter(ca => ca.id !== id)
    return Promise.resolve({ data: { message: '删除成功' } })
  },

  getCorporateActionsSummary(params = {}) {
    const filtered = getUserCorporateActions().filter(ca => {
      if (params.symbol && !ca.symbol.toLowerCase().includes(params.symbol.toLowerCase())) return false
      if (params.market && ca.market !== params.market) return false
      return true
    })
    
    const divGross = filtered.filter(ca => ca.action_type === 'CASH_DIVIDEND').reduce((sum, ca) => sum + convertToCNY(ca.total_dividend, ca.currency), 0)
    const divNet = filtered.filter(ca => ca.action_type === 'CASH_DIVIDEND').reduce((sum, ca) => sum + convertToCNY(ca.net_dividend, ca.currency), 0)
    const tax = divGross - divNet
    
    return Promise.resolve({
      data: {
        total_count: filtered.length,
        cash_dividends: {
          total_dividend: divGross,
          total_tax: tax,
          net_dividend: divNet
        }
      }
    })
  },

  // Exchange Rates
  getLatestRates() {
    return Promise.resolve({
      data: {
        base_currency: 'CNY',
        rates: exchangeRates,
        effective_date: new Date().toISOString().split('T')[0],
        source: 'api'
      }
    })
  },

  getExchangeRates(params = {}) {
    let list = [...exchangeRatesHistory]
    if (params.from_currency) {
      list = list.filter(r => r.from_currency === params.from_currency)
    }
    if (params.to_currency) {
      list = list.filter(r => r.to_currency === params.to_currency)
    }
    // Sort by effective_date desc, then by from_currency
    list.sort((a, b) => {
      const dateA = new Date(a.effective_date)
      const dateB = new Date(b.effective_date)
      if (dateB - dateA !== 0) return dateB - dateA
      return a.from_currency.localeCompare(b.from_currency)
    })
    return Promise.resolve({ data: list })
  },

  getExchangeRate(from, to) {
    const fromRate = exchangeRates[from] || 1.0
    const toRate = exchangeRates[to] || 1.0
    return Promise.resolve({ data: { rate: toRate / fromRate } })
  },

  createOrUpdateExchangeRate(data) {
    const rateVal = parseFloat(data.rate)
    const fromCurr = data.from_currency
    const toCurr = data.to_currency || 'CNY'
    const effDate = data.effective_date || new Date().toISOString().split('T')[0]
    const src = data.source || 'manual'
    const isActive = data.is_active !== false

    // Update quick-convert rates
    if (toCurr === 'CNY' && isActive) {
      exchangeRates[fromCurr] = rateVal
    }

    // Check if duplicate exists (from_currency, to_currency, effective_date)
    const existingIdx = exchangeRatesHistory.findIndex(
      r => r.from_currency === fromCurr && r.to_currency === toCurr && r.effective_date === effDate
    )

    const now = new Date().toISOString()
    let record
    if (existingIdx !== -1) {
      // Update
      exchangeRatesHistory[existingIdx] = {
        ...exchangeRatesHistory[existingIdx],
        rate: rateVal,
        source: src,
        is_active: isActive,
        updated_at: now
      }
      record = exchangeRatesHistory[existingIdx]
    } else {
      // Create
      record = {
        id: `er-${Date.now()}`,
        from_currency: fromCurr,
        to_currency: toCurr,
        rate: rateVal,
        effective_date: effDate,
        source: src,
        is_active: isActive,
        created_at: now,
        updated_at: now
      }
      exchangeRatesHistory.push(record)
    }

    return Promise.resolve({ data: record })
  },

  updateExchangeRate(id, data) {
    const idx = exchangeRatesHistory.findIndex(r => String(r.id) === String(id) || r.from_currency === id)
    if (idx === -1) {
      if (exchangeRates[id] !== undefined) {
        exchangeRates[id] = parseFloat(data.rate)
        return Promise.resolve({ data: { id, from_currency: id, rate: data.rate } })
      }
      return Promise.reject({ response: { status: 404, data: { detail: '汇率记录不存在' } } })
    }

    const updated = {
      ...exchangeRatesHistory[idx],
      ...data,
      rate: data.rate !== undefined ? parseFloat(data.rate) : exchangeRatesHistory[idx].rate,
      source: data.source !== undefined ? data.source : exchangeRatesHistory[idx].source,
      is_active: data.is_active !== undefined ? data.is_active : exchangeRatesHistory[idx].is_active,
      updated_at: new Date().toISOString()
    }

    exchangeRatesHistory[idx] = updated

    // Update quick-convert rates
    if (updated.to_currency === 'CNY' && updated.is_active) {
      exchangeRates[updated.from_currency] = updated.rate
    }

    return Promise.resolve({ data: updated })
  },

  deleteExchangeRate(id) {
    const idx = exchangeRatesHistory.findIndex(r => String(r.id) === String(id) || r.from_currency === id)
    if (idx !== -1) {
      const record = exchangeRatesHistory[idx]
      exchangeRatesHistory.splice(idx, 1)
      const stillHasActive = exchangeRatesHistory.some(r => r.from_currency === record.from_currency && r.is_active)
      if (!stillHasActive) {
        delete exchangeRates[record.from_currency]
      }
    } else {
      delete exchangeRates[id]
    }
    return Promise.resolve({ data: { message: '删除成功' } })
  },

  convertCurrency(data) {
    const val = convertToCNY(data.amount, data.from_currency)
    return Promise.resolve({ data: { converted_amount: val } })
  },

  refreshRatesFromAPI() {
    Object.keys(exchangeRates).forEach(curr => {
      if (curr !== 'CNY') {
        exchangeRates[curr] = parseFloat((exchangeRates[curr] * (1 + (Math.random() * 0.02 - 0.01))).toFixed(4))
        
        const now = new Date().toISOString()
        const effDate = now.split('T')[0]
        const existingIdx = exchangeRatesHistory.findIndex(
          r => r.from_currency === curr && r.to_currency === 'CNY' && r.effective_date === effDate
        )
        if (existingIdx !== -1) {
          exchangeRatesHistory[existingIdx].rate = exchangeRates[curr]
          exchangeRatesHistory[existingIdx].source = 'api'
          exchangeRatesHistory[existingIdx].updated_at = now
        } else {
          exchangeRatesHistory.push({
            id: `er-${Date.now()}-${curr}`,
            from_currency: curr,
            to_currency: 'CNY',
            rate: exchangeRates[curr],
            effective_date: effDate,
            source: 'api',
            is_active: true,
            created_at: now,
            updated_at: now
          })
        }
      }
    })
    
    return Promise.resolve({
      data: {
        message: '汇率同步成功（Mock）',
        rates: exchangeRates,
        count: Object.keys(exchangeRates).length - 1
      }
    })
  },

  // Users (Admin)
  getUsers() {
    // Strip passwords before returning
    return Promise.resolve({ data: users.map(({ password: _, ...u }) => u) })
  },

  createUser(userData) {
    const newUser = {
      id: `u-${Date.now()}`,
      username: userData.username,
      password: 'changeme',
      is_admin: userData.is_admin === true,
      email: userData.email || '',
      name: userData.name || userData.username,
      is_active: userData.is_active !== false,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString()
    }
    users.push(newUser)
    const { password: _, ...safeUser } = newUser
    return Promise.resolve({ data: safeUser })
  },

  updateUser(id, userData) {
    const idx = users.findIndex(u => u.id === id)
    if (idx === -1) return Promise.reject({ response: { status: 404, data: { detail: '用户不存在' } } })
    users[idx] = { 
      ...users[idx], 
      ...userData,
      updated_at: new Date().toISOString()
    }
    return Promise.resolve({ data: users[idx] })
  },

  deleteUser(id) {
    const idx = users.findIndex(u => u.id === id)
    if (idx !== -1) users.splice(idx, 1)
    return Promise.resolve({ data: { message: '删除成功' } })
  },

  resetUserPassword() {
    return Promise.resolve({ data: { message: '密码重置成功（Mock）' } })
  },

  // Admin Holdings
  getAllHoldingsAdmin() {
    return Promise.resolve({ data: holdings })
  },

  getUserHoldingsAdmin(userId) {
    return Promise.resolve({ data: holdings.filter(h => h.owner_id === userId) })
  },

  // Imports Preview & Execute (Always succeed with detailed stats for WOW effect!)
  previewCmbFundFlows() {
    return Promise.resolve({
      data: {
        broker: '招商证券 (Mock)',
        total_rows: 15,
        eligible_trade_rows: 12,
        eligible_dividend_rows: 2,
        eligible_tax_rows: 1,
        duplicate_rows: 0,
        skipped_non_trade_rows: 0,
        skipped_invalid_rows: 0,
        date_start: '2024-01-01',
        date_end: '2024-05-15'
      }
    })
  },

  importCmbFundFlows() {
    return Promise.resolve({
      data: {
        imported_transactions: 12,
        imported_corporate_actions: 2,
        imported_tax_adjustments: 1,
        duplicate_rows: 0
      }
    })
  },

  previewIbkrActivity() {
    return Promise.resolve({
      data: {
        broker: 'IBKR (Mock)',
        total_rows: 25,
        eligible_trade_rows: 20,
        eligible_dividend_rows: 3,
        eligible_tax_rows: 2,
        duplicate_rows: 0,
        skipped_non_trade_rows: 0,
        skipped_invalid_rows: 0,
        skipped_option_rows: 0,
        skipped_fx_rows: 0,
        skipped_cash_rows: 0,
        date_start: '2024-01-01',
        date_end: '2024-05-15'
      }
    })
  },

  importIbkrActivity() {
    return Promise.resolve({
      data: {
        imported_transactions: 20,
        imported_corporate_actions: 3,
        imported_tax_adjustments: 2,
        duplicate_rows: 0
      }
    })
  },

  previewEastmoneyStatement() {
    return Promise.resolve({
      data: {
        broker: '东方财富 (Mock)',
        total_rows: 18,
        eligible_trade_rows: 15,
        eligible_dividend_rows: 2,
        eligible_tax_rows: 1,
        duplicate_rows: 0,
        skipped_non_trade_rows: 0,
        skipped_invalid_rows: 0,
        skipped_cash_rows: 0,
        skipped_unsupported_rows: 0,
        date_start: '2024-01-01',
        date_end: '2024-05-15'
      }
    })
  },

  importEastmoneyStatement() {
    return Promise.resolve({
      data: {
        imported_transactions: 15,
        imported_corporate_actions: 2,
        imported_tax_adjustments: 1,
        duplicate_rows: 0
      }
    })
  },

  importCSV() {
    return Promise.resolve({ data: { message: '标准 CSV 记录导入成功！(Mocked 5 条新数据)' } })
  },

  importExcel() {
    return Promise.resolve({ data: { message: '标准 Excel 记录导入成功！(Mocked 5 条新数据)' } })
  },

  exportExcel() {
    // Return empty blob for export mock
    return Promise.resolve({ data: new Blob([], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }) })
  }
}
