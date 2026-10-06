import { describe, expect, it } from 'vitest'
import type { CollectorStatus, CollectorSymbolsStatus } from '@/types'
import {
  collectorHealth,
  cookieLabel,
  cookieTagType,
  isValidCubeId,
  isValidXueqiuUserId,
  runStatusLabel,
  runStatusType,
  symbolsCycleSummary,
  xueqiuCubeUrl
} from './collectorStatus'

function status(overrides: Partial<CollectorStatus> = {}): CollectorStatus {
  return {
    enabled: true,
    alive: true,
    heartbeat_at: '2026-09-27T07:00:00Z',
    cycle_minutes: 60,
    last_cycle_started_at: '2026-09-27T06:00:00Z',
    last_cycle_finished_at: '2026-09-27T06:30:00Z',
    last_cycle_status: 'ok',
    last_cycle_message: '',
    last_waf_at: null,
    waf_cooldown_until: null,
    run_requested_at: null,
    run_pending: false,
    cookie: { level: 'normal', message: 'ok', days_left: 10, cookie: 'xq_a_token' },
    recent_runs: [],
    authors: [],
    symbols: symbolsStatus(),
    cubes: [],
    ...overrides
  }
}

function symbolsStatus(overrides: Partial<CollectorSymbolsStatus> = {}): CollectorSymbolsStatus {
  return {
    enabled: true,
    run_after: '07:30',
    last_started_at: null,
    last_finished_at: null,
    last_status: '',
    last_message: '',
    last_business_date: null,
    last_stats: {},
    run_requested_at: null,
    run_pending: false,
    retry_pending: false,
    retry_attempts: 0,
    retry_item_count: null,
    ...overrides
  }
}

describe('symbolsCycleSummary', () => {
  it('关闭 / 未运行 / 运行中 / 有结果', () => {
    expect(symbolsCycleSummary(null)).toBe('')
    expect(symbolsCycleSummary(symbolsStatus({ enabled: false }))).toContain('已关闭')
    expect(symbolsCycleSummary(symbolsStatus())).toBe('按标的采集：每天 07:30 后一轮，尚未运行')
    expect(symbolsCycleSummary(symbolsStatus({ last_status: 'running' }))).toBe(
      '按标的采集：采集中'
    )
    expect(
      symbolsCycleSummary(
        symbolsStatus({
          last_status: 'partial',
          last_stats: {
            symbols: 44,
            announcement: { fetched: 300, new: 12 },
            discussion: { fetched: 880, new: 97 },
            failures: 2
          }
        })
      )
    ).toBe('按标的采集：部分成功 · 44 只标的 · 新公告 12 / 新讨论 97 · 失败 2 项')
  })

  it('当天有待重试项时如实说明（失败不会被当成当天已跑完）', () => {
    const base = {
      last_status: 'partial',
      last_stats: { symbols: 2, failures: 1 },
      retry_pending: true,
      retry_attempts: 1
    }
    expect(symbolsCycleSummary(symbolsStatus({ ...base, retry_item_count: 1 }))).toContain(
      '今日待重试 1 项（已尝试 1 轮）'
    )
    expect(symbolsCycleSummary(symbolsStatus({ ...base, retry_item_count: null }))).toContain(
      '今日待重试 整轮'
    )
  })

  it('统计字段缺失或类型不对按 0', () => {
    expect(
      symbolsCycleSummary(
        symbolsStatus({ last_status: 'ok', last_stats: { symbols: 'x', announcement: null } })
      )
    ).toBe('按标的采集：成功 · 0 只标的 · 新公告 0 / 新讨论 0')
  })
})

describe('isValidCubeId / xueqiuCubeUrl', () => {
  it('两位字母加数字，大小写不敏感', () => {
    expect(isValidCubeId('ZH000001')).toBe(true)
    expect(isValidCubeId(' zh009440 ')).toBe(true)
    expect(isValidCubeId('ZH')).toBe(false)
    expect(isValidCubeId('9440')).toBe(false)
    expect(isValidCubeId('https://xueqiu.com/P/ZH000001')).toBe(false)
    expect(xueqiuCubeUrl('ZH000001')).toBe('https://xueqiu.com/P/ZH000001')
  })
})

describe('collectorHealth', () => {
  it('按排障顺序判定：未启用 > 离线 > Cookie 失效 > WAF 冷却 > 上一轮结果', () => {
    expect(collectorHealth(status({ enabled: false, alive: false })).label).toBe('未启用')
    expect(collectorHealth(status({ alive: false })).type).toBe('danger')
    expect(collectorHealth(status({ alive: false })).label).toBe('进程离线')
    expect(
      collectorHealth(
        status({
          cookie: { level: 'critical', message: '缺失', days_left: null, cookie: 'xqat' },
          waf_cooldown_until: '2026-09-27T08:00:00Z'
        })
      ).label
    ).toBe('Cookie 需检查')
    expect(collectorHealth(status({ waf_cooldown_until: '2026-09-27T08:00:00Z' })).label).toBe(
      'WAF 冷却中'
    )
    expect(collectorHealth(status({ run_pending: true })).label).toBe('等待运行')
    expect(collectorHealth(status()).label).toBe('运行正常')
    expect(collectorHealth(status({ last_cycle_status: '' })).label).toBe('尚未运行')
  })

  it.each([
    [0.5, 'Cookie 即将到期', 'warning'],
    [0, 'Cookie 已过期', 'danger'],
    [-0.5, 'Cookie 已过期', 'danger'],
    [null, 'Cookie 需检查', 'danger']
  ] as const)('critical Cookie 剩余 %s 天如实区分到期状态', (days_left, label, type) => {
    const health = collectorHealth(
      status({
        cookie: { level: 'critical', message: '请更新凭证', days_left, cookie: 'xq_a_token' }
      })
    )
    expect(health).toEqual({ label, type, hint: '请更新凭证' })
  })

  it('非 ok 的上一轮把消息带出来', () => {
    const health = collectorHealth(
      status({ last_cycle_status: 'partial', last_cycle_message: '1000000003:failed(超时)' })
    )
    expect(health.type).toBe('warning')
    expect(health.hint).toContain('超时')
  })

  it('空状态不抛错', () => {
    expect(collectorHealth(null).label).toBe('加载中')
  })
})

describe('run/cookie labels', () => {
  it('未知状态原样显示、缺省为未运行', () => {
    expect(runStatusLabel('waf')).toBe('触发 WAF')
    expect(runStatusLabel('mystery')).toBe('mystery')
    expect(runStatusLabel('')).toBe('未运行')
    expect(runStatusType('failed')).toBe('danger')
    expect(runStatusLabel('error')).toBe('抓取失败')
    expect(runStatusType('error')).toBe('danger')
    expect(runStatusType(undefined)).toBe('info')
  })

  it('Cookie 剩余天数与过期', () => {
    expect(cookieLabel({ level: 'normal', message: '', days_left: 12.34, cookie: '' })).toBe(
      '剩 12.3 天'
    )
    expect(cookieLabel({ level: 'critical', message: '', days_left: -2, cookie: '' })).toBe(
      '已过期 2.0 天'
    )
    expect(cookieLabel({ level: 'critical', message: '', days_left: null, cookie: '' })).toBe(
      '需检查'
    )
    expect(cookieLabel({ level: 'unconfigured', message: '', days_left: null, cookie: '' })).toBe(
      '无法预判到期'
    )
    expect(cookieTagType('warning')).toBe('warning')
    expect(cookieTagType('unconfigured')).toBe('info')
  })
})

describe('isValidXueqiuUserId', () => {
  it('只接受 1-20 位 ASCII 数字（与后端同口径）', () => {
    expect(isValidXueqiuUserId('1000000001')).toBe(true)
    expect(isValidXueqiuUserId(' 1000000001 ')).toBe(true)
    expect(isValidXueqiuUserId('')).toBe(false)
    expect(isValidXueqiuUserId('abc')).toBe(false)
    expect(isValidXueqiuUserId('１２３')).toBe(false)
    expect(isValidXueqiuUserId('1'.repeat(21))).toBe(false)
    expect(isValidXueqiuUserId('https://xueqiu.com/u/1')).toBe(false)
  })
})
