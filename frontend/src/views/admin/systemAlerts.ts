import type { AlertItem, NotifyChannelSummary, NotifyResult } from '../../types'

/** 「系统告警」页的纯展示逻辑（有 spec）：严重度/来源文案、渠道状态、推送结果。 */

export type Severity = AlertItem['severity']
type TagType = 'info' | 'warning' | 'danger' | 'success'

export const SEVERITY_LABELS: Record<Severity, string> = {
  info: '提示',
  warning: '警告',
  critical: '严重'
}

const SEVERITY_RANK: Record<Severity, number> = { info: 0, warning: 1, critical: 2 }

const SEVERITY_TAGS: Record<Severity, TagType> = {
  info: 'info',
  warning: 'warning',
  critical: 'danger'
}

// 与后端 alert_checks.SOURCE_* / alert_service.SOURCE_EXTERNAL 对应
const SOURCE_LABELS: Record<string, string> = {
  xueqiu_collector: '雪球采集器',
  xueqiu_cookie: '雪球 Cookie',
  fx: '汇率数据源',
  periodic_tasks: '周期任务',
  background_jobs: '后台任务',
  alert_checks: '告警检查器',
  external: '外部信号'
}

export function severityLabel(severity: string): string {
  return SEVERITY_LABELS[severity as Severity] ?? severity
}

export function severityTagType(severity: string): TagType {
  return SEVERITY_TAGS[severity as Severity] ?? 'info'
}

export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source
}

export interface StatusLine {
  type: TagType
  text: string
}

/** 推送渠道的一句话状态（页首提示条） */
export function channelStatus(summary: NotifyChannelSummary): StatusLine {
  if (!summary.configured) {
    return {
      type: 'warning',
      text: '未配置推送渠道（NOTIFY_URLS 为空）：告警只在本页记录，不会推送到手机'
    }
  }
  if (!summary.apprise_available) {
    return { type: 'danger', text: '后端未安装 apprise，无法推送；请重新构建镜像' }
  }
  const invalid = summary.count - summary.valid_count
  const kinds = summary.channels
    .filter((item) => item.valid)
    .map((item) => (item.kind === 'bark' ? 'Bark' : item.kind))
  const kindText = kinds.length ? `（${[...new Set(kinds)].join('、')}）` : ''
  const threshold = `${severityLabel(summary.min_severity)}及以上推送`
  if (invalid > 0) {
    return {
      type: summary.valid_count > 0 ? 'warning' : 'danger',
      text: `已配置 ${summary.count} 个渠道，其中 ${invalid} 个无法识别${kindText}；${threshold}`
    }
  }
  return { type: 'success', text: `已配置 ${summary.count} 个推送渠道${kindText}；${threshold}` }
}

/** 单条告警的推送情况 */
export function notifyStatusText(item: AlertItem, minSeverity: string): string {
  const rank = SEVERITY_RANK[item.severity] ?? 0
  const threshold = SEVERITY_RANK[minSeverity as Severity] ?? SEVERITY_RANK.warning
  if (item.notify_count > 0) return `已推送 ${item.notify_count} 次`
  if (rank < threshold) return '仅记录（低于推送门槛）'
  const status = item.last_notify?.status
  if (status === 'unconfigured') return '未推送（未配置渠道）'
  if (status === 'failed') return '推送失败，下一轮检查重试'
  return '待推送'
}

/** 「发送测试通知」的结果提示 */
export function testResultMessage(result: NotifyResult): StatusLine {
  if (result.status === 'sent') return { type: 'success', text: result.message }
  if (result.status === 'partial') {
    const failed = result.channels.filter((item) => !item.ok)
    const detail = failed.map((item) => `${item.channel}：${item.error ?? '失败'}`).join('；')
    return { type: 'warning', text: `${result.message}（${detail}）` }
  }
  if (result.status === 'unconfigured') return { type: 'warning', text: result.message }
  const detail = result.channels.map((item) => item.error).filter(Boolean)
  return {
    type: 'danger',
    text: detail.length ? `${result.message}：${[...new Set(detail)].join('；')}` : result.message
  }
}

/** 持续时长：「3 天 2 小时」「45 分钟」；缺时间返回空串 */
export function durationText(
  from: string | null | undefined,
  to: string | null | undefined
): string {
  if (!from || !to) return ''
  const minutes = Math.max(Math.floor((Date.parse(to) - Date.parse(from)) / 60000), 0)
  if (Number.isNaN(minutes)) return ''
  if (minutes < 60) return `${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) {
    const rest = minutes % 60
    return rest ? `${hours} 小时 ${rest} 分钟` : `${hours} 小时`
  }
  const days = Math.floor(hours / 24)
  const restHours = hours % 24
  return restHours ? `${days} 天 ${restHours} 小时` : `${days} 天`
}
