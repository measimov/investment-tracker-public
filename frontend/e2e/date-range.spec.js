// 区间预设日期运算的纯函数测试（Node 直测，无浏览器）。
// 覆盖检视意见指出的两类缺陷：toISOString 的 UTC 偏移、setMonth 的月末溢出。
import { expect, test } from '@playwright/test'
import { formatLocalDate, monthsBefore, presetRangeParams } from '../src/utils/dateRange.js'

test.describe('dateRange utils', () => {
  test('formatLocalDate uses local calendar date, not UTC', () => {
    // 本地 0 点 30 分：toISOString 在正时区会回退到前一天，本地格式化不会
    expect(formatLocalDate(new Date(2026, 0, 1, 0, 30))).toBe('2026-01-01')
    expect(formatLocalDate(new Date(2026, 11, 31, 23, 59))).toBe('2026-12-31')
  })

  test('monthsBefore clamps to the last day of shorter months', () => {
    // 7月31日 - 1月 → 6月30日（setMonth 会溢出成 7月1日）
    expect(formatLocalDate(monthsBefore(new Date(2026, 6, 31), 1))).toBe('2026-06-30')
    // 3月31日 - 1月 → 2月28日
    expect(formatLocalDate(monthsBefore(new Date(2026, 2, 31), 1))).toBe('2026-02-28')
    // 闰年：2028-03-31 - 1月 → 2月29日
    expect(formatLocalDate(monthsBefore(new Date(2028, 2, 31), 1))).toBe('2028-02-29')
    // 常规日期不受影响
    expect(formatLocalDate(monthsBefore(new Date(2026, 6, 15), 3))).toBe('2026-04-15')
    // 跨年
    expect(formatLocalDate(monthsBefore(new Date(2026, 1, 28), 12))).toBe('2025-02-28')
  })

  test('presetRangeParams produces the expected windows', () => {
    const now = new Date(2026, 6, 31, 0, 30) // 本地 7月31日凌晨
    expect(presetRangeParams('1m', now)).toEqual({
      start_date: '2026-06-30',
      end_date: '2026-07-31'
    })
    expect(presetRangeParams('1y', now)).toEqual({
      start_date: '2025-07-31',
      end_date: '2026-07-31'
    })
    expect(presetRangeParams('ytd', now)).toEqual({
      start_date: '2026-01-01',
      end_date: '2026-07-31'
    })
    expect(presetRangeParams('all', now)).toBeNull()
  })
})
