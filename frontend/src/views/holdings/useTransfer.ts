/**
 * 账户间转仓 feature（issue #140）：dialog 状态、目标账户候选与提交。
 * 账户目录与标签、以及提交成功后的持仓刷新由壳层注入（数据归持仓
 * feature 所有，这里只消费）。
 */

import { computed, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import { useTransactionsStore } from '@/stores/transactions'
import type { Holding } from '@/stores/holdings'
import { todayLocalISODate, toNumber } from '@/utils/helpers'
import { getApiErrorMessage } from '@/utils/apiErrors'
import type { BrokerAccount } from '@/types'
import type { TransferForm } from './types'

export function useTransfer({
  accounts,
  accountLabel,
  reload
}: {
  accounts: () => BrokerAccount[]
  accountLabel: (accountId: number | null | undefined) => string
  reload: () => Promise<void>
}) {
  const transactionsStore = useTransactionsStore()

  const state = reactive({
    visible: false,
    submitting: false,
    form: null as TransferForm | null
  })

  const targetAccounts = computed(() => {
    const form = state.form
    if (!form) return accounts()
    return accounts().filter((account) => account.id !== form.from_broker_account_id)
  })

  function openDialog(row: Holding) {
    state.form = {
      symbol: row.symbol,
      market: row.market,
      from_broker_account_id: row.broker_account_id,
      // undefined = 尚未选择；null 仅在用户点选"未指定账户"(sentinel) 后映射产生
      to_broker_account_id: undefined,
      quantity: toNumber(row.quantity),
      max_quantity: toNumber(row.quantity),
      transfer_date: todayLocalISODate(),
      notes: ''
    }
    state.visible = true
  }

  async function submit() {
    const form = state.form
    if (!form) return
    if (form.to_broker_account_id === undefined) {
      ElMessage.warning('请选择转入账户')
      return
    }
    const targetAccountId =
      form.to_broker_account_id === 'unassigned' ? null : form.to_broker_account_id
    if (targetAccountId === form.from_broker_account_id) {
      ElMessage.warning('请选择不同的转入账户')
      return
    }
    if (!form.quantity || form.quantity <= 0) {
      ElMessage.warning('请输入有效的转仓数量')
      return
    }
    state.submitting = true
    try {
      await transactionsStore.createTransfer({
        symbol: form.symbol,
        market: form.market,
        quantity: form.quantity,
        from_broker_account_id: form.from_broker_account_id,
        to_broker_account_id: targetAccountId,
        transfer_date: form.transfer_date,
        notes: form.notes || null
      })
      ElMessage.success('转仓成功')
      state.visible = false
      await reload()
    } catch (error) {
      ElMessage.error(getApiErrorMessage(error, '转仓失败'))
    } finally {
      state.submitting = false
    }
  }

  return reactive({
    state,
    targetAccounts,
    accountLabel,
    openDialog,
    submit
  })
}

export type TransferFeature = ReturnType<typeof useTransfer>
