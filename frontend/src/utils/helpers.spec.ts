// 展示格式化纯函数（#142：此前完全无测试）。formatDate/formatDateTime 依赖
// Node 自带 ICU 的 zh-CN locale——CI 与本地均为 full-icu，输出确定。
import { describe, expect, test } from 'vitest'
import {
  EMPTY,
  formatCurrency,
  formatDate,
  formatNumber,
  formatPercent,
  formatPlainPercent,
  formatPrice,
  formatQuantity,
  profitColor,
  pnlClass,
  toNumber
} from './helpers'

describe('formatNumber', () => {
  test('formats with zh-CN thousands separators and fixed decimals', () => {
    expect(formatNumber(1234567.891)).toBe('1,234,567.89')
    expect(formatNumber('1234567.891', 1)).toBe('1,234,567.9')
    expect(formatNumber(0)).toBe('0.00')
  })

  test('null/undefined/garbage render as the shared placeholder, not 0 or NaN', () => {
    expect(formatNumber(null)).toBe(EMPTY)
    expect(formatNumber(undefined)).toBe(EMPTY)
    expect(formatNumber('abc')).toBe(EMPTY)
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
    expect(formatPercent(0)).toBe('0.00%')
    expect(formatPercent(-0.004)).toBe('0.00%')
  })

  test('negative values keep the minus sign', () => {
    expect(formatPercent(-1.2)).toBe('-1.20%')
  })

  test('null/undefined/NaN render as the placeholder — missing data must not look like 0%', () => {
    expect(formatPercent(null)).toBe(EMPTY)
    expect(formatPercent(undefined)).toBe(EMPTY)
    expect(formatPercent('not-a-number')).toBe(EMPTY)
  })
})

describe('formatPlainPercent', () => {
  test.each([
    [0.02549 * 100, '2.5%'],
    [0.0255 * 100, '2.6%'],
    [0.02551 * 100, '2.6%'],
    [-0.0255 * 100, '-2.6%'],
    [0, '0.0%']
  ])('rounds percentage %s to one decimal without a plus sign', (value, expected) => {
    expect(formatPlainPercent(value, 1)).toBe(expected)
  })

  test.each([null, undefined, '', 'not-a-number', NaN])(
    'keeps unknown %s as a placeholder without a percent suffix',
    (value) => expect(formatPlainPercent(value, 1)).toBe(EMPTY)
  )
})

describe('formatCurrency', () => {
  test('maps known currencies to their symbols', () => {
    expect(formatCurrency(1000)).toBe('¥1,000.00')
    expect(formatCurrency(1000, 'USD')).toBe('$1,000.00')
    expect(formatCurrency(1000, 'HKD')).toBe('HK$1,000.00')
    expect(formatCurrency(1000, 'SGD')).toBe('S$1,000.00')
  })

  test('symbols come from CURRENCIES; unknown currency degrades to a bare number', () => {
    expect(formatCurrency(1000, 'EUR')).toBe('€1,000.00')
    expect(formatCurrency(1000, 'JPY')).toBe('JP¥1,000.00')
    expect(formatCurrency(1000, 'XYZ')).toBe('1,000.00')
  })

  test('minus sign goes before the currency symbol', () => {
    expect(formatCurrency(-48644.14)).toBe('-¥48,644.14')
    expect(formatCurrency(-7246.04, 'USD')).toBe('-$7,246.04')
    expect(formatCurrency('-4572.23', 'HKD')).toBe('-HK$4,572.23')
  })

  test('a negative amount that rounds to zero carries no minus sign', () => {
    expect(formatCurrency(-0.004)).toBe('¥0.00')
  })

  test('missing amount renders as the placeholder without a symbol', () => {
    expect(formatCurrency(null)).toBe(EMPTY)
    expect(formatCurrency(undefined, 'USD')).toBe(EMPTY)
  })
})

describe('formatQuantity', () => {
  test('integers carry no decimals; fractional shares keep up to 4 places', () => {
    expect(formatQuantity(63900)).toBe('63,900')
    expect(formatQuantity('500.0000')).toBe('500')
    expect(formatQuantity(12.5)).toBe('12.5')
    expect(formatQuantity(0.123456)).toBe('0.1235')
    expect(formatQuantity(null)).toBe(EMPTY)
  })
})

describe('formatPrice', () => {
  test('2 to 4 decimals, trailing zeros beyond the second trimmed', () => {
    expect(formatPrice(97.45)).toBe('97.45')
    expect(formatPrice('97.4500')).toBe('97.45')
    expect(formatPrice(1.409)).toBe('1.409')
    expect(formatPrice(2.545)).toBe('2.545')
    expect(formatPrice(0.085)).toBe('0.085')
    expect(formatPrice(436.6)).toBe('436.60')
    expect(formatPrice(null)).toBe(EMPTY)
  })
})

describe('formatDate', () => {
  test('zero-padded zh-CN date; empty values render as a dash', () => {
    expect(formatDate('2026-01-05')).toBe('2026/01/05')
    expect(formatDate(null)).toBe(EMPTY)
    expect(formatDate('')).toBe(EMPTY)
  })

  test('pure date strings are local dates (no UTC shift west of Greenwich)', () => {
    const original = process.env.TZ
    process.env.TZ = 'America/Los_Angeles'
    try {
      expect(formatDate('2026-01-05')).toBe('2026/01/05')
    } finally {
      process.env.TZ = original
    }
  })
})

describe('profitColor', () => {
  test('zero is neutral, gains success-colored, losses danger-colored', () => {
    expect(profitColor(0)).toBe('')
    expect(profitColor(-0)).toBe('')
    expect(pnlClass(null)).toBe('pnl-flat')
    expect(pnlClass('0')).toBe('pnl-flat')
    expect(pnlClass('1')).toBe('pnl-pos')
    expect(pnlClass(-1)).toBe('pnl-neg')
    expect(profitColor(12.3)).toBe('var(--app-success)')
    expect(profitColor(-0.01)).toBe('var(--app-danger)')
  })

  test('missing values are not colored as gains', () => {
    expect(profitColor(null)).toBe('')
    expect(profitColor(undefined)).toBe('')
  })
})
