/**
 * 标的详情页「雪球公告 / 讨论」的取数（采集器每日按标的落库的只读展示）。
 *
 * 与 useOpinionFeed 同一套保护：折叠区首次展开才拉取、同上下文 in-flight 不重复发、
 * 路由换标的时 reset() 收回旧请求所有权（旧响应不得写新标的的状态）。
 */

import { ref } from 'vue'
import type { CollapseModelValue } from 'element-plus'
import api from '@/api'
import type { XueqiuFeedPost } from '@/types'

export const XUEQIU_FEED_LIMIT = 20

export function useXueqiuSymbolFeed({
  symbol,
  market,
  isUnmounted
}: {
  symbol: () => string
  market: () => string
  isUnmounted: () => boolean
}) {
  const announcements = ref<XueqiuFeedPost[]>([])
  const discussions = ref<XueqiuFeedPost[]>([])
  const lastCycleFinishedAt = ref<string | null>(null)
  const loading = ref(false)
  const loaded = ref(false)
  const failed = ref(false)
  const openPanels = ref<string[]>([])
  let op = 0

  async function load() {
    if (loaded.value || loading.value) return
    const current = ++op
    const owns = () => !isUnmounted() && current === op
    loading.value = true
    failed.value = false
    try {
      const response = await api.getXueqiuSymbolFeed({
        symbol: symbol(),
        market: market(),
        limit: XUEQIU_FEED_LIMIT
      })
      if (!owns()) return
      announcements.value = response.data?.announcements ?? []
      discussions.value = response.data?.discussions ?? []
      lastCycleFinishedAt.value = response.data?.last_cycle_finished_at ?? null
      loaded.value = true
    } catch {
      // 锦上添花的数据：失败只在折叠区里提示，不弹全局通知
      if (owns()) failed.value = true
    } finally {
      if (owns()) loading.value = false
    }
  }

  function onToggle(open: CollapseModelValue) {
    if (Array.isArray(open) ? open.length > 0 : Boolean(open)) load()
  }

  /** 路由换标的时调用：收回旧请求所有权并清空展示状态。 */
  function reset() {
    op += 1
    announcements.value = []
    discussions.value = []
    lastCycleFinishedAt.value = null
    loaded.value = false
    loading.value = false
    failed.value = false
    openPanels.value = []
  }

  return {
    announcements,
    discussions,
    lastCycleFinishedAt,
    loading,
    loaded,
    failed,
    openPanels,
    onToggle,
    load,
    reset
  }
}
