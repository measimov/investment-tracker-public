/**
 * AI 分析标签的褒贬配色（#221）：按后端 `security_analysis_prompts.ALLOWED_TAGS` 白名单分
 * 正面 / 负面 / 中性。纯函数、无 view 依赖——持仓页 AI 列复用同一份，两处配色不会分叉。
 *
 * 白名单变更时同步这里（spec 钉住当前全集）；白名单外的标签按中性处理，不会误着色。
 */

export type AnalysisTagTone = 'positive' | 'negative' | 'neutral'
export type AnalysisTagType = 'success' | 'danger' | 'info'

export const POSITIVE_ANALYSIS_TAGS: ReadonlySet<string> = new Set([
  '高股息',
  '分红连续',
  '业绩增长',
  '大股东增持',
  '估值偏低',
  '安全边际充足',
  '财务强度高',
  '净现金充裕'
])

export const NEGATIVE_ANALYSIS_TAGS: ReadonlySet<string> = new Set([
  '分红中断',
  '业绩下滑',
  '业绩预警',
  '高质押',
  '大股东减持',
  '解禁临近',
  '审计非标',
  '估值偏高',
  '利润质量存疑',
  '现金流背离',
  '依赖非经常损益',
  '安全边际不足',
  '高杠杆脆弱',
  '尾部风险暴露'
])

// 中性：「数据不足」（数据边界，不是褒贬）及白名单外的一切

export function analysisTagTone(tag: string): AnalysisTagTone {
  if (POSITIVE_ANALYSIS_TAGS.has(tag)) return 'positive'
  if (NEGATIVE_ANALYSIS_TAGS.has(tag)) return 'negative'
  return 'neutral'
}

const TONE_TYPES: Record<AnalysisTagTone, AnalysisTagType> = {
  positive: 'success',
  negative: 'danger',
  neutral: 'info'
}

/** el-tag 的 type：正面 success、负面 danger、中性 info */
export function analysisTagType(tag: string): AnalysisTagType {
  return TONE_TYPES[analysisTagTone(tag)]
}

export type RiskTagType = 'danger' | 'warning' | 'success' | 'info'

/** 风险等级配色：高 danger、中 warning、低 success；未知等级 info（不再兜底成绿色） */
export function riskTagType(level: string | null | undefined): RiskTagType {
  if (level === 'high') return 'danger'
  if (level === 'medium') return 'warning'
  if (level === 'low') return 'success'
  return 'info'
}

const RISK_LABELS: Record<string, string> = { low: '低', medium: '中', high: '高' }

export function riskLabel(level: string | null | undefined): string {
  return RISK_LABELS[String(level || '')] || String(level || '未知')
}
