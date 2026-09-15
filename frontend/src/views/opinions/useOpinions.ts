/**
 * 观点页数据层：标的维度概览（listOpinionSummaries）+ 作者维度动态
 * （getOpinionFeed）+ 数据源新鲜度状态。
 *
 * 数据源是另一个项目（xueqiu-timeline-archiver）经 cron 写入同库的表：
 * source_available=false（未接入）与 freshness.stale（cron 停摆）都要如实
 * 展示为状态条——这正是"上游挂了本仓要能看出来"的第一出口。
 */

import { computed, reactive, ref } from 'vue'
import api from '@/api'
import { getApiErrorMessage } from '@/utils/apiErrors'
import type {
  OpinionFeedAuthor,
  OpinionFreshness,
  OpinionSummariesResponse,
  OpinionSummaryRow
} from '@/types'

// 近期变化类标签：角标与高亮共用（与后端 ALLOWED_OPINION_TAGS 的变化层一致）
export const OPINION_CHANGE_TAGS = new Set(['近期转多', '近期转空', '新增关注'])

export function opinionTagType(tag: string): 'danger' | 'success' | 'warning' | 'info' {
  if (tag === '近期转多' || tag === '一致看多' || tag === '偏多') return 'danger' // A股涨跌色：红=多
  if (tag === '近期转空' || tag === '一致看空' || tag === '偏空') return 'success' // 绿=空
  if (OPINION_CHANGE_TAGS.has(tag)) return 'warning'
  return 'info'
}

export function useOpinions() {
  const state = reactive({
    loading: false,
    feedLoading: false,
    loadError: '' as string,
    sourceAvailable: true,
    freshness: null as OpinionFreshness | null,
    recentDays: 30,
    items: [] as OpinionSummaryRow[],
    feedAuthors: [] as OpinionFeedAuthor[]
  })
  const feedLoaded = ref(false)

  const staleHoursText = computed(() => {
    const latest = state.freshness?.latest_scan_at
    if (!latest) return ''
    const hours = Math.round((Date.now() - new Date(latest).getTime()) / 3600_000)
    return `${hours}`
  })

  async function loadSummaries() {
    state.loading = true
    state.loadError = ''
    try {
      const response = await api.listOpinionSummaries()
      const body = response.data as OpinionSummariesResponse
      state.sourceAvailable = body.source_available
      state.freshness = body.freshness
      state.recentDays = body.recent_days
      state.items = body.items
    } catch (error) {
      state.loadError = getApiErrorMessage(error, '观点概览加载失败')
    } finally {
      state.loading = false
    }
  }

  async function loadFeed(force = false) {
    if (feedLoaded.value && !force) return
    state.feedLoading = true
    try {
      const response = await api.getOpinionFeed({ days: state.recentDays, limit: 200 })
      state.feedAuthors = (response.data?.authors || []) as OpinionFeedAuthor[]
      feedLoaded.value = true
    } catch (error) {
      state.loadError = getApiErrorMessage(error, '作者动态加载失败')
    } finally {
      state.feedLoading = false
    }
  }

  return { state, staleHoursText, loadSummaries, loadFeed }
}
