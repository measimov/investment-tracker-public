/**
 * 画布侧设计令牌：ECharts 等 canvas 场景无法读 CSS 变量，这里的 hex
 * 必须与 styles.css 的 :root 保持一致（唯一允许重复 hex 的地方）。
 */
export const COLOR = {
  primary: '#4f46e5',
  success: '#059669',
  danger: '#e11d48',
  warning: '#d97706',
  info: '#0ea5e9',
  textMuted: '#64748b'
} as const

export const CHART_FONT_FAMILY =
  "-apple-system, BlinkMacSystemFont, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif"

/** 图表分类色板：品牌 indigo 起手，避免 ECharts 默认蓝绿黄与主题脱节 */
export const CHART_PALETTE = [
  '#4f46e5',
  '#0ea5e9',
  '#059669',
  '#d97706',
  '#e11d48',
  '#8b5cf6',
  '#14b8a6'
]

export const chartTooltipCurrency = (value: number | string, currency = '¥'): string =>
  `${currency}${Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  })}`
