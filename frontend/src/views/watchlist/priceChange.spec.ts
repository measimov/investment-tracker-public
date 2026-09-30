import { describe, expect, it } from 'vitest'
import { addedPriceBasisLabel, describeChangeSinceAdded, missingChangeReason } from './priceChange'

describe('describeChangeSinceAdded', () => {
  it('formats rise with basis tooltip', () => {
    const info = describeChangeSinceAdded({
      change_since_added_pct: 0.052,
      added_price: '10.00000000',
      added_price_date: '2026-09-18',
      added_price_basis: 'close_on_add',
      current_price: '10.52'
    })
    expect(info).not.toBeNull()
    expect(info!.text).toBe('+5.20%')
    expect(info!.direction).toBe('up')
    expect(info!.tooltip[0]).toBe('基准价 10.00（加入日收盘，2026/09/18）')
    expect(info!.tooltip[1]).toBe('现价 10.52')
  })

  it('formats fall and flat', () => {
    expect(describeChangeSinceAdded({ change_since_added_pct: -0.1 })!.text).toBe('-10.00%')
    expect(describeChangeSinceAdded({ change_since_added_pct: -0.1 })!.direction).toBe('down')
    expect(describeChangeSinceAdded({ change_since_added_pct: 0 })!.direction).toBe('flat')
  })

  it('returns null when change is unknown', () => {
    expect(describeChangeSinceAdded({ change_since_added_pct: null })).toBeNull()
    expect(describeChangeSinceAdded({})).toBeNull()
  })

  it('keeps unknown basis date readable', () => {
    const info = describeChangeSinceAdded({
      change_since_added_pct: 0.01,
      added_price: 5,
      added_price_basis: 'quote'
    })
    expect(info!.tooltip[0]).toBe('基准价 5.00（加入时报价，日期未知）')
  })
})

describe('basis labels and missing reasons', () => {
  it('maps known basis and passes through unknown', () => {
    expect(addedPriceBasisLabel('first_quote')).toBe('加入后首次报价')
    expect(addedPriceBasisLabel('other')).toBe('other')
    expect(addedPriceBasisLabel(null)).toBe('未知口径')
  })

  it('explains why change is missing', () => {
    expect(missingChangeReason({ current_price: null })).toContain('尚未取到现价')
    expect(missingChangeReason({ current_price: '1.2' })).toContain('按加入日收盘补齐')
    expect(
      missingChangeReason({ current_price: '1.2', added_price_basis: 'pending_quote' })
    ).toContain('下一次成功刷新的报价将作为基准')
    expect(addedPriceBasisLabel('close_after_add')).toContain('加入后首个收盘')
  })
})
