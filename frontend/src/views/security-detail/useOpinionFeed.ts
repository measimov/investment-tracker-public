/**
 * 标的详情页「相关作者动态」的取数与所有权管理（评审 P2）。
 *
 * 两层保护，缺一不可：
 * - **同上下文防重复 in-flight**：折叠区快速收起再展开时，首个请求未归
 *   不再发第二个（每次都是 90 天全表扫描，重复纯浪费）。
 * - **op 所有权令牌**：路由切标的时 reset() 收回旧请求所有权——旧响应
 *   既不能写 authors/loaded，其 finally 也不能把新标的请求的 loading
 *   清成 false（跨标的竞态）。只有当前所有者能写任何状态。
 */

import { ref } from 'vue'
import type { CollapseModelValue } from 'element-plus'
import api from '@/api'
import type { OpinionFeedAuthor } from '@/types'

export const OPINION_FEED_DAYS = 90
export const OPINION_FEED_PER_AUTHOR = 20

export function useOpinionFeed({
  symbol,
  market,
  isUnmounted
}: {
  symbol: () => string
  market: () => string
  isUnmounted: () => boolean
}) {
  const authors = ref<OpinionFeedAuthor[]>([])
  const loading = ref(false)
  const loaded = ref(false)
  const openPanels = ref<string[]>([])
  let op = 0

  async function loadFeed() {
    if (loaded.value || loading.value) return
    const current = ++op
    const owns = () => !isUnmounted() && current === op
    loading.value = true
    try {
      const response = await api.getOpinionFeed({
        symbol: symbol(),
        market: market(),
        days: OPINION_FEED_DAYS,
        per_author: OPINION_FEED_PER_AUTHOR
      })
      if (!owns()) return
      authors.value = (response.data?.authors || []) as OpinionFeedAuthor[]
      loaded.value = true
    } catch {
      // 动态流是锦上添花：失败静默，展开时显示空态
    } finally {
      if (owns()) loading.value = false
    }
  }

  function onToggle(open: CollapseModelValue) {
    if (Array.isArray(open) ? open.length > 0 : Boolean(open)) loadFeed()
  }

  /** 路由换标的时调用：收回旧请求所有权并清空展示状态。 */
  function reset() {
    op += 1
    authors.value = []
    loaded.value = false
    loading.value = false
    openPanels.value = []
  }

  return { authors, loading, loaded, openPanels, onToggle, loadFeed, reset }
}
