/** 导入与导出：标准 CSV/Excel、三家券商对账单的预览与导入。 */
import { apiClient, uploadFile } from './client'
import type { StandardImportResult } from '@/types'

export const importsApi = {
  // Import/Export
  importCSV(file: File | Blob, brokerAccountId: number | string | null = null) {
    return uploadFile<StandardImportResult>('/import/csv', file, {
      broker_account_id: brokerAccountId
    })
  },
  importExcel(file: File | Blob, brokerAccountId: number | string | null = null) {
    return uploadFile<StandardImportResult>('/import/excel', file, {
      broker_account_id: brokerAccountId
    })
  },
  importCorporateActionsCSV(file: File | Blob, brokerAccountId: number | string | null = null) {
    return uploadFile<StandardImportResult>('/import/corporate-actions/csv', file, {
      broker_account_id: brokerAccountId
    })
  },
  importCorporateActionsExcel(file: File | Blob, brokerAccountId: number | string | null = null) {
    return uploadFile<StandardImportResult>('/import/corporate-actions/excel', file, {
      broker_account_id: brokerAccountId
    })
  },
  // confirmHashes：用户确认为真实成交的疑似重复行（#190），空数组时不发该字段
  previewCmbFundFlows(
    file: File | Blob,
    brokerAccountId: number | string | null = null,
    confirmHashes: string[] = []
  ) {
    return uploadFile('/import/cmb-fund-flows/preview', file, {
      broker_account_id: brokerAccountId,
      confirm_suspected_row_hashes: confirmHashes.join(',')
    })
  },
  importCmbFundFlows(
    file: File | Blob,
    brokerAccountId: number | string | null = null,
    confirmHashes: string[] = []
  ) {
    return uploadFile('/import/cmb-fund-flows', file, {
      broker_account_id: brokerAccountId,
      confirm_suspected_row_hashes: confirmHashes.join(',')
    })
  },
  // confirmHashes：与招商同一表单字段——用户确认为真实的另一笔的疑似重复行
  previewIbkrActivity(
    file: File | Blob,
    brokerAccountId: number | string | null = null,
    confirmHashes: string[] = []
  ) {
    return uploadFile('/import/ibkr-activity/preview', file, {
      broker_account_id: brokerAccountId,
      confirm_suspected_row_hashes: confirmHashes.join(',')
    })
  },
  importIbkrActivity(
    file: File | Blob,
    brokerAccountId: number | string | null = null,
    confirmHashes: string[] = []
  ) {
    return uploadFile('/import/ibkr-activity', file, {
      broker_account_id: brokerAccountId,
      confirm_suspected_row_hashes: confirmHashes.join(',')
    })
  },
  previewEastmoneyStatement(file: File | Blob, brokerAccountId: number | string | null = null) {
    return uploadFile('/import/eastmoney-statement/preview', file, {
      broker_account_id: brokerAccountId
    })
  },
  importEastmoneyStatement(file: File | Blob, brokerAccountId: number | string | null = null) {
    return uploadFile('/import/eastmoney-statement', file, {
      broker_account_id: brokerAccountId
    })
  },
  exportExcel() {
    return apiClient.get('/export/excel', { responseType: 'blob' })
  }
}
