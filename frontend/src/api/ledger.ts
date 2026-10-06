/** 账本：交易、转仓、券商账户、导入批次、现金事件、特例规则、对账快照、持仓、公司行动与分红建议。 */
import { apiClient, type QueryParams } from './client'
import type {
  BrokerAccount,
  BrokerAccountCreate,
  BrokerAccountUpdate,
  CashEvent,
  CashEventCreate,
  CashEventUpdate,
  DividendTaxAllocationsUpdate,
  CorporateAction,
  CorporateActionCreate,
  CorporateActionUpdate,
  DividendSuggestion,
  HoldingResponse,
  ImportBatch,
  OpeningPositionCostUpdate,
  ReconciliationSnapshot,
  ReconciliationSnapshotCreate,
  ReconciliationSnapshotUpdate,
  SecurityEvent,
  SecurityRule,
  SecurityRuleCreate,
  SuggestionAccept,
  SuggestionReceiptsUpdate,
  Transaction,
  TransactionCreate,
  TransactionUpdate,
  TransferCreate
} from '@/types'

export const ledgerApi = {
  // Transactions
  getTransactions(params?: QueryParams) {
    return apiClient.get<Transaction[]>('/transactions', { params })
  },
  getTransactionsCount(params?: QueryParams) {
    return apiClient.get('/transactions/count', { params })
  },
  createTransaction(data: TransactionCreate) {
    return apiClient.post<Transaction>('/transactions', data)
  },
  updateTransaction(id: number | string, data: TransactionUpdate) {
    return apiClient.put<Transaction>(`/transactions/${id}`, data)
  },
  deleteTransaction(id: number | string) {
    return apiClient.delete<void>(`/transactions/${id}`)
  },
  // 账户间转仓：创建 TRANSFER_OUT/TRANSFER_IN 互指交易对，成本基础跟随迁移
  createTransfer(data: TransferCreate) {
    return apiClient.post<Transaction[]>('/transactions/transfer', data)
  },

  // Broker accounts
  getBrokerAccounts(params?: QueryParams) {
    return apiClient.get<BrokerAccount[]>('/broker-accounts', { params })
  },
  createBrokerAccount(data: BrokerAccountCreate) {
    return apiClient.post<BrokerAccount>('/broker-accounts', data)
  },
  updateBrokerAccount(id: number | string, data: BrokerAccountUpdate) {
    return apiClient.put<BrokerAccount>(`/broker-accounts/${id}`, data)
  },
  deleteBrokerAccount(id: number | string) {
    return apiClient.delete<void>(`/broker-accounts/${id}`)
  },

  // Import traceability
  getImportBatches(params?: QueryParams) {
    return apiClient.get<ImportBatch[]>('/import-batches', { params })
  },
  getImportBatch(id: number | string) {
    return apiClient.get<ImportBatch>(`/import-batches/${id}`)
  },

  // Account cash events
  getCashEvents(params?: QueryParams) {
    return apiClient.get<CashEvent[]>('/cash-events', { params })
  },
  createCashEvent(data: CashEventCreate) {
    return apiClient.post<CashEvent>('/cash-events', data)
  },
  updateCashEvent(id: number | string, data: CashEventUpdate) {
    return apiClient.put<CashEvent>(`/cash-events/${id}`, data)
  },
  deleteCashEvent(id: number | string) {
    return apiClient.delete<void>(`/cash-events/${id}`)
  },
  updateDividendTaxAllocations(id: number, data: DividendTaxAllocationsUpdate) {
    return apiClient.put<CashEvent>(`/cash-events/${id}/dividend-allocations`, data)
  },

  // 账本特例规则（issue #82）：排除/现金管理/转板映射/名称覆盖/行情缺口豁免/招商现金业务
  getSecurityRules(params?: QueryParams) {
    return apiClient.get<SecurityRule[]>('/security-rules', { params })
  },
  createSecurityRule(data: SecurityRuleCreate) {
    return apiClient.post<SecurityRule>('/security-rules', data)
  },
  deleteSecurityRule(id: number | string) {
    return apiClient.delete<void>(`/security-rules/${id}`)
  },

  // Month-end reconciliation snapshots
  getReconciliationSnapshots(params?: QueryParams) {
    return apiClient.get<ReconciliationSnapshot[]>('/reconciliation-snapshots', { params })
  },
  createReconciliationSnapshot(data: ReconciliationSnapshotCreate) {
    return apiClient.post<ReconciliationSnapshot>('/reconciliation-snapshots', data)
  },
  updateReconciliationSnapshot(id: number | string, data: ReconciliationSnapshotUpdate) {
    return apiClient.put<ReconciliationSnapshot>(`/reconciliation-snapshots/${id}`, data)
  },
  deleteReconciliationSnapshot(id: number | string) {
    return apiClient.delete<void>(`/reconciliation-snapshots/${id}`)
  },
  // 手动触发快照自动比对（账本变化后刷新红绿状态与 diff 明细）
  compareReconciliationSnapshot(id: number | string) {
    return apiClient.post<ReconciliationSnapshot>(`/reconciliation-snapshots/${id}/compare`)
  },

  // Holdings
  getHoldings(params?: QueryParams) {
    return apiClient.get<HoldingResponse[]>('/holdings', { params })
  },

  // Corporate Actions
  getCorporateActions(params?: QueryParams) {
    return apiClient.get<CorporateAction[]>('/corporate-actions', { params })
  },
  getCorporateActionsCount(params?: QueryParams) {
    return apiClient.get('/corporate-actions/count', { params })
  },
  createCorporateAction(data: CorporateActionCreate) {
    return apiClient.post<CorporateAction>('/corporate-actions', data)
  },
  updateCorporateAction(id: number | string, data: CorporateActionUpdate) {
    return apiClient.put<CorporateAction>(`/corporate-actions/${id}`, data)
  },
  // 期初建仓补录成本（导入建的行动也允许，只开放两个成本字段与备注，#174）
  updateOpeningPositionCost(id: number | string, data: OpeningPositionCostUpdate) {
    return apiClient.patch<CorporateAction>(`/corporate-actions/${id}/cost-basis`, data)
  },
  deleteCorporateAction(id: number | string) {
    return apiClient.delete<void>(`/corporate-actions/${id}`)
  },
  getCorporateActionsSummary(params?: QueryParams) {
    return apiClient.get('/corporate-actions/statistics/summary', { params })
  },

  // 分红公告建议（Tushare 同步；仅 A/B 股）与标的事件
  startDividendSyncJob() {
    return apiClient.post('/corporate-actions/dividend-sync-jobs')
  },
  getDividendSyncJob(id: string) {
    return apiClient.get(`/corporate-actions/dividend-sync-jobs/${id}`)
  },
  listDividendSuggestions(params?: QueryParams) {
    return apiClient.get<DividendSuggestion[]>('/corporate-actions/suggestions', { params })
  },
  getDividendReceiptCandidates(id: number) {
    return apiClient.get<CorporateAction[]>(`/corporate-actions/suggestions/${id}/receipts`)
  },
  updateDividendReceipts(id: number, data: SuggestionReceiptsUpdate) {
    return apiClient.put<DividendSuggestion>(`/corporate-actions/suggestions/${id}/receipts`, data)
  },
  countDividendSuggestions() {
    return apiClient.get('/corporate-actions/suggestions/count')
  },
  acceptDividendSuggestion(id: number | string, data: SuggestionAccept = {}) {
    return apiClient.post<CorporateAction>(`/corporate-actions/suggestions/${id}/accept`, data)
  },
  ignoreDividendSuggestion(id: number | string) {
    return apiClient.post<DividendSuggestion>(`/corporate-actions/suggestions/${id}/ignore`)
  },
  restoreDividendSuggestion(id: number | string) {
    return apiClient.post<DividendSuggestion>(`/corporate-actions/suggestions/${id}/restore`)
  },
  getSecurityEvents(params?: QueryParams) {
    return apiClient.get<SecurityEvent[]>('/corporate-actions/security-events', { params })
  }
}
