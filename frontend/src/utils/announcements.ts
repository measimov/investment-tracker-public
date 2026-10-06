/**
 * 官方公告（巨潮 / 披露易 / EDGAR，#306）的展示纯函数：详情页「公告」tab、持仓与观察清单徽标共用。
 * 类别与后端 announcement_classifier.CATEGORIES 对应（组本身带 category_label，这里只供筛选项）。
 */

import type { AnnouncementGroup, SecurityAnnouncements } from '@/types'
import { formatDate } from './helpers'

export const ANNOUNCEMENT_CATEGORIES: { value: string; label: string }[] = [
  { value: 'financing', label: '融资' },
  { value: 'restructuring', label: '重组/收购' },
  { value: 'earnings_alert', label: '业绩预告/快报' },
  { value: 'suspension', label: '停复牌/风险警示' },
  { value: 'legal', label: '诉讼/处罚/监管' },
  { value: 'buyback', label: '回购' },
  { value: 'shareholding', label: '增减持/权益变动' },
  { value: 'incentive', label: '股权激励' },
  { value: 'dividend', label: '分红' },
  { value: 'periodic', label: '定期报告' },
  { value: 'management', label: '董事/高管变动' },
  { value: 'governance', label: '股东会/治理' },
  { value: 'other', label: '其他' }
]

export type ImportanceFilter = 'major' | 'normal' | 'all'

export const IMPORTANCE_FILTER_OPTIONS: { value: ImportanceFilter; label: string }[] = [
  { value: 'major', label: '重要' },
  { value: 'normal', label: '重要+一般' },
  { value: 'all', label: '全部' }
]

const IMPORTANCE_LABELS: Record<string, string> = { major: '重要', normal: '一般', minor: '例行' }
const SOURCE_LABELS: Record<string, string> = {
  cninfo: '巨潮资讯',
  hkexnews: '披露易',
  edgar: 'SEC EDGAR'
}

export function importanceLabel(importance: string): string {
  return IMPORTANCE_LABELS[importance] ?? importance
}

export function importanceTagType(importance: string): 'danger' | 'warning' | 'info' {
  if (importance === 'major') return 'danger'
  if (importance === 'normal') return 'warning'
  return 'info'
}

export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source
}

/** 只放行 http(s) 原文链接（链接来自第三方站点）；不可用时返回 null。 */
export function safeAnnouncementUrl(url: string | null | undefined): string | null {
  const trimmed = (url ?? '').trim()
  return /^https?:\/\//i.test(trimmed) ? trimmed : null
}

/** 同步状态提示：未同步与「没有公告」必须分得开。 */
export function syncStatusText(
  body: Pick<SecurityAnnouncements, 'sync_status' | 'last_synced' | 'unsupported_reason'>
): { type: 'info' | 'warning'; text: string } {
  if (body.sync_status === 'unsupported') {
    return {
      type: 'info',
      text: `该标的没有可用的官方公告源${body.unsupported_reason ? `（${body.unsupported_reason}）` : ''}`
    }
  }
  if (body.sync_status === 'pending') {
    return {
      type: 'warning',
      text: '尚未同步官方公告：只同步持仓与观察清单里的标的，新加入的标的要等下一轮同步（约 30 分钟）'
    }
  }
  return { type: 'info', text: `已同步至 ${formatDate(body.last_synced)}，每 30 分钟增量更新` }
}

/** 按公告日分段（组已按公告日倒序）：时间线的日期小标题。 */
export function groupsByDate(
  groups: AnnouncementGroup[]
): { date: string; groups: AnnouncementGroup[] }[] {
  const sections: { date: string; groups: AnnouncementGroup[] }[] = []
  for (const group of groups) {
    const last = sections[sections.length - 1]
    if (last && last.date === group.ann_date) last.groups.push(group)
    else sections.push({ date: group.ann_date, groups: [group] })
  }
  return sections
}

/** 翻页游标：最后一组的公告日（后端按整日取完，下一页取更早的日期）。 */
export function nextBeforeDate(groups: AnnouncementGroup[]): string | null {
  return groups.length ? groups[groups.length - 1].ann_date : null
}

/** 持仓/观察清单徽标：近期重要公告的条数与悬浮说明（无则 null）。 */
export function announcementBadge(
  groups: AnnouncementGroup[] | undefined
): { text: string; lines: string[] } | null {
  if (!groups?.length) return null
  const lines = groups
    .slice(0, 5)
    .map(
      (group) =>
        `${group.ann_date.slice(5)} ${group.category_label}：${group.title}` +
        (group.document_count > 1 ? `（${group.document_count} 份）` : '')
    )
  if (groups.length > 5) lines.push(`…另 ${groups.length - 5} 组`)
  return {
    text: groups.length > 1 ? `公告·${groups.length}` : `公告·${groups[0].category_label}`,
    lines
  }
}
