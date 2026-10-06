// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createPinia, disposePinia, setActivePinia, type Pinia } from 'pinia'
import api from '@/api'
import type { SecurityEvent } from '@/types'
import { useSecurityBadges } from './useSecurityBadges'

vi.mock('@/api', () => ({ default: { getSecurityEvents: vi.fn() } }))

let pinia: Pinia
beforeEach(() => {
  localStorage.clear()
  pinia = createPinia()
  setActivePinia(pinia)
})

const security = { symbol: '600036', market: 'A股' }
function event(date: string, type = 'DIVIDEND_PLAN', announcement = true): SecurityEvent {
  return {
    ...security,
    id: 1,
    event_type: type,
    event_date: date,
    payload: announcement ? { date_basis: 'announcement', div_proc: '预案' } : null,
    source: 'tushare-dividend'
  }
}

afterEach(() => {
  disposePinia(pinia)
  vi.useRealTimers()
  vi.clearAllMocks()
})

it.each(['2026-10-01', '2026-10-02'])(
  '%s 的预案按公告日展示，不把过去的除权或披露事件当成未来日程',
  async (planDate) => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(2026, 9, 2, 12))
    vi.mocked(api.getSecurityEvents).mockResolvedValue({
      data: [
        event('2026-09-24'),
        event('2026-09-25'),
        event('2026-09-29', 'EARNINGS_DISCLOSURE', false),
        event('2026-09-30', 'DIVIDEND_PLAN', false),
        event(planDate)
      ]
    } as never)
    const badges = useSecurityBadges()
    await badges.loadEvents()
    expect(api.getSecurityEvents).toHaveBeenCalledWith({ days_ahead: 90, days_back: 7 })
    expect(badges.upcomingEvent(security)).toEqual({
      label: '分红预案',
      daysText: '已公告',
      date: planDate
    })
    expect(badges.eventTooltip(security)).toBe(
      `2026/09/25 分红预案（公告日，除权除息日未公布）；${planDate.replace(/-/g, '/')} 分红预案（公告日，除权除息日未公布）`
    )
    expect(badges.tagSourceOf(security).eventTypes).toEqual(['DIVIDEND_PLAN', 'DIVIDEND_PLAN'])
  }
)

it.each([
  ['2026-10-01', 'EARNINGS_DISCLOSURE', '财报披露'],
  ['2026-10-02', 'EARNINGS_DISCLOSURE', '财报披露'],
  ['2026-10-02', 'SHARE_UNLOCK', '限售解禁']
])('%s 的预案不会挤掉明天的%s日程', async (planDate, scheduleType, label) => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date(2026, 9, 2, 12))
  vi.mocked(api.getSecurityEvents).mockResolvedValue({
    data: [event(planDate), event('2026-10-03', scheduleType, false)]
  } as never)
  const badges = useSecurityBadges()
  await badges.loadEvents()
  expect(badges.upcomingEvent(security)).toEqual({
    label,
    daysText: '1天后',
    date: '2026-10-03'
  })
  expect(badges.eventTooltip(security)).toContain('除权除息日未公布')
})
