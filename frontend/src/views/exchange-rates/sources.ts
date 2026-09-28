// 汇率来源的展示口径（#200）：人民币汇率中间价为主源，第三方聚合只做比对/兜底
export const SOURCE_LABELS: Record<string, string> = {
  'cfets-ccpr': '官方中间价',
  'api-ecb': '欧洲央行参考价',
  'api-backup': '备用聚合源',
  api: '自动获取',
  manual: '手工录入',
  system: '系统默认'
}

export const sourceLabel = (value: string | null | undefined) =>
  value ? SOURCE_LABELS[value] || value : '—'

export const sourceTagType = (source: string | null | undefined) => {
  if (source === 'cfets-ccpr') return 'success'
  if (source === 'manual') return 'warning'
  // 第三方报价只在官方中间价长期不可用时才会成为生效汇率：用醒目色提示降级
  if (source === 'api-ecb' || source === 'api-backup') return 'danger'
  return 'info'
}

// 比对差异（%）超过这个幅度标红；与后端 FX_CHECK_WARN_PCT 默认值一致，只影响展示。
// 人民币即期可在中间价 ±2% 内波动，第三方参考价与中间价差 0.5% 上下是常态
export const DIFF_WARN_PCT = 2

export const isDiffAbnormal = (diffPct: number | string | null | undefined) =>
  diffPct !== null && diffPct !== undefined && Math.abs(Number(diffPct)) > DIFF_WARN_PCT
