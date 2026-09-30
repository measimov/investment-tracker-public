import { COLOR } from '@/styles/tokens'
import { formatLocalDate, parseLocalDate } from './dateRange'
import { CURRENCIES } from './currency'

/** 空值占位符：全站统一（此前 '-' / '--' / '—' 三种并存，#219） */
export const EMPTY = '—'

function isMissing(value: unknown): boolean {
  return (
    value === null ||
    value === undefined ||
    value === '' ||
    Number.isNaN(Number(value as number | string))
  )
}

export function formatNumber(num: number | string | null | undefined, decimals = 2): string {
  if (isMissing(num)) return EMPTY
  return Number(num).toLocaleString('zh-CN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals
  })
}

/** 数量：整数不带小数（5,000 而不是 5,000.0000），碎股最多 4 位且去掉尾随 0 */
export function formatQuantity(value: number | string | null | undefined): string {
  if (isMissing(value)) return EMPTY
  return Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 4
  })
}

/**
 * 价格：至少 2 位、最多 4 位小数，去掉 2 位之后的尾随 0——
 * A 股 97.45、ETF 1.409、港股 2.545、仙股 0.0850 各自显示到有效位，
 * 不再一律 97.4500。
 */
export function formatPrice(value: number | string | null | undefined): string {
  if (isMissing(value)) return EMPTY
  return Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4
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
  if (!date) return EMPTY
  // 纯日期串 'YYYY-MM-DD' 按本地零点解析：new Date('2026-01-05') 是 UTC 零点，UTC 以西时区会
  // 显示成前一天（#284）；带时间的串与时间戳照旧交给 Date
  const value =
    typeof date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(date)
      ? parseLocalDate(date)
      : new Date(date)
  // 补零格式（2026/01/05）：与 formatDateTime 一致，日期列在 tabular-nums 下可对齐
  return value.toLocaleDateString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit'
  })
}

// 带时区的 ISO 时间串必须走这里（转成浏览器本地时区）；不要对它做 slice——
// 后端 timestamptz 以 UTC 输出，切片会让北京时间 0-8 点显示成前一天（#221）
export function formatDateTime(date: string | number | Date | null | undefined): string {
  if (!date) return EMPTY
  return new Date(date).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  })
}

export function formatPercent(value: number | string | null | undefined, precision = 2): string {
  // 缺数据显示占位符而不是 0.00%——后者看起来像「真的是 0」（#218/#219）
  if (isMissing(value)) return EMPTY
  return `${Number(value) >= 0 ? '+' : ''}${Number(value).toFixed(precision)}%`
}

const CURRENCY_SYMBOLS: Record<string, string> = Object.fromEntries(
  CURRENCIES.map((item) => [item.code, item.symbol])
)

/** 金额：负号在币种符号之前（-¥1.00，不是 ¥-1.00）；缺值为占位符 */
export function formatCurrency(
  amount: number | string | null | undefined,
  currency = 'CNY',
  decimals = 2
): string {
  if (isMissing(amount)) return EMPTY
  const value = Number(amount)
  const symbol = CURRENCY_SYMBOLS[currency] ?? ''
  const body = formatNumber(Math.abs(value), decimals)
  // -0.004 四舍五入后是 0.00，不应带负号
  const negative = value < 0 && body !== formatNumber(0, decimals)
  return `${negative ? '-' : ''}${symbol}${body}`
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
  // 缺值不着色（此前 Number(null) = 0 被染成盈利绿）
  if (isMissing(value)) return ''
  return Number(value) >= 0 ? COLOR.success : COLOR.danger
}
