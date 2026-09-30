/**
 * 观点页数据层：标的维度概览（listOpinionSummaries）+ 作者维度动态
 * （getOpinionFeed）+ 数据源新鲜度状态。
 *
 * 数据源是本仓雪球采集器（独立的 xueqiu-collector 进程）写入的表：
 * source_available=false（采集器从未成功运行且无发言）与 freshness.stale（采集停摆）
 * 都要如实展示为状态条——这正是"上游挂了要能看出来"的第一出口；采集器自身的
 * 状态在 CollectorCard（useCollector）。
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

// 标签样式单独成文件（有 spec）；这里保留旧导出名，持仓页角标等调用方不用改
export { OPINION_CHANGE_TAGS, opinionTagStyle, opinionTagType } from './opinionTags'

/**
 * 数据源停更文案：latest_scan_at 为 null（从未扫描到/表空）时不能拼出「已  小时未更新」，
 * 改为「长时间未更新」。纯函数，now 注入便于测试。
 */
export function staleDurationText(
  latestScanAt: string | null | undefined,
  now = Date.now()
): string {
  if (!latestScanAt) return '长时间未更新'
  const time = new Date(latestScanAt).getTime()
  if (!Number.isFinite(time)) return '长时间未更新'
  const hours = Math.max(0, Math.round((now - time) / 3600_000))
  if (hours >= 48) return `已 ${Math.round(hours / 24)} 天未更新`
  return `已 ${hours} 小时未更新`
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

  const staleText = computed(() => staleDurationText(state.freshness?.latest_scan_at))

  async function loadSummaries() {
    state.loading = true
    state.loadError = ''
    try {
      const response = await api.listOpinionSummaries()
      const body = response.data
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

  return { state, staleText, loadSummaries, loadFeed }
}
