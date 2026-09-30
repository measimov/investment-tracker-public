/**
 * 交易列表 feature（issue #140：交易页的核心数据面）。列表、过滤、分页、
 * 删除（含转仓配对腿提示）全在这里；TransactionsTable 只做展示与交互绑定。
 *
 * 返回 reactive 包：作为单个 prop 传给子组件后模板可直接读写
 * （filters/pagination 由壳层过滤表单与表格分页双向绑定）。
 */

import { UNASSIGNED_ACCOUNT, type UnassignedAccount } from '@/utils/labels'
import { makeConfirmedAction } from '@/composables/useConfirmAction'
import { reactive } from 'vue'
import { usePagedList } from '@/composables/usePagedList'
import { useTransactionsStore, type Transaction } from '@/stores/transactions'
import { isTransfer } from './shared'

export function useTransactionsList() {
  const transactionsStore = useTransactionsStore()

  const filters = reactive<{
    symbol: string
    market: string
    transaction_type: string
    account: '' | UnassignedAccount | number
  }>({
    symbol: '',
    market: '',
    transaction_type: '',
    account: ''
  })

  function buildQueryParams() {
    const params: Record<string, unknown> = {}
    const symbol = filters.symbol.trim()
    if (symbol) params.symbol = symbol
    if (filters.market) params.market = filters.market
    if (filters.transaction_type) params.transaction_type = filters.transaction_type
    if (filters.account === UNASSIGNED_ACCOUNT) {
      params.unassigned_account = true
    } else if (filters.account !== '' && filters.account != null) {
      params.broker_account_id = filters.account
    }
    return params
  }

  // 分页、页码回退与旧请求守卫在 usePagedList（与公司行动页共用）
  const list = usePagedList<Transaction>({
    failureMessage: '加载交易记录失败',
    fetchPage: async ({ skip, limit }, { force }) => {
      const params = buildQueryParams()
      const [items, total] = await Promise.all([
        transactionsStore.fetchTransactions({ ...params, skip, limit }, { force }),
        transactionsStore.fetchTransactionsCount(params, { force })
      ])
      return { items, total }
    }
  })
  const loadTransactions = list.load
  const handleSearch = list.search
  const handlePageSizeChange = list.changePageSize

  function resetFilters() {
    filters.symbol = ''
    filters.market = ''
    filters.transaction_type = ''
    filters.account = ''
    handleSearch()
  }

  const handleDelete = makeConfirmedAction<Transaction>({
    title: '删除交易',
    message: (row) =>
      isTransfer(row)
        ? '这是转仓交易：删除将同时删除配对的另一腿，并重算相关持仓。确定继续吗？'
        : '确定要删除这条交易记录吗？',
    confirmText: '删除',
    request: (row) => transactionsStore.deleteTransaction(row.id),
    successMessage: '删除成功',
    failureMessage: '删除失败',
    reload: () => loadTransactions()
  })

  return reactive({
    loading: list.loading,
    transactions: list.items,
    pagination: list.pagination,
    filters,
    loadTransactions,
    handleSearch,
    handlePageSizeChange,
    resetFilters,
    handleDelete
  })
}

export type TransactionsListFeature = ReturnType<typeof useTransactionsList>
