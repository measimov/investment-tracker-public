/**
 * 「更新雪球 Cookie」对话框的纯函数（有 spec）。
 *
 * Cookie 是登录凭证：这里的函数只看格式与长度、只产出名称/天数/文案，
 * 任何返回值都不包含 Cookie 值——提示文案里也不回显用户粘贴的内容。
 */

import type { XueqiuCookieAdminStatus, XueqiuCookiePrimaryFact } from '@/types'

/** 与后端 MAX_CONTENT_BYTES 同口径（真实导出约 2–10KB）。 */
export const MAX_COOKIE_CONTENT_BYTES = 256 * 1024
export const COOKIE_TOO_LARGE_MESSAGE = `内容过大（上限 ${MAX_COOKIE_CONTENT_BYTES / 1024}KB），请确认选对了文件`

export type CookieInputFormat = 'empty' | 'json' | 'header'

/** 粗判粘贴内容的格式（仅用于提示；权威解析在后端）。 */
export function detectCookieFormat(text: string | null | undefined): CookieInputFormat {
  const trimmed = (text ?? '').replace(/^﻿/, '').trim()
  if (!trimmed) return 'empty'
  return /^[[{"]/.test(trimmed) ? 'json' : 'header'
}

export function cookieFormatHint(format: CookieInputFormat): string {
  if (format === 'json') return '识别为 JSON（浏览器插件导出），将保留到期时间用于提前告警'
  if (format === 'header') {
    return '识别为请求头格式（a=b; c=d）：不含到期时间，建议勾选「更新后探活」'
  }
  return ''
}

function utf8Length(text: string): number {
  return new TextEncoder().encode(text).length
}

/** 提交前的本地检查：返回错误文案（不含内容），可提交返回 null。 */
export function cookieContentError(text: string | null | undefined): string | null {
  const value = text ?? ''
  if (detectCookieFormat(value) === 'empty') return '请粘贴 Cookie 或选择导出的 JSON 文件'
  if (utf8Length(value) > MAX_COOKIE_CONTENT_BYTES) {
    return COOKIE_TOO_LARGE_MESSAGE
  }
  return null
}

/** 只接受 .json / .txt（插件导出是 .json；有人会把请求头存成 .txt）。 */
export function isAcceptedCookieFile(name: string | null | undefined): boolean {
  return /\.(json|txt)$/i.test((name ?? '').trim())
}

export function cookieSourceLabel(source: XueqiuCookieAdminStatus['source'] | undefined): string {
  if (source === 'file') return 'Cookie 文件（XUEQIU_COOKIE_FILE）'
  if (source === 'inline') return '环境变量内联（XUEQIU_COOKIES）'
  return '未配置'
}

const LEVEL_LABELS: Record<string, string> = {
  normal: '正常',
  warning: '即将到期',
  critical: '失效',
  unconfigured: '无法预判到期'
}

export function cookieLevelLabel(level: string | null | undefined): string {
  if (!level) return '未知'
  return LEVEL_LABELS[level] ?? level
}

const FORMAT_LABELS: Record<string, string> = {
  j2team: '浏览器插件导出（J2Team）',
  json_list: 'Cookie 数组 JSON',
  json_dict: '{name: value} JSON',
  header: '请求头（a=b; c=d）'
}

export function cookieFormatLabel(format: string | null | undefined): string {
  if (!format) return '—'
  return FORMAT_LABELS[format] ?? format
}

/** 单个主凭证的一句话：「xq_a_token 剩 12.3 天」「xqat 缺失」「xqat 无到期时间」。 */
export function primaryFactText(fact: XueqiuCookiePrimaryFact): string {
  if (!fact.present) return `${fact.name} 缺失`
  if (fact.days_left === null || fact.days_left === undefined) return `${fact.name} 无到期时间`
  if (fact.days_left <= 0) return `${fact.name} 已过期 ${Math.abs(fact.days_left).toFixed(1)} 天`
  return `${fact.name} 剩 ${fact.days_left.toFixed(1)} 天`
}

export function primaryExpirySummary(facts: XueqiuCookiePrimaryFact[] | null | undefined): string {
  if (!facts || facts.length === 0) return '—'
  return facts.map(primaryFactText).join(' · ')
}

/** Cookie 名称列表（只有名字）；太长时截断并注明总数。 */
export function cookieKeysText(keys: string[] | null | undefined, limit = 12): string {
  if (!keys || keys.length === 0) return '—'
  if (keys.length <= limit) return keys.join('、')
  return `${keys.slice(0, limit).join('、')} 等 ${keys.length} 个`
}
