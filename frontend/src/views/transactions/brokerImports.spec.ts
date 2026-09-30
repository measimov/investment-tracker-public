import { describe, expect, it, vi } from 'vitest'

vi.mock('@/api', () => ({ default: {} }))

import type { BrokerImportResult } from '@/types'
import {
  BROKER_IMPORTS,
  brokerImportHasIssues,
  brokerImportSummary,
  isBrokerImportMode
} from './brokerImports'

const result = {
  imported_transactions: 3,
  imported_corporate_actions: 1,
  imported_tax_adjustments: 2,
  imported_cash_events: 0,
  duplicate_rows: 5,
  batch_status: 'COMPLETED',
  reconciliation_status: null,
  errors: []
} as unknown as BrokerImportResult

describe('brokerImports（#284）', () => {
  it('提示文案按券商取税行与现金行叫法', () => {
    expect(brokerImportSummary('ibkr', result)).toBe(
      '导入交易 3 条，公司行动 1 条，预扣税调整 2 条，现金事件 0 条，跳过重复 5 条'
    )
    expect(brokerImportSummary('eastmoney', result)).toContain('组合费 0 条')
  })

  it('诊断报告文件名按券商区分', () => {
    expect(new Set(Object.values(BROKER_IMPORTS).map((spec) => spec.diagnosticsPrefix)).size).toBe(
      3
    )
  })

  it('部分入账 / 失败 / 对账不一致 / 行级错误都算未达标', () => {
    expect(brokerImportHasIssues(result)).toBe(false)
    expect(brokerImportHasIssues({ ...result, batch_status: 'PARTIAL' })).toBe(true)
    expect(brokerImportHasIssues({ ...result, reconciliation_status: 'MISMATCHED' })).toBe(true)
    expect(brokerImportHasIssues({ ...result, errors: ['x'] })).toBe(true)
  })

  it('只有三家券商是券商模式', () => {
    expect(isBrokerImportMode('cmb')).toBe(true)
    expect(isBrokerImportMode('standard')).toBe(false)
  })
})
