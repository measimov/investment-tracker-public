/**
 * 三家券商对账单导入的差异表（#284）：ImportDialog 的预览与导入此前各三段 if/else，只差 API、
 * 文案与诊断文件名（诊断报告一律叫 cmb-import-diagnostics-…，IBKR/东财也用这个名字）。
 */

import api from '@/api'
import type { BrokerImportResult } from '@/types'

export type BrokerImportMode = 'cmb' | 'ibkr' | 'eastmoney'

interface BrokerImportSpec {
  /** 上传框 accept */
  accept: string
  /** 诊断报告文件名前缀 */
  diagnosticsPrefix: string
  /** 成功提示里税行与现金行的叫法 */
  taxLabel: string
  cashLabel: string
  /** 疑似重复确认（#190）：只有招商与 IBKR 支持回传 confirm hashes */
  preview(file: File, accountId: number, confirmed: string[]): Promise<{ data: BrokerImportResult }>
  commit(file: File, accountId: number, confirmed: string[]): Promise<{ data: BrokerImportResult }>
}

export const BROKER_IMPORTS: Record<BrokerImportMode, BrokerImportSpec> = {
  cmb: {
    accept: '.pdf',
    diagnosticsPrefix: 'cmb-import-diagnostics',
    taxLabel: '红利税调整',
    cashLabel: '现金事件',
    preview: (file, accountId, confirmed) => api.previewCmbFundFlows(file, accountId, confirmed),
    commit: (file, accountId, confirmed) => api.importCmbFundFlows(file, accountId, confirmed)
  },
  ibkr: {
    // 规范格式为 trade_history.xlsx；Activity CSV 保留供历史回填
    accept: '.csv,.xlsx',
    diagnosticsPrefix: 'ibkr-import-diagnostics',
    taxLabel: '预扣税调整',
    cashLabel: '现金事件',
    preview: (file, accountId, confirmed) => api.previewIbkrActivity(file, accountId, confirmed),
    commit: (file, accountId, confirmed) => api.importIbkrActivity(file, accountId, confirmed)
  },
  eastmoney: {
    accept: '.pdf',
    diagnosticsPrefix: 'eastmoney-import-diagnostics',
    taxLabel: '红利税调整',
    cashLabel: '组合费',
    preview: (file, accountId) => api.previewEastmoneyStatement(file, accountId),
    commit: (file, accountId) => api.importEastmoneyStatement(file, accountId)
  }
}

export function isBrokerImportMode(mode: string): mode is BrokerImportMode {
  return mode in BROKER_IMPORTS
}

/** 正式导入成功后的提示文案。 */
export function brokerImportSummary(mode: BrokerImportMode, result: BrokerImportResult): string {
  const spec = BROKER_IMPORTS[mode]
  return (
    `导入交易 ${result.imported_transactions} 条，` +
    `公司行动 ${result.imported_corporate_actions} 条，` +
    `${spec.taxLabel} ${result.imported_tax_adjustments} 条，` +
    `${spec.cashLabel} ${result.imported_cash_events || 0} 条，` +
    `跳过重复 ${result.duplicate_rows} 条`
  )
}

/** 导入结果是否未达完整入账标准（部分入账、失败、对账不一致或有行级错误）。 */
export function brokerImportHasIssues(result: BrokerImportResult): boolean {
  return (
    result.batch_status === 'PARTIAL' ||
    result.batch_status === 'FAILED' ||
    result.reconciliation_status === 'MISMATCHED' ||
    Boolean(result.errors?.length)
  )
}
