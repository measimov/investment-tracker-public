<script setup lang="ts">
/**
 * 公司行动记录 tab：筛选 + 表格/卡片 + 分页（列表状态在 useCorporateActionsList），新增/编辑与
 * 补录成本各是一个对话框组件（#284 拆分，此前 1158 行）。
 */
import { computed, defineAsyncComponent, h, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  NAlert,
  NButton,
  NDataTable,
  NEmpty,
  NFormItem,
  NSpin,
  NTag,
  type DataTableColumns
} from 'naive-ui'
import FinancialStatistic from '@/components/FinancialStatistic.vue'
import { Plus } from '@lucide/vue'
import SecuritySelect from '@/components/SecuritySelect.vue'
import { useMediaQuery } from '@/composables/useMediaQuery'
import type { BrokerAccount, CorporateAction } from '@/types'
import { formatCurrency, formatDate, formatNumber, formatQuantity } from '@/utils/helpers'
import {
  type AccountListStatus,
  accountLabel,
  accountOptionLabel,
  ACTION_TYPE_LABELS,
  actionTypeLabel,
  actionTypeTag,
  optionsOf,
  UNASSIGNED_ACCOUNT,
  UNASSIGNED_ACCOUNT_LABEL
} from '@/utils/labels'
import { holdingsLink, MARKETS } from '@/utils/securities'
import CorporateActionFormDialog from './CorporateActionFormDialog.vue'
import OpeningCostDialog from './OpeningCostDialog.vue'
import { cashDividendAmounts, openingPositionCostKnown } from './shared'
import { useCorporateActionsList } from './useCorporateActionsList'

type CorporateActionRow = CorporateAction

const props = defineProps<{
  brokerAccounts: BrokerAccount[]
  brokerAccountsStatus: AccountListStatus
}>()

const isMobileView = useMediaQuery('(max-width: 640px)')
const actionTypeOptions = optionsOf(ACTION_TYPE_LABELS)
const list = useCorporateActionsList()
const formDialog = ref<InstanceType<typeof CorporateActionFormDialog> | null>(null)
const costDialog = ref<InstanceType<typeof OpeningCostDialog> | null>(null)

function brokerAccountLabelById(accountId: number | null | undefined) {
  return accountLabel(props.brokerAccounts, accountId, { status: props.brokerAccountsStatus })
}

// 详情文案：桌面表格与移动卡片共用一份。
// 后端 CorporateActionCreate 允许若干字段二选一（送股=比例或绝对股数、
// 拆股=比例或拆后股数），所以这里只拼存在的字段——直接模板插值会把
// 合法的"只填绝对股数"记录显示成 `比例: null`，还会丢掉唯一有效的值。
// 字段顺序与 portfolio/semantics.py 的优先级一致：决定复算的比例在前。
// 金额一律带币种符号（港/美股息不能看起来像人民币）；数量走 formatQuantity
function actionDetail(row: CorporateActionRow): string {
  const currency = row.currency || 'CNY'
  const money = (value: unknown, precision = 2) =>
    formatCurrency(value as number | string, currency, precision)
  const qty = (value: unknown) => formatQuantity(value as number | string)
  const has = (value: unknown) => value !== null && value !== undefined && value !== ''
  const parts: string[] = []

  switch (row.action_type) {
    case 'CASH_DIVIDEND': {
      if (row.receipt_status === 'UNVERIFIED') parts.push('历史待核实，不计入实收')
      if (row.amount_basis === 'NET_ONLY') parts.push('税前额与税额待核实')
      if (has(row.dividend_per_share)) parts.push(`每股: ${money(row.dividend_per_share, 4)}`)
      if (has(row.total_dividend)) parts.push(`总额: ${money(row.total_dividend)}`)
      const { tax, net } = cashDividendAmounts(row)
      if (tax > 0) parts.push(`预扣税: ${money(tax)}`)
      if (has(row.net_dividend) || (has(row.total_dividend) && tax > 0))
        parts.push(`税后: ${money(net)}`)
      break
    }
    case 'STOCK_DIVIDEND':
    case 'BONUS_ISSUE':
      if (has(row.distribution_ratio)) parts.push(`比例: ${row.distribution_ratio}`)
      if (has(row.shares_received)) parts.push(`获得股数: ${qty(row.shares_received)}`)
      break
    case 'RIGHTS_ISSUE':
      if (has(row.subscription_price)) parts.push(`认购价: ${money(row.subscription_price, 4)}`)
      if (has(row.subscription_quantity)) parts.push(`数量: ${qty(row.subscription_quantity)}`)
      break
    case 'STOCK_SPLIT':
    case 'REVERSE_SPLIT':
      if (has(row.split_ratio)) parts.push(`拆分比例: ${row.split_ratio}`)
      if (has(row.new_shares)) parts.push(`拆后股数: ${qty(row.new_shares)}`)
      break
    case 'OPENING_POSITION':
      if (has(row.adjusted_quantity)) parts.push(`数量: ${qty(row.adjusted_quantity)}`)
      if (has(row.cost_basis_adjustment)) parts.push(`总成本: ${money(row.cost_basis_adjustment)}`)
      else if (has(row.adjusted_cost_per_share))
        parts.push(`单位成本: ${money(row.adjusted_cost_per_share, 4)}`)
      else parts.push('成本未知')
      break
  }

  return parts.length ? parts.join(' | ') : '—'
}

// 只读判定与后端同一判据：带导入批次，或被券商来源流水引用（后端 read_only 字段）
function isReadOnly(row: CorporateActionRow): boolean {
  return Boolean(row.read_only || row.import_batch_id)
}

// 跨 tab 刷新入口：分红建议"接受"入账后由壳层调用（新记录要出现在列表里）
defineExpose({ reload: list.loadActions })

const DateRangeDialog = defineAsyncComponent(() => import('@/components/DateRangeDialog.vue'))
const dateDialogOpened = ref(false)
const dateDialogVisible = ref(false)
const filtersOpen = ref(false)
const activeFilterCount = computed(
  () =>
    [
      list.filters.symbol.trim(),
      list.filters.market,
      list.filters.action_type,
      list.filters.account,
      list.filters.date_range.length === 2
    ].filter(Boolean).length
)
const resultCount = computed(() =>
  list.loading || list.loadError || !list.hasLoaded ? '— 条' : `${list.pagination.total} 条`
)
const emptyDescription = computed(() =>
  list.loading
    ? '正在加载公司行动记录'
    : list.loadError
      ? '公司行动记录暂不可用，请重试'
      : !list.hasLoaded
        ? '公司行动记录尚未加载'
        : activeFilterCount.value
          ? '没有符合当前筛选的公司行动'
          : '暂无公司行动记录'
)
const dateRange = computed<[string, string] | null>(() =>
  list.filters.date_range.length === 2
    ? [list.filters.date_range[0], list.filters.date_range[1]]
    : null
)
const dateRangeLabel = computed(() =>
  dateRange.value
    ? `${formatDate(dateRange.value[0])} 至 ${formatDate(dateRange.value[1])}`
    : '全部日期'
)
function openDateRange() {
  dateDialogOpened.value = true
  dateDialogVisible.value = true
}
function applyDateRange(value: [string, string] | null) {
  list.filters.date_range = value ?? []
  dateDialogVisible.value = false
  list.handleSearch()
}
function typeTag(row: CorporateActionRow) {
  const type = actionTypeTag(row.action_type)
  return type === 'danger' ? 'error' : type === 'info' ? 'default' : type
}
const deleteButtonTheme = {
  textColorTextHoverError: 'var(--app-danger-text)',
  textColorTextPressedError: 'var(--app-danger-text)',
  textColorTextFocusError: 'var(--app-danger-text)'
}
function renderActions(row: CorporateActionRow) {
  if (isReadOnly(row))
    return h('div', { class: 'row-actions' }, [
      h(NTag, { size: 'small', bordered: false }, () => '导入只读'),
      row.action_type === 'OPENING_POSITION' && !openingPositionCostKnown(row)
        ? h(
            NButton,
            {
              text: true,
              type: 'warning',
              onClick: () => costDialog.value?.open(row),
              'aria-label': `补录 ${row.symbol} ${formatDate(row.ex_date)} 成本`
            },
            () => '补录成本'
          )
        : null
    ])
  return h('div', { class: 'row-actions' }, [
    h(
      NButton,
      {
        text: true,
        type: 'primary',
        onClick: () => formDialog.value?.openEdit(row),
        'aria-label': `编辑 ${row.symbol} ${formatDate(row.ex_date)} 公司行动`
      },
      () => '编辑'
    ),
    h(
      NButton,
      {
        text: true,
        type: 'error',
        themeOverrides: deleteButtonTheme,
        onClick: () => list.handleDelete(row),
        'aria-label': `删除 ${row.symbol} ${formatDate(row.ex_date)} 公司行动`
      },
      () => '删除'
    )
  ])
}
const columns: DataTableColumns<CorporateActionRow> = [
  { title: '除权除息日', key: 'ex_date', width: 120, render: (row) => formatDate(row.ex_date) },
  {
    title: '股息到账日',
    key: 'payment_date',
    width: 120,
    render: (row) => (row.action_type === 'CASH_DIVIDEND' ? formatDate(row.payment_date) : '—')
  },
  {
    title: '标的',
    key: 'symbol',
    width: 170,
    render: (row) =>
      h('div', { class: 'security-cell' }, [
        h(RouterLink, { to: holdingsLink(row), class: 'symbol-link' }, () => row.symbol),
        h('span', { class: 'security-name' }, row.name || row.market)
      ])
  },
  { title: '市场', key: 'market', width: 80 },
  {
    title: '类型',
    key: 'action_type',
    width: 120,
    render: (row) =>
      h(NTag, { type: typeTag(row), size: 'small', bordered: false }, () =>
        actionTypeLabel(row.action_type)
      )
  },
  {
    title: '账户',
    key: 'account',
    width: 180,
    render: (row) =>
      h(
        'span',
        { class: !row.broker_account_id ? 'account-unassigned' : '' },
        brokerAccountLabelById(row.broker_account_id)
      )
  },
  { title: '币种', key: 'currency', width: 70 },
  {
    title: '详情',
    key: 'detail',
    width: 240,
    render: (row) => h('div', { class: 'action-detail' }, actionDetail(row))
  },
  {
    title: '备注',
    key: 'notes',
    width: 180,
    render: (row) => h('div', { class: 'action-detail' }, row.notes || '—')
  },
  { title: '操作', key: 'actions', width: 170, fixed: 'right', render: renderActions }
]
</script>
<template>
  <section class="records-tab" :aria-busy="list.loading" aria-labelledby="action-records-heading">
    <header class="detail-heading">
      <h2 id="action-records-heading">
        公司行动记录
        <span class="result-count" data-testid="action-result-count">{{ resultCount }}</span>
      </h2>
      <div class="header-actions">
        <NButton type="primary" @click="formDialog?.openAdd()"
          ><template #icon><Plus /></template>新增记录</NButton
        >
        <NButton
          v-if="isMobileView"
          :aria-expanded="filtersOpen"
          aria-controls="action-filters"
          @click="filtersOpen = !filtersOpen"
          >{{ filtersOpen ? '收起筛选' : '筛选'
          }}{{ activeFilterCount ? `（${activeFilterCount}）` : '' }}</NButton
        >
      </div>
    </header>
    <p v-if="list.loading" role="status" class="list-state">
      正在加载{{ list.hasLoaded ? '，暂保留上次查询结果' : '' }}
    </p>
    <div
      id="action-filters"
      v-show="!isMobileView || filtersOpen"
      class="filter-form ledger-filter-form"
    >
      <NFormItem label="代码" :show-feedback="false"
        ><SecuritySelect
          v-model="list.filters.symbol"
          id="action-symbol-filter"
          :resolve="false"
          placeholder="标的代码"
          class="filter-symbol"
          @select="list.onFilterSymbolSelected"
          @clear="list.handleSearch"
          @keyup.enter="list.handleSearch"
      /></NFormItem>
      <NFormItem label="市场" :show-feedback="false"
        ><select v-model="list.filters.market" aria-label="筛选市场" @change="list.handleSearch">
          <option value="">全部市场</option>
          <option v-for="market in MARKETS" :key="market" :value="market">{{ market }}</option>
        </select></NFormItem
      >
      <NFormItem label="类型" :show-feedback="false"
        ><select
          v-model="list.filters.action_type"
          aria-label="筛选行动类型"
          @change="list.handleSearch"
        >
          <option value="">全部类型</option>
          <option v-for="option in actionTypeOptions" :key="option.value" :value="option.value">
            {{ option.label }}
          </option>
        </select></NFormItem
      >
      <NFormItem label="账户" :show-feedback="false"
        ><select v-model="list.filters.account" aria-label="筛选账户" @change="list.handleSearch">
          <option value="">全部账户</option>
          <option :value="UNASSIGNED_ACCOUNT">{{ UNASSIGNED_ACCOUNT_LABEL }}</option>
          <option v-for="account in brokerAccounts" :key="account.id" :value="account.id">
            {{ accountOptionLabel(account) }}
          </option>
        </select></NFormItem
      >
      <NFormItem label="日期" :show-feedback="false" class="date-filter-item"
        ><NButton
          block
          aria-label="选择公司行动日期范围"
          :aria-expanded="dateDialogVisible"
          @click="openDateRange"
          >{{ dateRangeLabel }}</NButton
        ></NFormItem
      >
      <div class="filter-actions">
        <NButton type="primary" @click="list.handleSearch">查询</NButton
        ><NButton @click="list.resetFilters">重置</NButton>
      </div>
    </div>
    <p class="date-basis-note">
      现金股息按到账日筛选，缺少到账日时使用除息日；其他公司行动按除权日筛选。股息汇总按最新汇率折算
      CNY，收益曲线与 XIRR 按流水当日汇率折算。
    </p>
    <NAlert
      v-if="list.loadError"
      type="error"
      :show-icon="false"
      class="state-alert"
      data-testid="action-load-error"
      >{{
        list.hasLoaded
          ? '记录加载失败，保留上次成功查询的数据。当前筛选结果尚未确认。'
          : '公司行动记录加载失败，数量与记录暂不可用。'
      }}<NButton @click="list.loadActions">重试记录</NButton></NAlert
    >
    <NAlert
      v-if="list.summaryError"
      type="error"
      :show-icon="false"
      class="state-alert"
      data-testid="action-summary-error"
      >{{
        list.summaryHasLoaded
          ? '汇总加载失败，保留上次成功的汇总。当前筛选总额尚未确认。'
          : '汇总加载失败，记录数量与股息总额未知。'
      }}<NButton @click="list.loadActions">重试汇总</NButton></NAlert
    >
    <p v-if="list.summaryLoading" role="status" class="list-state">正在加载当前筛选汇总…</p>
    <NAlert
      v-if="list.summary?.cash_dividends?.legacy_unreviewed_count"
      type="warning"
      class="state-alert"
      :title="`${list.summary.cash_dividends.legacy_unreviewed_count} 笔历史股息仍按旧账本计入，需依据凭证核对；并非已逐笔确认。`"
    />
    <NAlert
      v-if="list.summary?.cash_dividends?.amounts_incomplete_count"
      type="info"
      class="state-alert"
      title="部分股息仅净到账额已知；税前与预扣税汇总为已知部分，未知税额不代表免税。"
    />
    <NAlert
      v-if="list.summary?.cash_dividends?.missing_rate_currencies?.length"
      type="warning"
      class="state-alert"
      :title="`缺少 ${list.summary.cash_dividends.missing_rate_currencies.join('/')} 汇率，对应股息未计入 CNY 折算总额，请先在汇率页补录`"
    />
    <div class="summary-grid" :aria-busy="list.summaryLoading">
      <FinancialStatistic
        title="总记录数"
        :value="list.summaryLoading ? null : list.summary?.total_count"
        :formatter="(value) => formatNumber(value, 0)"
      />
      <FinancialStatistic
        title="股息总额（CNY折算）"
        :value="list.summaryLoading ? null : list.summary?.cash_dividends?.total_dividend"
        :formatter="formatCurrency"
      />
      <FinancialStatistic
        title="预扣税（CNY折算）"
        :value="list.summaryLoading ? null : list.summary?.cash_dividends?.total_tax"
        :formatter="formatCurrency"
      />
      <FinancialStatistic
        title="税后净额（CNY折算）"
        :value="list.summaryLoading ? null : list.summary?.cash_dividends?.net_dividend"
        :formatter="formatCurrency"
      />
    </div>
    <NDataTable
      v-if="!isMobileView"
      class="records-table"
      :columns="columns"
      :data="list.actions"
      :loading="list.loading"
      :row-key="(row) => row.id"
      :scroll-x="1450"
      :max-height="560"
      :bordered="false"
      ><template #empty><NEmpty :description="emptyDescription" /></template
    ></NDataTable>
    <NSpin v-else :show="list.loading"
      ><div class="mobile-card-list">
        <NEmpty v-if="!list.actions.length" :description="emptyDescription" />
        <article
          v-for="row in list.actions"
          :key="row.id"
          class="mobile-card"
          data-testid="corporate-action-card"
        >
          <div class="mobile-card-head">
            <div class="mobile-card-title">
              <RouterLink :to="holdingsLink(row)" class="mobile-card-symbol">{{
                row.symbol
              }}</RouterLink
              ><span class="mobile-card-name">{{ row.name || row.market }}</span>
            </div>
            <NTag :type="typeTag(row)" size="small" :bordered="false">{{
              actionTypeLabel(row.action_type)
            }}</NTag>
          </div>
          <div class="action-detail">{{ actionDetail(row) }}</div>
          <div class="mobile-card-meta">
            <span>除权除息日 {{ formatDate(row.ex_date) }}</span
            ><span v-if="row.action_type === 'CASH_DIVIDEND'"
              >到账日 {{ formatDate(row.payment_date) }}</span
            ><span>{{ row.market }} · {{ row.currency }}</span
            ><span :class="{ 'account-unassigned': !row.broker_account_id }">{{
              brokerAccountLabelById(row.broker_account_id)
            }}</span
            ><span v-if="row.notes">{{ row.notes }}</span>
          </div>
          <div class="mobile-card-actions">
            <template v-if="isReadOnly(row)"
              ><NTag size="small" :bordered="false">导入只读</NTag
              ><NButton
                v-if="row.action_type === 'OPENING_POSITION' && !openingPositionCostKnown(row)"
                text
                type="warning"
                :aria-label="`补录 ${row.symbol} ${formatDate(row.ex_date)} 成本`"
                @click="costDialog?.open(row)"
                >补录成本</NButton
              ></template
            ><template v-else
              ><NButton
                text
                type="primary"
                :aria-label="`编辑 ${row.symbol} ${formatDate(row.ex_date)} 公司行动`"
                @click="formDialog?.openEdit(row)"
                >编辑</NButton
              ><NButton
                text
                type="error"
                :theme-overrides="deleteButtonTheme"
                :aria-label="`删除 ${row.symbol} ${formatDate(row.ex_date)} 公司行动`"
                @click="list.handleDelete(row)"
                >删除</NButton
              ></template
            >
          </div>
        </article>
      </div></NSpin
    >
    <div class="pagination-bar">
      <span class="pagination-info">共 {{ resultCount }}</span
      ><el-pagination
        v-model:current-page="list.pagination.page"
        v-model:page-size="list.pagination.pageSize"
        :total="list.pagination.total"
        :page-sizes="[25, 50, 100, 200]"
        :pager-count="isMobileView ? 5 : 7"
        layout="sizes, prev, pager, next, jumper"
        :disabled="list.loading || list.loadError"
        @size-change="list.handlePageSizeChange"
        @current-change="list.loadActions"
      />
    </div>
    <DateRangeDialog
      v-if="dateDialogOpened"
      title="公司行动日期范围"
      v-model:show="dateDialogVisible"
      :date-range="dateRange"
      @apply="applyDateRange"
    />
    <CorporateActionFormDialog
      ref="formDialog"
      :broker-accounts="brokerAccounts"
      @saved="list.loadActions"
    /><OpeningCostDialog ref="costDialog" @saved="list.loadActions" />
  </section>
</template>
<style scoped>
.records-tab {
  container-type: inline-size;
  width: 100%;
  min-width: 0;
}
.detail-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin: 24px 0;
}
.detail-heading h2 {
  font-size: 16px;
  font-weight: 500;
  margin: 0;
}
.result-count {
  font-size: 12px;
  color: var(--app-text-muted);
  margin-left: 10px;
}
.header-actions,
.filter-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.date-basis-note,
.list-state {
  font-size: 12px;
  line-height: 1.8;
  color: var(--app-text-muted);
  margin: 14px 0;
}
.state-alert {
  margin: 12px 0;
}
.state-alert .n-button {
  margin-left: 12px;
}
.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 20px;
  padding: 22px 0 26px;
  border-bottom: 1px solid var(--app-border);
  margin-bottom: 16px;
}
.records-table :deep(.security-cell) {
  display: grid;
  gap: 4px;
}
.records-table :deep(.security-name) {
  font-size: 12px;
  color: var(--app-text-muted);
  overflow-wrap: anywhere;
}
.records-table :deep(.symbol-link),
.mobile-card-symbol {
  color: var(--app-primary-strong);
  text-decoration: none;
  font-weight: 600;
}
.records-table :deep(.symbol-link:hover),
.mobile-card-symbol:hover {
  text-decoration: underline;
}
.records-table :deep(.row-actions) {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}
.records-table :deep(.row-actions .n-button) {
  min-height: 24px;
}
.records-table :deep(.account-unassigned),
.account-unassigned {
  color: var(--app-text-muted);
}
.records-table :deep(.action-detail),
.action-detail {
  font-size: 13px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}
.pagination-bar {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 16px;
  flex-wrap: wrap;
  padding: 20px 0;
}
.pagination-info {
  font-size: 12px;
  color: var(--app-text-muted);
}
@media (max-width: 900px) {
  .summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .pagination-bar {
    justify-content: flex-start;
  }
}
@media (max-width: 640px) {
  .detail-heading {
    align-items: flex-start;
    flex-wrap: wrap;
  }
  .records-tab .n-button {
    min-height: 44px;
  }
  .summary-grid {
    gap: 20px 12px;
  }
  .summary-grid :deep(.financial-statistic-value) {
    font-size: 24px;
  }
  .mobile-card-list {
    margin-top: 16px;
  }
  .mobile-card {
    background: var(--app-surface);
    border: 1px solid var(--app-border-soft);
    border-radius: 3px;
    box-shadow: none;
  }
  .mobile-card-meta {
    line-height: 1.8;
  }
  .mobile-card-actions {
    display: flex;
    gap: 20px;
    flex-wrap: wrap;
  }
  .pagination-bar :deep(.el-pagination) {
    flex-wrap: wrap;
    gap: 10px;
  }
  .pagination-bar :deep(.el-pagination .btn-prev),
  .pagination-bar :deep(.el-pagination .btn-next),
  .pagination-bar :deep(.el-pager li) {
    min-height: 44px;
    min-width: 36px;
  }
  .pagination-bar :deep(.el-pagination__sizes .el-select__wrapper) {
    min-height: 44px;
  }
}
</style>
