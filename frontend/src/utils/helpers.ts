import { COLOR } from '@/styles/tokens'
import { formatLocalDate } from './dateRange'

export function formatNumber(num: number | string | null | undefined, decimals = 2): string {
  if (num === null || num === undefined) return '-'
  return Number(num).toLocaleString('zh-CN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals
  })
}

// parseFloat 语义的数值兜底：null/undefined/不可解析 → 0（迁移 TS 后统一入口）
export function toNumber(value: number | string | null | undefined): number {
  const parsed = parseFloat(String(value ?? 0))
  return Number.isNaN(parsed) ? 0 : parsed
}

// 浏览器本地时区的今天（YYYY-MM-DD）。不要用 toISOString().split('T')[0]：
// 那是 UTC 日期，Asia/Shanghai 等正时区在本地 00:00-08:00 会得到前一天。
export function todayLocalISODate(): string {
  return formatLocalDate(new Date())
}

export function formatDate(date: string | number | Date | null | undefined): string {
  if (!date) return '-'
  // 补零格式（2026/01/05）：与 formatDateTime 一致，日期列在 tabular-nums 下可对齐
  return new Date(date).toLocaleDateString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit'
  })
}

export function formatDateTime(date: string | number | Date | null | undefined): string {
  if (!date) return '-'
  return new Date(date).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  })
}

export function formatPercent(value: number | string | null | undefined, precision = 2): string {
  if (value === null || value === undefined || Number.isNaN(Number(value)))
    return `${(0).toFixed(precision)}%`
  return `${Number(value) >= 0 ? '+' : ''}${Number(value).toFixed(precision)}%`
}

export function formatCurrency(
  amount: number | string | null | undefined,
  currency = 'CNY'
): string {
  const symbols: Record<string, string> = {
    CNY: '¥',
    USD: '$',
    HKD: 'HK$',
    SGD: 'S$'
  }
  return `${symbols[currency] || ''}${formatNumber(amount)}`
}

export function downloadFile(blob: Blob, filename: string): void {
  const url = window.URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  window.URL.revokeObjectURL(url)
}

// 盈亏着色：>=0 绿、<0 红（Statistics 口径）
export function profitColor(value: number | string | null | undefined): string {
  return Number(value) >= 0 ? COLOR.success : COLOR.danger
}
