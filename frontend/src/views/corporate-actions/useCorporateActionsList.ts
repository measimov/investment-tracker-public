/**
 * 公司行动记录列表（#284：由 RecordsTab 拆出，与交易页的 useTransactionsList 对称）：
 * 筛选、分页（usePagedList）、现金股息汇总卡片与删除。
 */

import { reactive, ref } from 'vue'
import api from '@/api'
import { makeConfirmedAction } from '@/composables/useConfirmAction'
import { usePagedList } from '@/composables/usePagedList'
import type { CorporateAction, SecuritySearchItem } from '@/types'
import { UNASSIGNED_ACCOUNT, type UnassignedAccount } from '@/utils/labels'

export interface ActionsSummary {
  total_count?: number
  cash_dividends?: {
    total_dividend?: number
    total_tax?: number
    net_dividend?: number
    missing_rate_currencies?: string[]
  } | null
  [key: string]: unknown
}

export function useCorporateActionsList() {
  const summary = ref<ActionsSummary | null>(null)
  const filters = reactive<{
    account: '' | UnassignedAccount | number
    symbol: string
    market: string
    action_type: string
    date_range: string[]
  }>({
    account: '',
    symbol: '',
    market: '',
    action_type: '',
    date_range: []
  })

  function buildQueryParams() {
    const params: Record<string, unknown> = {}
    const symbol = filters.symbol.trim()

    if (symbol) params.symbol = symbol
    if (filters.account === UNASSIGNED_ACCOUNT) {
      params.unassigned_account = true
    } else if (filters.account) {
      params.broker_account_id = filters.account
    }
    if (filters.market) params.market = filters.market
    if (filters.action_type) params.action_type = filters.action_type
    if (filters.date_range?.length === 2) {
      params.start_date = filters.date_range[0]
      params.end_date = filters.date_range[1]
    }
    return params
  }

  // 分页、页码回退与旧请求守卫在 usePagedList（与交易页共用）；汇总卡片随列表按同一筛选重取
  const list = usePagedList<CorporateAction>({
    failureMessage: '加载公司行动记录失败',
    fetchPage: async ({ skip, limit }) => {
      const baseParams = buildQueryParams()
      const [listResponse, countResponse] = await Promise.all([
        api.getCorporateActions({ ...baseParams, skip, limit }),
        api.getCorporateActionsCount(baseParams)
      ])
      return { items: listResponse.data, total: countResponse.data.total || 0 }
    }
  })

  async function loadSummary(params: Record<string, unknown> = {}) {
    try {
      const response = await api.getCorporateActionsSummary(params)
      summary.value = response.data
    } catch (error) {
      summary.value = null
      console.error('加载统计失败', error)
    }
  }

  async function loadActions() {
    await list.load()
    loadSummary(buildQueryParams())
  }

  function handleSearch() {
    list.pagination.page = 1
    loadActions()
  }

  function handlePageSizeChange() {
    list.pagination.page = 1
    loadActions()
  }

  // 筛选框选中候选：代码与市场一起定，否则 00700 配 A股 筛选查空
  function onFilterSymbolSelected(item: SecuritySearchItem) {
    filters.market = item.market
    handleSearch()
  }

  function resetFilters() {
    filters.account = ''
    filters.symbol = ''
    filters.market = ''
    filters.action_type = ''
    filters.date_range = []
    handleSearch()
  }

  const handleDelete = makeConfirmedAction<CorporateAction>({
    title: '删除公司行动',
    message: '确定要删除这条公司行动记录吗？',
    confirmText: '删除',
    request: (row) => api.deleteCorporateAction(row.id),
    successMessage: '删除成功',
    failureMessage: '删除失败',
    reload: () => loadActions()
  })

  return reactive({
    loading: list.loading,
    actions: list.items,
    pagination: list.pagination,
    summary,
    filters,
    loadActions,
    handleSearch,
    handlePageSizeChange,
    onFilterSymbolSelected,
    resetFilters,
    handleDelete
  })
}
