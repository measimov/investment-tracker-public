/**
 * 持仓页的搜索与标签筛选（#235）：纯函数，不依赖 Vue/Pinia，可直接单测。
 *
 * - 关键词：代码（不区分大小写；港股容忍省略前导零，「700」「0700」都能找到 00700）
 *   或名称子串。拼音首字母暂不支持：持仓响应不带 security_catalog 的 cnspell，
 *   逐只调搜索端点换拼音不值当。
 * - 标签：五组（行业 / AI 标签 / 风险等级 / 雪球观点 / 近期事件）。**组内 OR、组间 AND**：
 *   选了「高股息」「估值偏低」+「高风险」= (高股息 或 估值偏低) 且 高风险。
 *   组内 AND 几乎总是空集（一只标的很少同时带几个同组标签），组间 OR 又会让
 *   加条件反而变多，都不符合「逐步收窄」的直觉。
 */

import { analysisTagTone, riskLabel, type AnalysisTagTone } from '../security-detail/analysisTags'
import { securityEventTypeLabel } from '@/utils/labels'

export type HoldingTagGroup = 'industry' | 'ai' | 'risk' | 'opinion' | 'event'

/** 一只标的可被筛选的标签来源（由 useSecurityBadges 按行组装） */
export interface HoldingTagSource {
  /** 行业分类（规则 > 官方 > 东方财富，后端已合成为一个值）；未取得为 null */
  industry: string | null
  /** AI 分析的全部标签（表格只显示第一个非噪音标签，筛选用全部） */
  aiTags: string[]
  riskLevel: string | null
  /** 雪球观点摘要的全部标签（整体立场 + 近期变化 + 语境） */
  opinionTags: string[]
  /** 未来窗口内（useSecurityBadges.loadEvents 已取的 90 天）的事件类型 */
  eventTypes: string[]
}

export const EMPTY_TAG_SOURCE: HoldingTagSource = {
  industry: null,
  aiTags: [],
  riskLevel: null,
  opinionTags: [],
  eventTypes: []
}

export const TAG_GROUP_LABELS: Record<HoldingTagGroup, string> = {
  industry: '行业',
  ai: 'AI 标签',
  risk: '风险等级',
  opinion: '雪球观点',
  event: '近期事件'
}

const GROUP_ORDER: HoldingTagGroup[] = ['industry', 'ai', 'risk', 'opinion', 'event']
const RISK_ORDER = ['high', 'medium', 'low']
const TONE_ORDER: AnalysisTagTone[] = ['negative', 'positive', 'neutral']

/** el-select 的选项值：`组:值`，同名标签在不同组里互不串 */
export function tagFilterValue(group: HoldingTagGroup, value: string): string {
  return `${group}:${value}`
}

function parseTagFilterValue(value: string): { group: HoldingTagGroup; value: string } | null {
  const index = value.indexOf(':')
  if (index <= 0) return null
  const group = value.slice(0, index) as HoldingTagGroup
  if (!GROUP_ORDER.includes(group)) return null
  return { group, value: value.slice(index + 1) }
}

function valuesOf(source: HoldingTagSource, group: HoldingTagGroup): string[] {
  if (group === 'industry') return source.industry ? [source.industry] : []
  if (group === 'ai') return source.aiTags
  if (group === 'risk') return source.riskLevel ? [source.riskLevel] : []
  if (group === 'opinion') return source.opinionTags
  return source.eventTypes
}

export interface TagFilterOption {
  value: string
  label: string
  /** 当前持仓里带这个标签的标的数 */
  count: number
  group: HoldingTagGroup
  /** AI 标签的褒贬（与详情页同一份配色口径） */
  tone?: AnalysisTagTone
}

export interface TagFilterOptionGroup {
  group: HoldingTagGroup
  label: string
  options: TagFilterOption[]
}

function optionLabel(group: HoldingTagGroup, value: string): string {
  if (group === 'risk') return `${riskLabel(value)}风险`
  if (group === 'event') return securityEventTypeLabel(value)
  return value
}

function compareOptions(group: HoldingTagGroup, a: TagFilterOption, b: TagFilterOption): number {
  if (group === 'risk') {
    const rank = (option: TagFilterOption) => {
      const index = RISK_ORDER.indexOf(option.value.slice('risk:'.length))
      return index === -1 ? RISK_ORDER.length : index
    }
    return rank(a) - rank(b)
  }
  if (group === 'ai') {
    const tone = TONE_ORDER.indexOf(a.tone!) - TONE_ORDER.indexOf(b.tone!)
    if (tone) return tone
  }
  return b.count - a.count || a.label.localeCompare(b.label, 'zh-CN')
}

/**
 * 选项只来自当前持仓实际出现的标签（不列白名单全集：选了没有结果的选项只是噪音），
 * 空组不出现。同一标的重复出现（按账户视图的多行）只计一次——调用方按标的去重后传入。
 */
export function buildTagFilterOptions(sources: HoldingTagSource[]): TagFilterOptionGroup[] {
  const groups: TagFilterOptionGroup[] = []
  for (const group of GROUP_ORDER) {
    const counts = new Map<string, number>()
    for (const source of sources) {
      for (const value of new Set(valuesOf(source, group))) {
        if (!value) continue
        counts.set(value, (counts.get(value) ?? 0) + 1)
      }
    }
    if (!counts.size) continue
    const options = [...counts.entries()].map(([value, count]) => ({
      value: tagFilterValue(group, value),
      label: optionLabel(group, value),
      count,
      group,
      ...(group === 'ai' ? { tone: analysisTagTone(value) } : {})
    }))
    options.sort((a, b) => compareOptions(group, a, b))
    groups.push({ group, label: TAG_GROUP_LABELS[group], options })
  }
  return groups
}

/** 组内 OR、组间 AND；未选任何标签 = 不过滤。无法识别的选项值忽略 */
export function matchesTagFilter(source: HoldingTagSource, selected: readonly string[]): boolean {
  const wanted = new Map<HoldingTagGroup, Set<string>>()
  for (const raw of selected) {
    const parsed = parseTagFilterValue(raw)
    if (!parsed) continue
    const set = wanted.get(parsed.group) ?? new Set<string>()
    set.add(parsed.value)
    wanted.set(parsed.group, set)
  }
  for (const [group, values] of wanted) {
    if (!valuesOf(source, group).some((value) => values.has(value))) return false
  }
  return true
}

function stripLeadingZeros(value: string): string {
  return value.replace(/^0+(?=\d)/, '')
}

/**
 * 代码是否相同：不区分大小写，纯数字代码忽略前导零（港股 700 = 00700）。
 * 深链定位用它判断「就是这一只」，比关键词子串严格。
 */
export function symbolEquals(a: string, b: string): boolean {
  const x = a.trim().toUpperCase()
  const y = b.trim().toUpperCase()
  if (x === y) return true
  return /^\d+$/.test(x) && /^\d+$/.test(y) && stripLeadingZeros(x) === stripLeadingZeros(y)
}

/** 关键词匹配：代码子串（不区分大小写，数字代码容忍前导零差异）或名称子串 */
export function matchesKeyword(
  row: { symbol: string; name?: string | null },
  keyword: string | null | undefined
): boolean {
  const query = (keyword ?? '').trim().toUpperCase()
  if (!query) return true
  const symbol = row.symbol.toUpperCase()
  if (symbol.includes(query)) return true
  if (/^\d+$/.test(query) && /^\d+$/.test(symbol)) {
    if (stripLeadingZeros(symbol).includes(stripLeadingZeros(query))) return true
  }
  return (row.name ?? '').toUpperCase().includes(query)
}
