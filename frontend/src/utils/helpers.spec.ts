// 展示格式化纯函数（#142：此前完全无测试）。formatDate/formatDateTime 依赖
// Node 自带 ICU 的 zh-CN locale——CI 与本地均为 full-icu，输出确定。
import { describe, expect, test } from 'vitest'
import { COLOR } from '@/styles/tokens'
import {
  formatCurrency,
  formatDate,
  formatNumber,
  formatPercent,
  profitColor,
  toNumber
} from './helpers'

describe('formatNumber', () => {
  test('formats with zh-CN thousands separators and fixed decimals', () => {
    expect(formatNumber(1234567.891)).toBe('1,234,567.89')
    expect(formatNumber('1234567.891', 1)).toBe('1,234,567.9')
    expect(formatNumber(0)).toBe('0.00')
  })

  test('null/undefined render as a dash, not 0', () => {
    expect(formatNumber(null)).toBe('-')
    expect(formatNumber(undefined)).toBe('-')
  })
})

describe('toNumber', () => {
  test('parses numeric strings and passes numbers through', () => {
    expect(toNumber('12.5')).toBe(12.5)
    expect(toNumber(3)).toBe(3)
  })

  test('null/undefined/garbage fall back to 0', () => {
    expect(toNumber(null)).toBe(0)
    expect(toNumber(undefined)).toBe(0)
    expect(toNumber('abc')).toBe(0)
  })
})

describe('formatPercent', () => {
  test('positive values carry an explicit plus sign', () => {
    expect(formatPercent(3.456)).toBe('+3.46%')
    expect(formatPercent(0)).toBe('+0.00%')
  })

  test('negative values keep the minus sign', () => {
    expect(formatPercent(-1.2)).toBe('-1.20%')
  })

  test('null/undefined/NaN render as unsigned zero', () => {
    expect(formatPercent(null)).toBe('0.00%')
    expect(formatPercent(undefined)).toBe('0.00%')
    expect(formatPercent('not-a-number')).toBe('0.00%')
  })
})

describe('formatCurrency', () => {
  test('maps known currencies to their symbols', () => {
    expect(formatCurrency(1000)).toBe('¥1,000.00')
    expect(formatCurrency(1000, 'USD')).toBe('$1,000.00')
    expect(formatCurrency(1000, 'HKD')).toBe('HK$1,000.00')
    expect(formatCurrency(1000, 'SGD')).toBe('S$1,000.00')
  })

  test('unknown currency degrades to a bare number, null amount to a dash', () => {
    expect(formatCurrency(1000, 'JPY')).toBe('1,000.00')
    expect(formatCurrency(null)).toBe('¥-')
  })
})

describe('formatDate', () => {
  test('zero-padded zh-CN date; empty values render as a dash', () => {
    expect(formatDate('2026-01-05')).toBe('2026/01/05')
    expect(formatDate(null)).toBe('-')
    expect(formatDate('')).toBe('-')
  })
})

describe('profitColor', () => {
  test('zero and gains are success-colored, losses danger-colored', () => {
    expect(profitColor(0)).toBe(COLOR.success)
    expect(profitColor(12.3)).toBe(COLOR.success)
    expect(profitColor(-0.01)).toBe(COLOR.danger)
  })
})
