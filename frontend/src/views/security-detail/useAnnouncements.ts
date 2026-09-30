/**
 * 标的详情页「公告」tab 的取数（官方公告，#306；只读库）。
 *
 * 与 useXueqiuSymbolFeed 同一套保护：op 计数收回旧请求所有权——筛选切换或路由换标的后，
 * 旧响应不得写进新状态；翻页按最后一组的公告日（后端按整日取完）。
 */

import { reactive } from 'vue'
import api from '@/api'
import type { AnnouncementGroup, SecurityAnnouncements } from '@/types'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { nextBeforeDate, type ImportanceFilter } from '@/utils/announcements'

export const ANNOUNCEMENT_PAGE_SIZE = 30

type SyncInfo = Pick<SecurityAnnouncements, 'sync_status' | 'last_synced' | 'unsupported_reason'>

export function useAnnouncements({
  symbol,
  market,
  isUnmounted
}: {
  symbol: () => string
  market: () => string
  isUnmounted: () => boolean
}) {
  const state = reactive({
    groups: [] as AnnouncementGroup[],
    sync: null as SyncInfo | null,
    importance: 'normal' as ImportanceFilter,
    category: '' as string,
    hasMore: false,
    loading: false,
    loadingMore: false,
    error: ''
  })
  let op = 0

  async function fetchPage(before: string | null) {
    const response = await api.getSecurityAnnouncements(market(), symbol(), {
      importance: state.importance,
      category: state.category || undefined,
      before: before || undefined,
      limit: ANNOUNCEMENT_PAGE_SIZE
    })
    return response.data
  }

  /** 从头加载（首次打开、筛选变化、换标的）。 */
  async function load() {
    const current = ++op
    const owns = () => !isUnmounted() && current === op
    state.loading = true
    state.loadingMore = false
    state.error = ''
    try {
      const body = await fetchPage(null)
      if (!owns()) return
      state.groups = body.groups
      state.hasMore = body.has_more
      state.sync = {
        sync_status: body.sync_status,
        last_synced: body.last_synced,
        unsupported_reason: body.unsupported_reason
      }
    } catch (error) {
      if (owns()) {
        state.groups = []
        state.hasMore = false
        state.error = getApiErrorMessage(error, '公告加载失败')
      }
    } finally {
      if (owns()) state.loading = false
    }
  }

  async function loadMore() {
    if (state.loading || state.loadingMore || !state.hasMore) return
    const current = op
    const owns = () => !isUnmounted() && current === op
    state.loadingMore = true
    try {
      const body = await fetchPage(nextBeforeDate(state.groups))
      if (!owns()) return
      state.groups = [...state.groups, ...body.groups]
      state.hasMore = body.has_more
    } catch (error) {
      if (owns()) state.error = getApiErrorMessage(error, '更早的公告加载失败')
    } finally {
      if (owns()) state.loadingMore = false
    }
  }

  /** 换标的：收回旧请求所有权并清空（筛选保留）。 */
  function reset() {
    op += 1
    state.groups = []
    state.sync = null
    state.hasMore = false
    state.loading = false
    state.loadingMore = false
    state.error = ''
  }

  return { state, load, loadMore, reset }
}
