import { describe, expect, it } from 'vitest'
import { staleDurationText } from './useOpinions'

describe('staleDurationText', () => {
  const now = Date.parse('2026-09-26T12:00:00Z')

  it('latest_scan_at 缺失时是「长时间未更新」而不是「已  小时未更新」', () => {
    expect(staleDurationText(null, now)).toBe('长时间未更新')
    expect(staleDurationText(undefined, now)).toBe('长时间未更新')
    expect(staleDurationText('not-a-date', now)).toBe('长时间未更新')
  })

  it('两天内按小时，更久按天', () => {
    expect(staleDurationText('2026-09-26T02:00:00Z', now)).toBe('已 10 小时未更新')
    expect(staleDurationText('2026-09-20T12:00:00Z', now)).toBe('已 6 天未更新')
  })
})
