/**
 * 标的角标 feature（issue #140）：持仓列表的附加信息——
 * AI 标的分析摘要（标签列；点击名称跳转详情页）、标的事件角标
 * （未来 90 天事件及近 7 天无除权日的分红预案）、雪球观点、行业分类（#235）与近 7 天
 * 重要官方公告（#306，useRecentAnnouncements 与观察清单共用）。
 *
 * 两者都是锦上添花：加载失败一律静默，不打断持仓主流程。
 */

import { reactive, watch } from 'vue'
import api from '@/api'
import { useXueqiuCapabilitiesStore } from '@/stores/xueqiuCapabilities'
import { useRecentAnnouncements } from '@/composables/useRecentAnnouncements'
import { formatDate, todayLocalISODate } from '@/utils/helpers'
import { formatLocalDate, parseLocalDate } from '@/utils/dateRange'
import type {
  AnalysisSummaryRow,
  OpinionSummariesResponse,
  OpinionSummaryRow,
  SecurityEvent,
  SecurityIndustryItem
} from '@/types'
import { OPINION_CHANGE_TAGS, opinionTagStyle } from '../opinions/opinionTags'
import { analysisTagType, riskLabel, riskTagType } from '../security-detail/analysisTags'
import { isAnalysisOutdated } from '../security-detail/format'
import { securityEventTypeLabel } from '@/utils/labels'
import type { HoldingTagSource } from './filters'

export function useSecurityBadges() {
  const capabilities = useXueqiuCapabilitiesStore()
  const state = reactive({
    analyses: new Map<string, AnalysisSummaryRow>(),
    events: new Map<string, SecurityEvent[]>(),
    opinions: new Map<string, OpinionSummaryRow>(),
    industries: new Map<string, SecurityIndustryItem>()
  })

  const announcements = useRecentAnnouncements()

  function analysisFor(row: { symbol: string; market: string }): AnalysisSummaryRow | null {
    return state.analyses.get(`${row.symbol}:${row.market}`) || null
  }

  /** 分析早于最新的财报摘要/报表抽取 = 没吃到最新数据（与详情页「可能过期」同一判据） */
  function analysisOutdated(row: { symbol: string; market: string }): boolean {
    const analysis = analysisFor(row)
    return !!analysis && isAnalysisOutdated(analysis.created_at, analysis.latest_data_at)
  }

  async function loadAnalyses() {
    try {
      const response = await api.listSecurityAnalyses()
      const map = new Map<string, AnalysisSummaryRow>()
      for (const row of response.data) {
        map.set(`${row.symbol}:${row.market}`, row)
      }
      state.analyses = map
    } catch {
      // 标签列失败静默：不打断持仓主流程
    }
  }

  async function loadEvents() {
    try {
      const response = await api.getSecurityEvents({ days_ahead: 90, days_back: 7 })
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
    if (!capabilities.showOpinions) return null
    return state.opinions.get(`${row.symbol}:${row.market}`) || null
  }

  /** 角标只亮"近期变化"层的标签：整体立场进详情页看，列宽有限。 */
  function opinionBadgeTags(row: { symbol: string; market: string }): string[] {
    const opinion = opinionFor(row)
    if (!opinion) return []
    return opinion.tags.filter((tag) => OPINION_CHANGE_TAGS.has(tag))
  }

  async function loadOpinions() {
    if (!capabilities.showOpinions) return
    try {
      const response = await api.listOpinionSummaries()
      const body = response.data
      const map = new Map<string, OpinionSummaryRow>()
      for (const row of body.items) {
        map.set(`${row.symbol}:${row.market}`, row)
      }
      state.opinions = map
    } catch {
      // 观点角标失败静默：不打断持仓主流程（数据源未接入时列表端点也返回 200）
    }
  }

  watch(
    () => capabilities.showOpinions,
    (visible) => {
      if (visible) void loadOpinions()
    }
  )

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

  function visibleEventsFor(row: { symbol: string; market: string }): SecurityEvent[] {
    const today = todayLocalISODate()
    const recentStart = parseLocalDate(today)
    recentStart.setDate(recentStart.getDate() - 7)
    const recentStartDate = formatLocalDate(recentStart)
    return eventsFor(row).filter(
      (event) =>
        event.event_date >= today ||
        (event.event_type === 'DIVIDEND_PLAN' &&
          event.payload?.date_basis === 'announcement' &&
          event.event_date >= recentStartDate)
    )
  }

  function upcomingEvent(row: { symbol: string; market: string }) {
    const today = todayLocalISODate()
    const visible = visibleEventsFor(row)
    // 优先未来日程；只有近期预案时显示最新公告，不能把公告日当除权日。
    const nearest =
      visible.find(
        (event) => event.event_date >= today && event.payload?.date_basis !== 'announcement'
      ) || visible.at(-1)
    if (!nearest) return null
    // 两端都按本地日期解析再相减：UTC「今天」在北京时间 0-8 点会差一天（#219）
    const days = Math.round(
      (parseLocalDate(nearest.event_date).getTime() - parseLocalDate(today).getTime()) / 86400000
    )
    return {
      label: securityEventTypeLabel(nearest.event_type),
      daysText:
        nearest.payload?.date_basis === 'announcement'
          ? '已公告'
          : days === 0
            ? '今天'
            : `${days}天后`,
      date: nearest.event_date
    }
  }

  function eventTooltip(row: { symbol: string; market: string }): string {
    return visibleEventsFor(row)
      .map(
        (event) =>
          `${formatDate(event.event_date)} ${securityEventTypeLabel(event.event_type)}${event.payload?.date_basis === 'announcement' ? '（公告日，除权除息日未公布）' : ''}`
      )
      .join('；')
  }

  /** 标签筛选（filters.ts）的输入：行业 + AI 全部标签 + 风险 + 观点全部标签 + 可见事件类型 */
  function tagSourceOf(row: { symbol: string; market: string }): HoldingTagSource {
    const analysis = analysisFor(row)
    return {
      industry: industryFor(row)?.industry ?? null,
      aiTags: analysis?.tags || [],
      riskLevel: analysis?.risk_level || null,
      opinionTags: opinionFor(row)?.tags || [],
      eventTypes: visibleEventsFor(row).map((event) => event.event_type)
    }
  }

  return reactive({
    state,
    riskLabel,
    riskTagType,
    analysisTagType,
    analysisFor,
    analysisOutdated,
    opinionFor,
    opinionBadgeTags,
    opinionTagStyle,
    loadAnalyses,
    loadEvents,
    loadOpinions,
    loadAnnouncements: announcements.load,
    announcementBadge: announcements.badgeFor,
    industryFor,
    loadIndustries,
    upcomingEvent,
    eventTooltip,
    tagSourceOf
  })
}

export type SecurityBadgesFeature = ReturnType<typeof useSecurityBadges>
