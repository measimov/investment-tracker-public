/**
 * 标的角标 feature（issue #140）：持仓列表两类附加信息——
 * AI 标的分析摘要（标签列；点击名称跳转详情页）与标的事件角标
 * （未来 90 天：财报披露 / 分红预案 / 限售解禁）。
 *
 * 两者都是锦上添花：加载失败一律静默，不打断持仓主流程。
 */

import { reactive } from 'vue'
import api from '@/api'
import type { OpinionSummariesResponse, OpinionSummaryRow, SecurityEvent } from '@/types'
import { OPINION_CHANGE_TAGS, opinionTagType } from '../opinions/useOpinions'
import type { AnalysisSummaryRow } from './types'

export const RISK_LABELS: Record<string, string> = { low: '低', medium: '中', high: '高' }

const EVENT_TYPE_LABELS: Record<string, string> = {
  EARNINGS_DISCLOSURE: '财报披露',
  DIVIDEND_PLAN: '分红预案',
  SHARE_UNLOCK: '限售解禁'
}

export function useSecurityBadges() {
  const state = reactive({
    analyses: new Map<string, AnalysisSummaryRow>(),
    events: new Map<string, SecurityEvent[]>(),
    opinions: new Map<string, OpinionSummaryRow>()
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

  function eventsFor(row: { symbol: string; market: string }): SecurityEvent[] {
    return state.events.get(`${row.symbol}:${row.market}`) || []
  }

  function upcomingEvent(row: { symbol: string; market: string }) {
    const today = new Date().toISOString().slice(0, 10)
    const upcoming = eventsFor(row).filter((event) => event.event_date >= today)
    if (!upcoming.length) return null
    const nearest = upcoming[0]
    const days = Math.round(
      (new Date(nearest.event_date).getTime() - new Date(today).getTime()) / 86400000
    )
    return {
      label: EVENT_TYPE_LABELS[nearest.event_type] || nearest.event_type,
      daysText: days === 0 ? '今天' : `${days}天后`,
      date: nearest.event_date
    }
  }

  function eventTooltip(row: { symbol: string; market: string }): string {
    const today = new Date().toISOString().slice(0, 10)
    return eventsFor(row)
      .filter((event) => event.event_date >= today)
      .map(
        (event) => `${event.event_date} ${EVENT_TYPE_LABELS[event.event_type] || event.event_type}`
      )
      .join('；')
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
    upcomingEvent,
    eventTooltip
  })
}

export type SecurityBadgesFeature = ReturnType<typeof useSecurityBadges>
