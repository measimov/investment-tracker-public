import { describe, expect, it } from 'vitest'
import { reconciledAccountSummary } from './shared'

describe('reconciledAccountSummary（#286：按账户计）', () => {
  const accounts = [{ id: 1 }, { id: 2 }, { id: 3 }, { id: 4, is_active: false }]

  it('每个账户只看最近快照日，同日任一范围不一致即不算通过', () => {
    const snapshots = [
      { broker_account_id: 1, snapshot_date: '2026-08-31', status: 'MISMATCHED' },
      { broker_account_id: 1, snapshot_date: '2026-09-30', status: 'MATCHED' },
      { broker_account_id: 2, snapshot_date: '2026-09-30', status: 'MATCHED' },
      { broker_account_id: 2, snapshot_date: '2026-09-30', status: 'MISMATCHED' },
      { broker_account_id: 4, snapshot_date: '2026-09-30', status: 'MATCHED' }
    ]
    expect(reconciledAccountSummary(accounts, snapshots)).toEqual({ matched: 1, total: 3 })
  })

  it('没有快照的账户计入分母', () => {
    expect(reconciledAccountSummary(accounts, [])).toEqual({ matched: 0, total: 3 })
  })
})
