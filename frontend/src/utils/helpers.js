export function formatNumber(num, decimals = 2) {
  if (num === null || num === undefined) return '-'
  return Number(num).toLocaleString('zh-CN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals
  })
}

export function formatDate(date) {
  if (!date) return '-'
  return new Date(date).toLocaleDateString('zh-CN')
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
