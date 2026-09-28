/**
 * 标的角标 feature（issue #140）：持仓列表的附加信息——
 * AI 标的分析摘要（标签列；点击名称跳转详情页）、标的事件角标
 * （未来 90 天：财报披露 / 分红预案 / 限售解禁）、雪球观点与行业分类（#235）。
 *
 * 两者都是锦上添花：加载失败一律静默，不打断持仓主流程。
 */

import { reactive } from 'vue'
import api from '@/api'
import { todayLocalISODate } from '@/utils/helpers'
import { parseLocalDate } from '@/utils/dateRange'
import type {
  OpinionSummariesResponse,
  OpinionSummaryRow,
  SecurityEvent,
  SecurityIndustryItem
} from '@/types'
import { OPINION_CHANGE_TAGS, opinionTagType } from '../opinions/useOpinions'
import { securityEventTypeLabel } from '@/utils/labels'
import type { AnalysisSummaryRow } from './types'
import type { HoldingTagSource } from './filters'

export const RISK_LABELS: Record<string, string> = { low: '低', medium: '中', high: '高' }

export function useSecurityBadges() {
  const state = reactive({
    analyses: new Map<string, AnalysisSummaryRow>(),
    events: new Map<string, SecurityEvent[]>(),
    opinions: new Map<string, OpinionSummaryRow>(),
    industries: new Map<string, SecurityIndustryItem>()
  })

  function riskTagType(level: string) {
    if (level === 'high') return 'danger'
    if (level === 'medium') return 'warning'
    return 'success'
  }

  function analysisFor(row: { symbol: string; market: string }): AnalysisSummaryRow | null {
    return state.analyses.get(`${row.symbol}:${row.market}`) || null
  }

  async function loadAnalyses() {
    try {
      const response = await api.listSecurityAnalyses()
      const map = new Map<string, AnalysisSummaryRow>()
      for (const row of response.data as AnalysisSummaryRow[]) {
        map.set(`${row.symbol}:${row.market}`, row)
      }
      state.analyses = map
    } catch {
      // 标签列失败静默：不打断持仓主流程
    }
  }

  async function loadEvents() {
    try {
      const response = await api.getSecurityEvents({ days_ahead: 90 })
      const map = new Map<string, SecurityEvent[]>()
      for (const event of response.data) {
        const key = `${event.symbol}:${event.market}`
        const list = map.get(key) || []
        list.push(event)
        map.set(key, list)
      }
      state.events = map
    } catch {
      // 事件角标失败静默：不打断持仓主流程
    }
  }

  function opinionFor(row: { symbol: string; market: string }): OpinionSummaryRow | null {
    return state.opinions.get(`${row.symbol}:${row.market}`) || null
  }

  /** 角标只亮"近期变化"层的标签：整体立场进详情页看，列宽有限。 */
  function opinionBadgeTags(row: { symbol: string; market: string }): string[] {
    const opinion = opinionFor(row)
    if (!opinion) return []
    return opinion.tags.filter((tag) => OPINION_CHANGE_TAGS.has(tag))
  }

  async function loadOpinions() {
    try {
      const response = await api.listOpinionSummaries()
      const body = response.data as OpinionSummariesResponse
      const map = new Map<string, OpinionSummaryRow>()
      for (const row of body.items) {
        map.set(`${row.symbol}:${row.market}`, row)
      }
      state.opinions = map
    } catch {
      // 观点角标失败静默：不打断持仓主流程（数据源未接入时列表端点也返回 200）
    }
  }

  /** 行业（规则 > 官方 > 东方财富，后端已合成）；未取得返回 null */
  function industryFor(row: { symbol: string; market: string }): SecurityIndustryItem | null {
    const item = state.industries.get(`${row.symbol}:${row.market}`)
    return item?.industry ? item : null
  }

  async function loadIndustries() {
    try {
      const response = await api.listSecurityIndustries()
      const map = new Map<string, SecurityIndustryItem>()
      for (const item of response.data) map.set(`${item.symbol}:${item.market}`, item)
      state.industries = map
    } catch {
      // 行业失败静默：不打断持仓主流程
    }
  }

  function eventsFor(row: { symbol: string; market: string }): SecurityEvent[] {
    return state.events.get(`${row.symbol}:${row.market}`) || []
  }

  function upcomingEvent(row: { symbol: string; market: string }) {
    const today = todayLocalISODate()
    const upcoming = eventsFor(row).filter((event) => event.event_date >= today)
    if (!upcoming.length) return null
    const nearest = upcoming[0]
    // 两端都按本地日期解析再相减：UTC「今天」在北京时间 0-8 点会差一天（#219）
    const days = Math.round(
      (parseLocalDate(nearest.event_date).getTime() - parseLocalDate(today).getTime()) / 86400000
    )
    return {
      label: securityEventTypeLabel(nearest.event_type),
      daysText: days === 0 ? '今天' : `${days}天后`,
      date: nearest.event_date
    }
  }

  function eventTooltip(row: { symbol: string; market: string }): string {
    const today = todayLocalISODate()
    return eventsFor(row)
      .filter((event) => event.event_date >= today)
      .map((event) => `${event.event_date} ${securityEventTypeLabel(event.event_type)}`)
      .join('；')
  }

  /** 标签筛选（filters.ts）的输入：行业 + AI 全部标签 + 风险 + 观点全部标签 + 未来事件类型 */
  function tagSourceOf(row: { symbol: string; market: string }): HoldingTagSource {
    const analysis = analysisFor(row)
    const today = todayLocalISODate()
    return {
      industry: industryFor(row)?.industry ?? null,
      aiTags: analysis?.tags || [],
      riskLevel: analysis?.risk_level || null,
      opinionTags: opinionFor(row)?.tags || [],
      eventTypes: eventsFor(row)
        .filter((event) => event.event_date >= today)
        .map((event) => event.event_type)
    }
  }

  return reactive({
    state,
    riskLabels: RISK_LABELS,
    riskTagType,
    analysisFor,
    opinionFor,
    opinionBadgeTags,
    opinionTagType,
    loadAnalyses,
    loadEvents,
    loadOpinions,
    industryFor,
    loadIndustries,
    upcomingEvent,
    eventTooltip,
    tagSourceOf
  })
}

export type SecurityBadgesFeature = ReturnType<typeof useSecurityBadges>
