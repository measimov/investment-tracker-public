<template>
  <div class="transactions-page" :aria-busy="list.loading">
    <header class="transactions-heading">
      <div>
        <h1 class="page-title">交易记录</h1>
        <p class="page-description">按交易日期倒序，查看每笔成交与账户来源。</p>
      </div>
      <div class="header-actions">
        <NButton @click="importDialog?.open()"
          ><template #icon><Upload /></template>导入</NButton
        >
        <NButton @click="handleExport"
          ><template #icon><Download /></template>导出全部</NButton
        >
        <NButton type="primary" @click="formDialog?.openAdd()"
          ><template #icon><Plus /></template>新增交易</NButton
        >
      </div>
    </header>

    <section class="transactions-detail" aria-labelledby="transaction-detail-title">
      <div
        ref="listTop"
        class="detail-heading transaction-list-top"
        role="region"
        aria-label="交易列表"
        tabindex="-1"
        data-testid="transaction-list-top"
      >
        <h2 id="transaction-detail-title">
          交易明细
          <span class="result-count" data-testid="transaction-result-count">{{ resultCount }}</span>
        </h2>
        <span v-if="list.loading" role="status" class="list-state"
          >正在加载{{ list.hasLoaded ? '，暂保留上次查询结果' : '' }}</span
        >
        <div class="detail-actions">
          <nav
            v-if="pageCount > 1"
            class="top-pagination"
            aria-label="交易列表翻页"
            data-testid="transaction-top-pagination"
          >
            <NButton
              text
              :disabled="paginationDisabled || list.pagination.page <= 1"
              @click="changePage(list.pagination.page - 1)"
            >
              上一页
            </NButton>
            <span class="pagination-info" aria-live="polite">
              {{
                list.loadError
                  ? '页码尚未确认'
                  : list.loading || !list.hasLoaded
                    ? '正在加载页码'
                    : isMobileView
                      ? `${list.pagination.page} / ${pageCount}`
                      : `第 ${list.pagination.page} / ${pageCount} 页`
              }}
            </span>
            <NButton
              text
              :disabled="paginationDisabled || list.pagination.page >= pageCount"
              @click="changePage(list.pagination.page + 1)"
            >
              下一页
            </NButton>
          </nav>
          <NButton
            class="filter-toggle"
            v-if="isMobileView"
            :aria-expanded="filtersOpen"
            aria-controls="transaction-filters"
            @click="filtersOpen = !filtersOpen"
          >
            <template #icon><Funnel :size="16" aria-hidden="true" /></template>
            {{ filtersOpen ? '收起筛选' : '筛选'
            }}{{ activeFilterCount ? `（${activeFilterCount}）` : '' }}
          </NButton>
        </div>
      </div>
      <div
        id="transaction-filters"
        v-show="!isMobileView || filtersOpen"
        class="filter-form ledger-filter-form"
      >
        <NFormItem label="代码" :show-feedback="false">
          <SecuritySelect
            v-model="list.filters.symbol"
            id="transaction-symbol-filter"
            :resolve="false"
            placeholder="标的代码"
            class="filter-symbol"
            @select="onFilterSymbolSelected"
            @clear="handleSearch"
            @keyup.enter="handleSearch"
          />
        </NFormItem>
        <NFormItem label="市场" :show-feedback="false">
          <select v-model="list.filters.market" aria-label="筛选市场" @change="handleSearch">
            <option value="">全部市场</option>
            <option v-for="market in MARKETS" :key="market" :value="market">{{ market }}</option>
          </select>
        </NFormItem>
        <NFormItem label="类型" :show-feedback="false">
          <select
            v-model="list.filters.transaction_type"
            aria-label="筛选交易类型"
            @change="handleSearch"
          >
            <option value="">全部类型</option>
            <option
              v-for="option in transactionTypeOptions"
              :key="option.value"
              :value="option.value"
            >
              {{ option.label }}
            </option>
          </select>
        </NFormItem>
        <NFormItem label="账户" :show-feedback="false">
          <select v-model="list.filters.account" aria-label="筛选账户" @change="handleSearch">
            <option value="">全部账户</option>
            <option v-for="option in accountOptions" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
        </NFormItem>
        <NFormItem label="日期" :show-feedback="false" class="date-filter-item">
          <NButton
            block
            class="date-filter-trigger"
            aria-label="选择交易日期范围"
            :aria-expanded="dateDialogVisible"
            @click="openDateRange"
          >
            <template #icon><Calendar aria-hidden="true" /></template>
            {{ dateRangeLabel }}
          </NButton>
        </NFormItem>
        <div class="filter-actions">
          <NButton type="primary" @click="handleSearch">查询</NButton
          ><NButton @click="resetFilters">重置</NButton>
        </div>
      </div>

      <NAlert
        v-if="list.loadError"
        type="error"
        :show-icon="false"
        class="list-error"
        data-testid="transaction-load-error"
      >
        {{
          list.hasLoaded
            ? '交易记录加载失败，保留上次成功查询的数据。当前筛选结果尚未确认。'
            : '交易记录加载失败，数量与记录暂不可用。'
        }}
        <NButton @click="handleSearch">重试</NButton>
      </NAlert>

      <TransactionsTable
        :list="list"
        :broker-accounts="brokerAccounts"
        :broker-accounts-status="accountsState.status"
        @edit="(row) => formDialog?.openEdit(row)"
      />
      <div class="pagination-bar">
        <span class="pagination-info">{{
          list.loading || list.loadError || !list.hasLoaded
            ? '共 — 条'
            : `共 ${list.pagination.total} 条`
        }}</span>
        <el-pagination
          :current-page="list.pagination.page"
          :page-size="list.pagination.pageSize"
          :page-sizes="[25, 50, 100, 200]"
          :total="list.pagination.total"
          :pager-count="isMobileView ? 5 : 7"
          layout="sizes, prev, pager, next, jumper"
          :disabled="paginationDisabled"
          @update:page-size="changePageSize"
          @update:current-page="changePage"
        />
      </div>
    </section>
    <DateRangeDialog
      title="交易日期范围"
      v-if="dateDialogOpened"
      v-model:show="dateDialogVisible"
      :date-range="list.filters.dateRange"
      @apply="applyDateRange"
    />
    <TransactionFormDialog
      ref="formDialog"
      :broker-accounts="brokerAccounts"
      :broker-accounts-loading="brokerAccountsLoading"
      @saved="handleSearch"
    />
    <ImportDialog
      ref="importDialog"
      :broker-accounts="brokerAccounts"
      :broker-accounts-loading="brokerAccountsLoading"
      @imported="handleImported"
    />
  </div>
</template>

<script setup lang="ts">
import { showApiError } from '@/utils/showApiError'
import { Upload, Download, Plus, Calendar, Funnel } from '@lucide/vue'
import { computed, ref, onMounted, nextTick, defineAsyncComponent } from 'vue'
import { useAliveGuard } from '@/composables/useAliveGuard'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { useBrokerAccounts } from '@/composables/useBrokerAccounts'
import { ElMessage } from 'element-plus'
import { NAlert, NButton, NFormItem } from 'naive-ui'
import api from '../api'
import SecuritySelect from '../components/SecuritySelect.vue'
import { useTransactionsStore } from '../stores/transactions'
import type { SecuritySearchItem } from '../types'
import { downloadFile, todayLocalISODate, formatDate } from '../utils/helpers'
import { MARKETS } from '../utils/securities'
import {
  accountOptionLabel,
  optionsOf,
  TRANSACTION_TYPE_LABELS,
  UNASSIGNED_ACCOUNT,
  UNASSIGNED_ACCOUNT_LABEL
} from '../utils/labels'

const transactionTypeOptions = optionsOf(TRANSACTION_TYPE_LABELS)
import TransactionsTable from './transactions/TransactionsTable.vue'
import TransactionFormDialog from './transactions/TransactionFormDialog.vue'
import ImportDialog from './transactions/ImportDialog.vue'
import { useTransactionsList } from './transactions/useTransactionsList'

// 壳层职责（issue #140）：页头（导入/导出/新增）、过滤表单、分页与三个
// 子件的编排。列表数据面在 useTransactionsList；新增编辑表单与导入向导
// 各自成自足 dialog（expose open*，保存/入账后回调壳层刷新）。
const transactionsStore = useTransactionsStore()
const list = useTransactionsList()
const listTop = ref<HTMLDivElement | null>(null)
const pageCount = computed(() =>
  Math.max(1, Math.ceil(list.pagination.total / list.pagination.pageSize))
)
const paginationDisabled = computed(() => list.loading || list.loadError || !list.hasLoaded)
let paginationRequest = 0
const { isUnmounted } = useAliveGuard()

async function loadPagination(load: () => Promise<void>) {
  const request = ++paginationRequest
  const previousRows = list.transactions
  await load()
  if (
    isUnmounted() ||
    request !== paginationRequest ||
    list.loading ||
    list.loadError ||
    list.transactions === previousRows
  )
    return
  await nextTick()
  if (isUnmounted() || request !== paginationRequest) return
  listTop.value?.focus({ preventScroll: true })
  listTop.value?.scrollIntoView({ block: 'start' })
}
function changePage(page: number) {
  if (paginationDisabled.value || page === list.pagination.page) return
  list.pagination.page = page
  return loadPagination(() => list.loadTransactions())
}
function changePageSize(size: number) {
  if (paginationDisabled.value || size === list.pagination.pageSize) return
  list.pagination.pageSize = size
  return loadPagination(() => list.handlePageSizeChange())
}
function handleSearch() {
  ++paginationRequest
  return list.handleSearch()
}
function resetFilters() {
  ++paginationRequest
  list.resetFilters()
}
const isMobileView = useMediaQuery('(max-width: 640px)')
const filtersOpen = ref(false)
const accountOptions = computed(() => [
  { label: UNASSIGNED_ACCOUNT_LABEL, value: UNASSIGNED_ACCOUNT },
  ...brokerAccounts.value.map((account) => ({
    label: accountOptionLabel(account),
    value: account.id
  }))
])
const resultCount = computed(() =>
  list.loading || list.loadError || !list.hasLoaded ? '—' : String(list.pagination.total)
)
const DateRangeDialog = defineAsyncComponent(() => import('@/components/DateRangeDialog.vue'))
const dateDialogOpened = ref(false)
const dateDialogVisible = ref(false)
const dateRangeLabel = computed(() =>
  list.filters.dateRange
    ? `${formatDate(list.filters.dateRange[0])} 至 ${formatDate(list.filters.dateRange[1])}`
    : '选择日期范围'
)
function openDateRange() {
  // 首次打开才加载日期面板；之后保持组件挂载，由 NModal 完成关闭与焦点返回。
  dateDialogOpened.value = true
  dateDialogVisible.value = true
}
function applyDateRange(range: [string, string] | null) {
  list.filters.dateRange = range
  dateDialogVisible.value = false
  handleSearch()
}
const activeFilterCount = computed(
  () =>
    [
      list.filters.symbol,
      list.filters.market,
      list.filters.transaction_type,
      list.filters.account,
      list.filters.dateRange
    ].filter((value) => value !== '' && value !== null && value !== undefined).length
)

// 筛选框选中候选：代码与市场一起定，否则 00700 配 A股 筛选查空
function onFilterSymbolSelected(item: SecuritySearchItem) {
  list.filters.market = item.market
  handleSearch()
}

const formDialog = ref<InstanceType<typeof TransactionFormDialog> | null>(null)
const importDialog = ref<InstanceType<typeof ImportDialog> | null>(null)

const { state: accountsState, load: loadBrokerAccounts } = useBrokerAccounts()
const brokerAccounts = computed(() => accountsState.accounts)
const brokerAccountsLoading = computed(() => accountsState.status === 'loading')

async function handleExport() {
  try {
    const response = await api.exportExcel()
    downloadFile(response.data, `transactions_${todayLocalISODate()}.xlsx`)
    ElMessage.success('导出成功')
  } catch (error) {
    showApiError(error, '导出失败')
  }
}

// 导入入账后：持仓/统计等派生数据一并失效，再按导入结果的口径刷新列表
// （部分入账时 force 强制重取，与拆分前一致）
async function handleImported(options: { force: boolean }) {
  ++paginationRequest
  transactionsStore.invalidateDependentData()
  await list.loadTransactions(options)
}

onMounted(() => {
  list.loadTransactions()
  loadBrokerAccounts()
})
</script>

<style scoped>
.transactions-page {
  width: 100%;
}
.transactions-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
  margin-bottom: 24px;
}
.header-actions,
.filter-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.header-actions :deep(svg) {
  width: 16px;
  height: 16px;
}
.transactions-detail {
  container-type: inline-size;
  border-top: 1px solid var(--app-border);
  padding-top: 20px;
}
.detail-heading {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 16px;
}
.detail-heading h2 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
}
.result-count {
  margin-left: 8px;
  color: var(--app-text-muted);
  font-size: 13px;
  font-weight: 400;
}
.list-state {
  color: var(--app-text-muted);
  font-size: 13px;
}
.list-error {
  margin-bottom: 16px;
}
.date-filter-trigger {
  justify-content: flex-start;
  padding: 0 10px;
  border-radius: 3px;
  background: var(--app-surface);
}
.date-filter-trigger :deep(.n-button__border) {
  border-color: var(--app-border);
}
.pagination-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 16px;
  padding-top: 20px;
}
.transaction-list-top {
  scroll-margin-top: calc(var(--app-header-height, 64px) + 16px);
}
.transaction-list-top:focus-visible {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: 4px;
}
.detail-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-left: auto;
}
.top-pagination {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 8px;
}
.pagination-info {
  color: var(--app-text-muted);
  font-size: 13px;
  white-space: nowrap;
}
@media (min-width: 1025px) {
  .transactions-heading {
    margin-bottom: var(--app-space-md);
  }
  .transactions-detail,
  .pagination-bar {
    padding-top: var(--app-space-sm);
  }
  .detail-heading {
    margin-bottom: var(--app-space-sm);
  }
}
@media (max-width: 800px) {
  .transactions-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 16px;
  }
}
@media (max-width: 640px) {
  .transactions-heading {
    margin-bottom: 20px;
    gap: 12px;
  }
  .transactions-heading p {
    margin-top: 4px;
  }
  .header-actions :deep(.n-button),
  .filter-actions :deep(.n-button),
  .list-error :deep(.n-button) {
    min-height: 44px;
  }
  .top-pagination :deep(.n-button),
  .filter-toggle {
    min-height: 44px;
  }
  .detail-heading {
    gap: 8px;
  }
  .top-pagination {
    gap: 6px;
  }
  .detail-actions {
    gap: 12px;
  }
  .filter-toggle {
    padding-inline: 10px;
    flex-shrink: 0;
  }
  .detail-actions :deep(.n-button) {
    font-size: 13px;
  }
  .header-actions {
    gap: 8px;
  }
  .transactions-detail {
    container-type: inline-size;
    padding-top: 16px;
  }
  .pagination-bar {
    align-items: flex-start;
    flex-direction: column;
  }
  .pagination-bar :deep(.el-pagination) {
    flex-wrap: wrap;
    gap: 8px;
  }
  .pagination-bar :deep(.el-pager li),
  .pagination-bar :deep(.btn-prev),
  .pagination-bar :deep(.btn-next),
  .pagination-bar :deep(.el-input__wrapper),
  .pagination-bar :deep(.el-select__wrapper) {
    min-width: 32px;
    min-height: 44px;
  }
}
</style>
