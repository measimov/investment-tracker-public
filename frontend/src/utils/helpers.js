export function formatNumber(num, decimals = 2) {
  if (num === null || num === undefined) return '-'
  return Number(num).toLocaleString('zh-CN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals
  })
}

// 浏览器本地时区的今天（YYYY-MM-DD）。不要用 toISOString().split('T')[0]：
// 那是 UTC 日期，Asia/Shanghai 等正时区在本地 00:00-08:00 会得到前一天。
export function todayLocalISODate() {
  const now = new Date()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${now.getFullYear()}-${month}-${day}`
}

export function formatDate(date) {
  if (!date) return '-'
  return new Date(date).toLocaleDateString('zh-CN')
}

export function formatDateTime(date) {
  if (!date) return '-'
  return new Date(date).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  })
}

export function formatPercent(value, precision = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value)))
    return `${(0).toFixed(precision)}%`
  return `${value >= 0 ? '+' : ''}${Number(value).toFixed(precision)}%`
}

export function formatCurrency(amount, currency = 'CNY') {
  const symbols = {
    CNY: '¥',
    USD: '$',
    HKD: 'HK$',
    SGD: 'S$'
  }
  return `${symbols[currency] || ''}${formatNumber(amount)}`
}

export function downloadFile(blob, filename) {
  const url = window.URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  window.URL.revokeObjectURL(url)
}
