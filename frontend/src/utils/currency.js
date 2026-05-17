/**
 * 货币格式化和转换工具
 */

/**
 * 格式化货币金额（支持CNY和USD）
 * @param {number} amount - 金额
 * @param {string} currency - 货币代码 ('CNY' 或 'USD')
 * @param {number} precision - 小数位数
 * @returns {string} 格式化后的金额
 */
export function formatCurrency(amount, currency = 'CNY', precision = 2) {
  if (amount === null || amount === undefined || isNaN(amount)) {
    return currency === 'USD' ? '$0.00' : '¥0.00'
  }

  const formatted = Number(amount).toFixed(precision)
  const parts = formatted.split('.')
  parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',')

  if (currency === 'USD') {
    return '$' + parts.join('.')
  } else {
    return '¥' + parts.join('.')
  }
}

/**
 * 格式化双币种显示（CNY / USD）
 * @param {number} amountCny - CNY金额
 * @param {number} amountUsd - USD金额
 * @returns {string} 格式化后的双币种字符串
 */
export function formatDualCurrency(amountCny, amountUsd) {
  const cny = formatCurrency(amountCny, 'CNY')
  const usd = formatCurrency(amountUsd, 'USD')
  return `${cny} / ${usd}`
}

/**
 * 获取货币符号
 * @param {string} currency - 货币代码
 * @returns {string} 货币符号
 */
export function getCurrencySymbol(currency) {
  const symbols = {
    'CNY': '¥',
    'USD': '$',
    'HKD': 'HK$',
    'SGD': 'S$',
    'EUR': '€',
    'GBP': '£',
    'JPY': '¥'
  }
  return symbols[currency] || currency
}

/**
 * 获取货币颜色（用于显示盈亏）
 * @param {number} amount - 金额
 * @returns {string} 颜色代码
 */
export function getProfitColor(amount) {
  if (amount > 0) return '#67C23A'  // 绿色（盈利）
  if (amount < 0) return '#F56C6C'  // 红色（亏损）
  return '#909399'  // 灰色（持平）
}

/**
 * 解析货币金额字符串
 * @param {string} str - 货币字符串（如 "¥1,234.56"）
 * @returns {number} 数值
 */
export function parseCurrency(str) {
  if (typeof str === 'number') return str
  if (!str) return 0

  // 移除货币符号和逗号
  const cleaned = str.replace(/[¥$,]/g, '')
  return parseFloat(cleaned) || 0
}

/**
 * 计算百分比变化
 * @param {number} current - 当前值
 * @param {number} original - 原始值
 * @returns {number} 百分比变化
 */
export function calculatePercentageChange(current, original) {
  if (!original || original === 0) return 0
  return ((current - original) / original) * 100
}

/**
 * 货币代码列表
 */
export const CURRENCIES = [
  { code: 'CNY', name: '人民币', symbol: '¥' },
  { code: 'USD', name: '美元', symbol: '$' },
  { code: 'HKD', name: '港币', symbol: 'HK$' },
  { code: 'SGD', name: '新加坡元', symbol: 'S$' },
  { code: 'EUR', name: '欧元', symbol: '€' },
  { code: 'GBP', name: '英镑', symbol: '£' },
  { code: 'JPY', name: '日元', symbol: '¥' }
]

/**
 * 根据货币代码获取货币信息
 * @param {string} code - 货币代码
 * @returns {object} 货币信息
 */
export function getCurrencyInfo(code) {
  return CURRENCIES.find(c => c.code === code) || { code, name: code, symbol: code }
}
