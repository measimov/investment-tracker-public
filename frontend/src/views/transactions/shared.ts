/**
 * 交易页拆分（issue #140）共用小件：转仓判定。账户显示名统一在 utils/labels（#284）。
 */

import type { Transaction } from '@/stores/transactions'

export function isTransfer(row: Transaction) {
  return row.transaction_type === 'TRANSFER_OUT' || row.transaction_type === 'TRANSFER_IN'
}
