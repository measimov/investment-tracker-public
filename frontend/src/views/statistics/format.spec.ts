import { describe, expect, it } from 'vitest'
import { EMPTY } from '@/utils/helpers'
import {
  formatNullableNumber,
  formatNullablePercent,
  formatPlainPercent,
  isShortRange,
  rangeSpanDays,
  signedNumber
} from './format'

describe('百分比正号规则', () => {
  it('有方向的百分比带正号，与 formatPercent 一致', () => {
    expect(formatNullablePercent(12.345)).toBe('+12.35%')
    expect(formatNullablePercent(-5)).toBe('-5.00%')
    expect(formatNullablePercent(null)).toBe(EMPTY)
  })

  it('无方向的比例不带正号', () => {
    expect(formatPlainPercent(55.5)).toBe('55.50%')
    expect(formatPlainPercent(undefined)).toBe(EMPTY)
  })

  it('el-statistic formatter：正数带 +、缺值占位', () => {
    expect(signedNumber(20)).toBe('+20.00')
    expect(signedNumber(-3.2)).toBe('-3.20')
    expect(signedNumber(1234.5)).toBe('+1,234.50')
    expect(signedNumber(null)).toBe(EMPTY)
  })

  it('formatNullableNumber 缺值占位', () => {
    expect(formatNullableNumber(1.234)).toBe('1.23')
    expect(formatNullableNumber(null)).toBe(EMPTY)
  })
})

describe('短区间年化标注', () => {
  it('rangeSpanDays 按本地日期计天数', () => {
    expect(rangeSpanDays('2026-08-26', '2026-09-26')).toBe(31)
    expect(rangeSpanDays(null, '2026-09-26')).toBeNull()
  })

  it('不足 180 天为短区间', () => {
    expect(isShortRange(31)).toBe(true)
    expect(isShortRange(179)).toBe(true)
    expect(isShortRange(180)).toBe(false)
    expect(isShortRange(null)).toBe(false)
  })
})
