import { describe, expect, it } from 'vitest'
import type { SuspectedDuplicateSample } from '@/types'
import {
  isSuspectedCashRow,
  supportsSuspectedConfirm,
  suspectedAlertTitle,
  suspectedExistingSource,
  suspectedRowTypeLabel
} from './suspectedDuplicates'

function sample(overrides: Partial<SuspectedDuplicateSample> = {}): SuspectedDuplicateSample {
  return {
    row_number: 5,
    symbol: '00883',
    market: '港股',
    transaction_type: 'BUY',
    trade_date: '2026-07-03',
    quantity: '200',
    amount: '-1055.10',
    price: '41.36',
    row_hash: 'a'.repeat(64),
    previously_held: false,
    ...overrides
  }
}

describe('suspectedDuplicates', () => {
  it('只有招商与 IBKR 走确认流程', () => {
    expect(supportsSuspectedConfirm('cmb')).toBe(true)
    expect(supportsSuspectedConfirm('ibkr')).toBe(true)
    expect(supportsSuspectedConfirm('eastmoney')).toBe(false)
  })

  it('行类型：成交/股息/预扣税', () => {
    expect(suspectedRowTypeLabel('BUY')).toBe('买入')
    expect(suspectedRowTypeLabel('CASH_DIVIDEND')).toBe('现金股息')
    expect(suspectedRowTypeLabel('DIVIDEND_TAX')).toBe('预扣税')
  })

  it('标题按模式切换，全部为上次归档时提示重复跳过', () => {
    expect(suspectedAlertTitle('ibkr', 2, 2)).toContain('股息/预扣税')
    expect(suspectedAlertTitle('cmb', 2, 2)).toContain('成交价精度不同')
    expect(suspectedAlertTitle('ibkr', 0, 3)).toBe(
      '3 条此前归档的疑似重复流水仍待确认（本次按重复跳过）'
    )
  })

  it('已入账来源：IBKR 来源说明优先，招商退回文件名+行号', () => {
    expect(suspectedExistingSource(sample({ existing_source: '手工录入' }))).toBe('手工录入')
    expect(
      suspectedExistingSource(
        sample({ existing_source_filename: 'cmb.pdf', existing_row_number: 12 })
      )
    ).toBe('cmb.pdf 第 12 行')
    expect(suspectedExistingSource(sample())).toBe('—')
  })

  it('股息/预扣税行不按成交价展示', () => {
    expect(isSuspectedCashRow(sample({ transaction_type: 'CASH_DIVIDEND' }))).toBe(true)
    expect(isSuspectedCashRow(sample({ transaction_type: 'DIVIDEND_TAX' }))).toBe(true)
    expect(isSuspectedCashRow(sample())).toBe(false)
  })
})
