import { describe, expect, it } from 'vitest'

import { buildRateCards, rateAgeDays } from './rateCards'

describe('rateAgeDays', () => {
  it('按本地日期相减', () => {
    expect(rateAgeDays('2026-09-26', '2026-09-26')).toBe(0)
    expect(rateAgeDays('2026-09-19', '2026-09-26')).toBe(7)
    expect(rateAgeDays('2026-06-26', '2026-09-26')).toBe(92)
    expect(rateAgeDays(null, '2026-09-26')).toBeNull()
  })
})

describe('buildRateCards', () => {
  const latest = {
    base_currency: 'CNY',
    rates: { CNY: '1', USD: '7.1234', SGD: '5.3' },
    effective_date: '2026-09-26',
    source: 'api',
    details: {
      USD: { rate: '7.1234', effective_date: '2026-09-25', source: 'api' },
      SGD: { rate: '5.3', effective_date: '2026-06-20', source: 'manual' }
    }
  }

  it('每个币种用自己的日期与来源，超过 7 天标记过期', () => {
    const cards = buildRateCards(latest, '2026-09-26')
    expect(cards.map((card) => card.currency)).toEqual(['USD', 'SGD'])
    expect(cards[0]).toMatchObject({
      rate: 7.1234,
      effectiveDate: '2026-09-25',
      source: 'api',
      stale: false
    })
    expect(cards[1]).toMatchObject({
      effectiveDate: '2026-06-20',
      source: 'manual',
      ageDays: 98,
      stale: true
    })
  })

  it('恰好 7 天不算过期，8 天算', () => {
    const one = (date: string) =>
      buildRateCards(
        { ...latest, details: { USD: { rate: '7', effective_date: date, source: 'api' } } },
        '2026-09-26'
      ).find((card) => card.currency === 'USD')
    expect(one('2026-09-19')?.stale).toBe(false)
    expect(one('2026-09-18')?.stale).toBe(true)
  })

  it('旧后端无 details：退回顶层日期/来源且不判过期', () => {
    const legacy = { ...latest, details: undefined }
    const cards = buildRateCards(legacy, '2027-01-01')
    expect(cards[0]).toMatchObject({ effectiveDate: '2026-09-26', source: 'api', stale: false })
  })

  it('details 里缺某币种：不借用别的币种的日期', () => {
    const partial = {
      ...latest,
      details: { USD: { rate: '7.1234', effective_date: '2026-09-25', source: 'api' } }
    }
    const sgd = buildRateCards(partial, '2026-09-26').find((card) => card.currency === 'SGD')
    expect(sgd).toMatchObject({ effectiveDate: null, source: null, stale: false })
  })
})
