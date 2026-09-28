import { describe, expect, it } from 'vitest'
import type { AlertItem, NotifyChannelSummary, NotifyResult } from '../../types'
import {
  channelStatus,
  durationText,
  notifyStatusText,
  severityLabel,
  severityTagType,
  sourceLabel,
  testResultMessage
} from './systemAlerts'

function summary(overrides: Partial<NotifyChannelSummary> = {}): NotifyChannelSummary {
  return {
    configured: true,
    count: 1,
    valid_count: 1,
    apprise_available: true,
    min_severity: 'warning',
    channels: [{ kind: 'bark', channel: 'barks://api.day.app/Ab***', valid: true }],
    ...overrides
  }
}

function alert(overrides: Partial<AlertItem> = {}): AlertItem {
  return {
    alert_key: 'xueqiu:cookie',
    source: 'xueqiu_cookie',
    severity: 'warning',
    status: 'active',
    title: '雪球 Cookie 即将过期',
    message: '',
    first_seen_at: '2026-09-28T00:00:00Z',
    last_seen_at: '2026-09-28T01:00:00Z',
    last_notified_at: null,
    notify_count: 0,
    resolved_at: null,
    last_notify: null,
    payload: {},
    ...overrides
  }
}

function result(overrides: Partial<NotifyResult> = {}): NotifyResult {
  return {
    ok: true,
    status: 'sent',
    message: '已发送到 1 个渠道',
    configured: 1,
    sent: 1,
    channels: [{ kind: 'bark', channel: 'barks://api.day.app/Ab***', ok: true, error: null }],
    ...overrides
  }
}

describe('severity / source labels', () => {
  it('maps severities to labels and tag types', () => {
    expect(severityLabel('critical')).toBe('严重')
    expect(severityTagType('critical')).toBe('danger')
    expect(severityTagType('warning')).toBe('warning')
    expect(severityTagType('bogus')).toBe('info')
    expect(sourceLabel('xueqiu_collector')).toBe('雪球采集器')
    expect(sourceLabel('custom')).toBe('custom')
  })
})

describe('channelStatus', () => {
  it('warns when no channel is configured', () => {
    const line = channelStatus(
      summary({ configured: false, count: 0, valid_count: 0, channels: [] })
    )
    expect(line.type).toBe('warning')
    expect(line.text).toContain('NOTIFY_URLS')
  })

  it('summarizes valid channels and the push threshold', () => {
    expect(channelStatus(summary())).toEqual({
      type: 'success',
      text: '已配置 1 个推送渠道（Bark）；警告及以上推送'
    })
  })

  it('flags unrecognized channels', () => {
    const line = channelStatus(
      summary({
        count: 2,
        valid_count: 1,
        channels: [
          { kind: 'bark', channel: 'barks://api.day.app/Ab***', valid: true },
          { kind: 'foo', channel: 'foo://***', valid: false }
        ]
      })
    )
    expect(line.type).toBe('warning')
    expect(line.text).toContain('1 个无法识别')
    const none = channelStatus(summary({ valid_count: 0, channels: [] }))
    expect(none.type).toBe('danger')
  })

  it('reports missing apprise', () => {
    expect(channelStatus(summary({ apprise_available: false })).type).toBe('danger')
  })
})

describe('notifyStatusText', () => {
  it('describes each push state', () => {
    expect(notifyStatusText(alert({ notify_count: 2 }), 'warning')).toBe('已推送 2 次')
    expect(notifyStatusText(alert({ severity: 'info' }), 'warning')).toBe('仅记录（低于推送门槛）')
    expect(notifyStatusText(alert({ last_notify: { status: 'unconfigured' } }), 'warning')).toBe(
      '未推送（未配置渠道）'
    )
    expect(notifyStatusText(alert({ last_notify: { status: 'failed' } }), 'warning')).toContain(
      '重试'
    )
    expect(notifyStatusText(alert(), 'warning')).toBe('待推送')
  })
})

describe('testResultMessage', () => {
  it('maps statuses to message types', () => {
    expect(testResultMessage(result())).toEqual({ type: 'success', text: '已发送到 1 个渠道' })
    expect(
      testResultMessage(
        result({ ok: false, status: 'unconfigured', message: '未配置', channels: [] })
      ).type
    ).toBe('warning')
    const partial = testResultMessage(
      result({
        status: 'partial',
        message: '部分发送成功：1/2 个渠道',
        channels: [
          { kind: 'bark', channel: 'a', ok: true, error: null },
          { kind: 'bark', channel: 'b', ok: false, error: '发送失败' }
        ]
      })
    )
    expect(partial).toEqual({ type: 'warning', text: '部分发送成功：1/2 个渠道（b：发送失败）' })
    const failed = testResultMessage(
      result({
        ok: false,
        status: 'failed',
        message: '发送失败：1 个渠道均未成功',
        channels: [{ kind: 'bark', channel: 'a', ok: false, error: 'RuntimeError' }]
      })
    )
    expect(failed).toEqual({ type: 'danger', text: '发送失败：1 个渠道均未成功：RuntimeError' })
  })
})

describe('durationText', () => {
  it('formats minutes, hours and days', () => {
    expect(durationText('2026-09-28T00:00:00Z', '2026-09-28T00:45:00Z')).toBe('45 分钟')
    expect(durationText('2026-09-28T00:00:00Z', '2026-09-28T25:00:00Z')).toBe('')
    expect(durationText('2026-09-28T00:00:00Z', '2026-09-29T01:30:00Z')).toBe('25 小时 30 分钟')
    expect(durationText('2026-09-25T00:00:00Z', '2026-09-28T02:00:00Z')).toBe('3 天 2 小时')
    expect(durationText(null, '2026-09-28T00:00:00Z')).toBe('')
  })
})
