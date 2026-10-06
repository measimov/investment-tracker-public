import type { OutputAdjustment, RiskLevelAdjustment } from '@/types'

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

/** 后端 `security_analyses.risk_level_adjusted`：风险等级按市场下限上调的记录 */
export type { RiskLevelAdjustment } from '@/types'

/**
 * 风险标签旁的上调提示：模型给出的等级低于市场下限（港股 medium）时后端上调并留痕，
 * 这里把记录转成一句话；无记录（未上调 / 旧分析行）返回 null，不显示提示。
 */
export function riskAdjustmentText(
  adjustment: RiskLevelAdjustment | null | undefined
): string | null {
  if (!adjustment || !adjustment.from || !adjustment.to) return null
  const base = `模型给出「${riskLabel(adjustment.from)}」，按市场下限上调为「${riskLabel(adjustment.to)}」`
  return adjustment.reason ? `${base}：${adjustment.reason}` : base
}

/** 后端 `security_analyses.output_adjustments`：解析层对模型输出的调整记录（#287） */
export type { OutputAdjustment } from '@/types'

/**
 * 「已调整」提示：标签被归一/丢弃/截断、与预计算数据矛盾被去掉（#265）、补了免责声明时，
 * 把记录合成一句话；
 * 仅有额外章节这类不影响内容的记录不提示。无记录（旧分析行）返回 null。
 */
export function outputAdjustmentText(
  adjustments: OutputAdjustment[] | null | undefined
): string | null {
  if (!adjustments || adjustments.length === 0) return null
  const parts: string[] = []
  const dropped = adjustments
    .filter((item) => item.type === 'tag_dropped' && item.tag)
    .map((item) => item.tag as string)
  const truncated = adjustments.flatMap((item) =>
    item.type === 'tags_truncated' ? item.dropped || [] : []
  )
  const normalized = adjustments
    .filter(
      (item) =>
        item.type === 'tag_normalized' && item.from && item.to && item.from.trim() !== item.to
    )
    .map((item) => `「${item.from}」→「${item.to}」`)
  if (dropped.length) parts.push(`丢弃不合规标签：${dropped.join('、')}`)
  if (truncated.length) parts.push(`标签超过 4 个，去掉：${truncated.join('、')}`)
  if (normalized.length) parts.push(`标签归一：${normalized.join('、')}`)
  const bySignal = adjustments
    .filter((item) => item.type === 'tag_dropped_by_signal' && item.tag)
    .map((item) => (item.reason ? `${item.tag}（${item.reason}）` : (item.tag as string)))
  if (bySignal.length) parts.push(`与数据不符，去掉：${bySignal.join('、')}`)
  if (adjustments.some((item) => item.type === 'tag_signal_conflict')) {
    parts.push('标签与数据有矛盾，但去掉后将无标签，已保留原标签')
  }
  if (adjustments.some((item) => item.type === 'disclaimer_appended')) parts.push('补上免责声明')
  return parts.length ? `解析时已按规则调整：${parts.join('；')}` : null
}
