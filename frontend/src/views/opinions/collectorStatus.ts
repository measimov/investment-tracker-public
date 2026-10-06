/**
 * 采集器卡片的纯函数（有 spec）：总体健康判定、运行状态文案、Cookie 标签、作者 ID 校验。
 *
 * 健康判定的优先级即排障顺序：未启用 → 进程离线（心跳超时）→ Cookie 失效 →
 * WAF 冷却 → 上一轮结果。前面的条件成立时后面的信息都不可信。
 */

import type { CollectorCookieStatus, CollectorStatus, CollectorSymbolsStatus } from '@/types'

export type TagType = 'success' | 'warning' | 'danger' | 'info' | 'primary'

export interface CollectorHealth {
  type: TagType
  label: string
  hint: string
}

const RUN_STATUS_LABELS: Record<string, string> = {
  ok: '成功',
  partial: '部分成功',
  failed: '失败',
  error: '抓取失败',
  waf: '触发 WAF',
  interrupted: '已中断',
  running: '采集中',
  unavailable: '不可用',
  no_authors: '无作者',
  locked: '并发跳过'
}

const RUN_STATUS_TYPES: Record<string, TagType> = {
  ok: 'success',
  partial: 'warning',
  failed: 'danger',
  error: 'danger',
  waf: 'danger',
  interrupted: 'info',
  running: 'primary',
  unavailable: 'danger',
  no_authors: 'warning',
  locked: 'info'
}

export function runStatusLabel(status: string | null | undefined): string {
  if (!status) return '未运行'
  return RUN_STATUS_LABELS[status] ?? status
}

export function runStatusType(status: string | null | undefined): TagType {
  if (!status) return 'info'
  return RUN_STATUS_TYPES[status] ?? 'info'
}

export function cookieTagType(level: string | null | undefined): TagType {
  if (level === 'normal') return 'success'
  if (level === 'warning') return 'warning'
  if (level === 'critical') return 'danger'
  return 'info'
}

export function cookieLabel(cookie: CollectorCookieStatus | null | undefined): string {
  if (!cookie) return '未知'
  if (cookie.level === 'normal' || cookie.level === 'warning' || cookie.level === 'critical') {
    if (cookie.days_left !== null && cookie.days_left !== undefined) {
      return cookie.days_left > 0
        ? `剩 ${cookie.days_left.toFixed(1)} 天`
        : `已过期 ${Math.abs(cookie.days_left).toFixed(1)} 天`
    }
    return cookie.level === 'critical' ? '需检查' : cookie.level
  }
  return '无法预判到期'
}

export function collectorHealth(status: CollectorStatus | null | undefined): CollectorHealth {
  if (!status) return { type: 'info', label: '加载中', hint: '' }
  if (!status.enabled) {
    return {
      type: 'info',
      label: '未启用',
      hint: '采集器未启用（需管理员在部署配置中开启）；启用后每小时采集一轮。'
    }
  }
  if (!status.alive) {
    return {
      type: 'danger',
      label: '进程离线',
      hint: '采集器心跳超时：xueqiu-collector 容器可能已停止或卡死，请查看容器日志。'
    }
  }
  if (status.cookie?.level === 'critical') {
    const daysLeft = status.cookie.days_left
    return {
      type: daysLeft != null && daysLeft > 0 ? 'warning' : 'danger',
      label:
        daysLeft == null ? 'Cookie 需检查' : daysLeft > 0 ? 'Cookie 即将到期' : 'Cookie 已过期',
      hint: status.cookie.message
    }
  }
  if (status.waf_cooldown_until) {
    return {
      type: 'warning',
      label: 'WAF 冷却中',
      hint: '上一轮触发阿里云 WAF 挑战页，冷却结束前不会开新一轮。'
    }
  }
  if (status.run_pending) {
    return { type: 'primary', label: '等待运行', hint: '已请求立即运行，采集器将在 30 秒内开始。' }
  }
  const last = status.last_cycle_status
  if (!last) return { type: 'info', label: '尚未运行', hint: '' }
  const type = runStatusType(last)
  return {
    type,
    label: last === 'ok' ? '运行正常' : runStatusLabel(last),
    hint: last === 'ok' ? '' : status.last_cycle_message
  }
}

/** 雪球用户 ID：1–20 位数字（主页链接 xueqiu.com/u/<ID>）。与后端校验同口径。 */
export function isValidXueqiuUserId(value: string | null | undefined): boolean {
  return /^[0-9]{1,20}$/.test((value ?? '').trim())
}

export function xueqiuProfileUrl(userId: string): string {
  return `https://xueqiu.com/u/${encodeURIComponent(userId)}`
}

/** 雪球组合代号：两位字母 + 数字（如 ZH000001）。与后端校验同口径（大小写不敏感）。 */
export function isValidCubeId(value: string | null | undefined): boolean {
  return /^[A-Za-z]{2}[0-9]{1,20}$/.test((value ?? '').trim())
}

export function xueqiuCubeUrl(cubeId: string): string {
  return `https://xueqiu.com/P/${encodeURIComponent(cubeId)}`
}

function statCount(stats: Record<string, unknown> | undefined, ...path: string[]): number {
  let current: unknown = stats
  for (const key of path) {
    if (!current || typeof current !== 'object') return 0
    current = (current as Record<string, unknown>)[key]
  }
  return typeof current === 'number' && Number.isFinite(current) ? current : 0
}

/** 每日按标的采集一行摘要（采集器卡片头部）。 */
export function symbolsCycleSummary(symbols: CollectorSymbolsStatus | null | undefined): string {
  if (!symbols) return ''
  if (!symbols.enabled) return '按标的采集已关闭，需管理员配置开启'
  if (!symbols.last_status) return `按标的采集：每天 ${symbols.run_after} 后一轮，尚未运行`
  const stats = symbols.last_stats as Record<string, unknown> | undefined
  const parts = [`按标的采集：${runStatusLabel(symbols.last_status)}`]
  if (symbols.last_status !== 'running') {
    parts.push(`${statCount(stats, 'symbols')} 只标的`)
    parts.push(
      `新公告 ${statCount(stats, 'announcement', 'new')} / 新讨论 ${statCount(stats, 'discussion', 'new')}`
    )
    const failures = statCount(stats, 'failures')
    if (failures) parts.push(`失败 ${failures} 项`)
  }
  if (symbols.retry_pending) {
    const scope =
      symbols.retry_item_count === null || symbols.retry_item_count === undefined
        ? '整轮'
        : `${symbols.retry_item_count} 项`
    parts.push(`今日待重试 ${scope}（已尝试 ${symbols.retry_attempts ?? 0} 轮）`)
  }
  return parts.join(' · ')
}
