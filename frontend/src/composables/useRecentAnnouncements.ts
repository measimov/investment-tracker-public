/**
 * 当前用户持仓 ∪ 观察清单近 7 天的重要公告（#306），按 "symbol:market" 分组：
 * 持仓页与观察清单的「公告」徽标共用。锦上添花的数据，加载失败一律静默。
 */

import { shallowRef } from 'vue'
import api from '@/api'
import type { AnnouncementGroup } from '@/types'
import { announcementBadge } from '@/utils/announcements'

export const RECENT_ANNOUNCEMENT_DAYS = 7

export function useRecentAnnouncements() {
  const byKey = shallowRef(new Map<string, AnnouncementGroup[]>())

  async function load() {
    try {
      const response = await api.getRecentAnnouncements({
        days: RECENT_ANNOUNCEMENT_DAYS,
        importance: 'major'
      })
      const map = new Map<string, AnnouncementGroup[]>()
      for (const group of response.data.groups) {
        const key = `${group.symbol}:${group.market}`
        const list = map.get(key) || []
        list.push(group)
        map.set(key, list)
      }
      byKey.value = map
    } catch {
      // 徽标失败静默：不打断主流程
    }
  }

  function badgeFor(row: { symbol: string; market: string }) {
    return announcementBadge(byKey.value.get(`${row.symbol}:${row.market}`))
  }

  return { byKey, load, badgeFor }
}
