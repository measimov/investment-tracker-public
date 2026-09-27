/**
 * 雪球按标的采集（公告/讨论、今日热帖）的展示纯函数（有 spec）。
 *
 * 标的详情「雪球公告 / 讨论」与观点页「今日热帖」共用：标题/摘要截断、外链白名单、
 * 数据新旧提示。时间一律交给 `formatDateTime`（毫秒时间戳），不在这里切字符串。
 */

import type { XueqiuFeedPost } from '@/types'

export const FEED_EXCERPT_CHARS = 160
/** 每日一轮：超过这个时长没有新一轮就提示「可能停摆」（留出一次 WAF 冷却/重启的余量） */
export const SYMBOL_FEED_STALE_HOURS = 36

type PostLike = Pick<XueqiuFeedPost, 'title' | 'text'>

/** 折叠空白后截断，超长补省略号。 */
export function truncateText(text: string | null | undefined, max: number): string {
  const flat = (text ?? '').replace(/\s+/g, ' ').trim()
  if (flat.length <= max) return flat
  return `${flat.slice(0, Math.max(0, max - 1))}…`
}

/** 列表主行：有标题用标题，否则取正文开头；都没有给占位。 */
export function postHeadline(post: PostLike, max = 60): string {
  const title = truncateText(post.title, max)
  if (title) return title
  const text = truncateText(post.text, max)
  return text || '（无正文）'
}

/** 摘要行：有标题时展示正文摘要；没有标题时主行已是正文开头，摘要给更长的一段。 */
export function postExcerpt(post: PostLike, max = FEED_EXCERPT_CHARS): string {
  if (!truncateText(post.title, 1)) {
    const text = truncateText(post.text, max)
    return text === truncateText(post.text, 60) ? '' : text
  }
  return truncateText(post.text, max)
}

/** 只放行 http(s) 外链（href 来自第三方站点内容，挡掉 javascript: 等协议）。 */
export function isSafeExternalUrl(url: string | null | undefined): boolean {
  return /^https?:\/\//i.test((url ?? '').trim())
}

/** 可展示的附件外链（缺字段按空，非 http(s) 剔除）。 */
export function safeLinks(post: Pick<XueqiuFeedPost, 'links'>): string[] {
  return (post.links ?? []).filter((link) => isSafeExternalUrl(link))
}

/** 发帖时间：0/缺失 → null（formatDateTime 显示占位符）。 */
export function postTimeMs(post: Pick<XueqiuFeedPost, 'created_at_ms'>): number | null {
  return post.created_at_ms > 0 ? post.created_at_ms : null
}

/**
 * 按标的采集的新旧提示：从未跑过 / 超过 SYMBOL_FEED_STALE_HOURS 未更新 → 提示文案，
 * 否则空串。
 */
export function feedFreshnessHint(
  lastFinishedAt: string | null | undefined,
  now: number = Date.now()
): string {
  if (!lastFinishedAt) return '按标的采集尚未运行过（每日一轮，由采集器进程执行）'
  const finished = new Date(lastFinishedAt).getTime()
  if (Number.isNaN(finished)) return ''
  const hours = (now - finished) / 3_600_000
  if (hours <= SYMBOL_FEED_STALE_HOURS) return ''
  return `按标的采集已 ${Math.floor(hours / 24)} 天未更新，采集器可能已停摆`
}
