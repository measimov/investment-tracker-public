/**
 * 交易列表 feature（issue #140：交易页的核心数据面）。列表、过滤、分页、
 * 删除（含转仓配对腿提示）全在这里；TransactionsTable 只做展示与交互绑定。
 *
 * 返回 reactive 包：作为单个 prop 传给子组件后模板可直接读写
 * （filters/pagination 由壳层过滤表单与表格分页双向绑定）。
 */

import { reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useTransactionsStore, type Transaction } from '@/stores/transactions'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { isTransfer } from './shared'

export function useTransactionsList() {
  const transactionsStore = useTransactionsStore()

  const loading = ref(false)
  const transactions = ref<Transaction[]>([])
  const pagination = reactive({
    page: 1,
    pageSize: 50,
    total: 0
  })
  const filters = reactive<{
    symbol: string
    market: string
    transaction_type: string
    account: '' | 'UNASSIGNED' | number
  }>({
    symbol: '',
    market: '',
    transaction_type: '',
    account: ''
  })

  function buildQueryParams() {
    const params: Record<string, unknown> = {}
    if (filters.symbol) params.symbol = filters.symbol
    if (filters.market) params.market = filters.market
    if (filters.transaction_type) params.transaction_type = filters.transaction_type
    if (filters.account === 'UNASSIGNED') {
      params.unassigned_account = true
    } else if (filters.account !== '' && filters.account != null) {
      params.broker_account_id = filters.account
    }
    return params
  }

  async function loadTransactions(options: { force?: boolean } = {}) {
    loading.value = true
    try {
      const params = buildQueryParams()
      params.skip = (pagination.page - 1) * pagination.pageSize
      params.limit = pagination.pageSize

      const [transactionsData, total] = await Promise.all([
        transactionsStore.fetchTransactions(params, { force: options?.force === true }),
        transactionsStore.fetchTransactionsCount(buildQueryParams(), {
          force: options?.force === true
        })
      ])

      transactions.value = transactionsData
      pagination.total = total

      const maxPage = Math.max(1, Math.ceil(pagination.total / pagination.pageSize))
      if (pagination.page > maxPage) {
        pagination.page = maxPage
        await loadTransactions()
      }
    } catch (error) {
      ElMessage.error(getApiErrorMessage(error, '加载交易记录失败'))
    } finally {
      loading.value = false
    }
  }

  function handleSearch() {
    pagination.page = 1
    loadTransactions({ force: true })
  }

  function handlePageSizeChange() {
    pagination.page = 1
    loadTransactions()
  }

  function resetFilters() {
    filters.symbol = ''
    filters.market = ''
    filters.transaction_type = ''
    filters.account = ''
    handleSearch()
  }

  function handleDelete(row: Transaction) {
    const message = isTransfer(row)
      ? '这是转仓交易：删除将同时删除配对的另一腿，并重算相关持仓。确定继续吗？'
      : '确定要删除这条交易记录吗？'
    ElMessageBox.confirm(message, '提示', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning'
    }).then(async () => {
      try {
        await transactionsStore.deleteTransaction(row.id)
        ElMessage.success('删除成功')
        loadTransactions()
      } catch (error) {
        ElMessage.error(getApiErrorMessage(error, '删除失败'))
      }
    })
  }

  return reactive({
    loading,
    transactions,
    pagination,
    filters,
    loadTransactions,
    handleSearch,
    handlePageSizeChange,
    resetFilters,
    handleDelete
  })
}

export type TransactionsListFeature = ReturnType<typeof useTransactionsList>
