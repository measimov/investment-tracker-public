<script setup lang="ts">
import { computed, h, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import {
  NButton,
  NDataTable,
  NEmpty,
  NSpin,
  NTag,
  type DataTableColumns,
  type DataTableProps
} from 'naive-ui'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { formatDate, formatPrice, formatQuantity } from '@/utils/helpers'
import { holdingsLink } from '@/utils/securities'
import type { BrokerAccount } from '@/types'
import type { Transaction } from '@/stores/transactions'
import {
  type AccountListStatus,
  accountLabel,
  transactionTypeLabel,
  transactionTypeTag
} from '@/utils/labels'
import { isTransfer } from './shared'
import type { TransactionsListFeature } from './useTransactionsList'
const props = defineProps<{
  list: TransactionsListFeature
  brokerAccounts: BrokerAccount[]
  brokerAccountsStatus: AccountListStatus
}>()
const emit = defineEmits<{ edit: [row: Transaction] }>()
const isMobileView = useMediaQuery('(max-width: 640px)')
const expanded = ref<number[]>([])
watch(
  () => props.list.transactions,
  () => {
    expanded.value = []
  }
)
const renderExpandIcon: NonNullable<DataTableProps['renderExpandIcon']> = ({
  expanded: isExpanded,
  rowData
}) =>
  h(
    'button',
    {
      type: 'button',
      class: 'notes-expand-button',
      'aria-label': `${isExpanded ? '收起' : '查看'} ${rowData.symbol} ${formatDate(rowData.transaction_date)} 的交易备注`,
      'aria-expanded': isExpanded,
      onClick: (event: MouseEvent) => {
        event.stopPropagation()
        expanded.value = isExpanded
          ? expanded.value.filter((id) => id !== rowData.id)
          : [...expanded.value, rowData.id]
      }
    },
    isExpanded ? '⌄' : '›'
  )
const deleteButtonTheme = {
  textColorTextHoverError: 'var(--app-danger-text)',
  textColorTextPressedError: 'var(--app-danger-text)',
  textColorTextFocusError: 'var(--app-danger-text)'
}

function brokerAccountLabelById(id: number | null | undefined) {
  return accountLabel(props.brokerAccounts, id, { status: props.brokerAccountsStatus })
}

const isFiltered = computed(
  () =>
    !!(
      props.list.filters.symbol ||
      props.list.filters.market ||
      props.list.filters.transaction_type ||
      props.list.filters.account ||
      props.list.filters.dateRange
    )
)
const emptyDescription = computed(() =>
  props.list.loading
    ? '正在加载交易记录'
    : props.list.loadError
      ? '交易记录暂不可用，请重试'
      : isFiltered.value
        ? '没有符合当前筛选的交易'
        : '暂无交易记录'
)
function typeTag(row: Transaction) {
  const type = transactionTypeTag(row.transaction_type)
  return type === 'danger' ? 'error' : type === 'info' ? 'default' : type
}
function renderType(row: Transaction) {
  return h(NTag, { type: typeTag(row), size: 'small', bordered: false }, () =>
    transactionTypeLabel(row.transaction_type)
  )
}
function renderActions(row: Transaction) {
  if (row.read_only)
    return h(NTag, { size: 'small', bordered: false, title: '对账单导入 · 只读' }, () => '只读')
  return h('div', { class: 'row-actions' }, [
    !isTransfer(row)
      ? h(
          NButton,
          {
            text: true,
            type: 'primary',
            onClick: () => emit('edit', row),
            'aria-label': `编辑 ${row.symbol} ${formatDate(row.transaction_date)} 交易`
          },
          () => '编辑'
        )
      : null,
    h(
      NButton,
      {
        text: true,
        type: 'error',
        themeOverrides: deleteButtonTheme,
        onClick: () => props.list.handleDelete(row),
        'aria-label': `删除 ${row.symbol} ${formatDate(row.transaction_date)} 交易`
      },
      () => '删除'
    )
  ])
}
const columns: DataTableColumns<Transaction> = [
  {
    type: 'expand',
    width: 36,
    expandable: (row) => !!row.notes,
    renderExpand: (row) =>
      h('div', { class: 'transaction-notes', 'data-testid': 'transaction-notes' }, [
        h('strong', '交易备注'),
        h('p', row.notes ?? '')
      ])
  },
  {
    title: '交易日期',
    key: 'transaction_date',
    width: 110,
    render: (row) => formatDate(row.transaction_date)
  },
  {
    title: '标的',
    key: 'symbol',
    width: 180,
    render: (row) =>
      h('div', { class: 'security-cell' }, [
        h(
          RouterLink,
          { to: holdingsLink(row), class: 'symbol-link', 'data-testid': 'transaction-symbol-link' },
          () => row.symbol
        ),
        h('span', { class: 'security-name' }, row.name ? `${row.name} · ${row.market}` : row.market)
      ])
  },
  {
    title: '账户',
    key: 'account',
    width: 150,
    render: (row) =>
      h(
        'span',
        { class: !row.broker_account_id ? 'account-unassigned' : '' },
        brokerAccountLabelById(row.broker_account_id)
      )
  },
  { title: '类型', key: 'transaction_type', width: 88, render: renderType },
  {
    title: '数量',
    key: 'quantity',
    className: 'transaction-number-column',
    width: 100,
    align: 'right',
    render: (row) => formatQuantity(row.quantity)
  },
  {
    title: '价格',
    key: 'price',
    className: 'transaction-number-column',
    width: 100,
    align: 'right',
    render: (row) =>
      h('div', { class: 'price-cell' }, [
        h('div', formatPrice(row.price)),
        h('div', { class: 'currency-label' }, row.currency)
      ])
  },
  {
    title: '手续费',
    key: 'fee',
    className: 'transaction-number-column',
    width: 88,
    align: 'right',
    render: (row) => formatPrice(row.fee)
  },
  { title: '操作', key: 'actions', width: 100, render: renderActions }
]
</script>

<template>
  <div class="transactions-list" data-testid="transaction-list-start">
    <NDataTable
      v-if="!isMobileView"
      :columns="columns"
      :data="list.transactions"
      :loading="list.loading"
      :row-key="(row: Transaction) => row.id"
      :scroll-x="952"
      v-model:expanded-row-keys="expanded"
      :render-expand-icon="renderExpandIcon"
      :bordered="false"
      class="desktop-data-table"
      data-testid="transactions-table"
    >
      <template #empty><NEmpty :description="emptyDescription" /></template>
    </NDataTable>
    <NSpin v-else :show="list.loading" class="mobile-spin"
      ><div class="mobile-card-list">
        <article
          v-for="row in list.transactions"
          :key="row.id"
          class="mobile-card"
          data-testid="transaction-card"
        >
          <div class="mobile-card-head">
            <div class="mobile-card-title">
              <router-link
                :to="holdingsLink(row)"
                class="mobile-card-symbol symbol-link"
                data-testid="transaction-symbol-link"
              >
                {{ row.symbol }}
              </router-link>
              <span class="mobile-card-name">{{ row.name || row.market }}</span>
            </div>
            <div class="mobile-card-actions mobile-card-top-actions">
              <NTag :type="typeTag(row)" size="small" :bordered="false">
                {{ transactionTypeLabel(row.transaction_type) }}
              </NTag>
              <template v-if="!row.read_only">
                <NButton
                  v-if="!isTransfer(row)"
                  type="primary"
                  text
                  :aria-label="`编辑 ${row.symbol} ${formatDate(row.transaction_date)} 交易`"
                  @click="$emit('edit', row)"
                  >编辑</NButton
                >
                <NButton
                  type="error"
                  :theme-overrides="deleteButtonTheme"
                  size="small"
                  text
                  :aria-label="`删除 ${row.symbol} ${formatDate(row.transaction_date)} 交易`"
                  @click="list.handleDelete(row)"
                  >删除</NButton
                >
              </template>
              <NTag v-else size="small" :bordered="false">对账单导入 · 只读</NTag>
            </div>
          </div>

          <div class="transaction-amount">
            <span
              >{{ formatDate(row.transaction_date) }} · {{ row.market }} · {{ row.currency }}</span
            >
            <strong>{{ formatQuantity(row.quantity) }} × {{ formatPrice(row.price) }}</strong>
          </div>

          <div class="mobile-card-meta">
            <span :class="{ 'account-unassigned': !row.broker_account_id }">
              账户：{{ brokerAccountLabelById(row.broker_account_id) }}
            </span>
            <span>手续费 {{ formatPrice(row.fee) }}</span>
            <button
              v-if="row.notes"
              type="button"
              class="mobile-notes-trigger"
              :aria-label="`${expanded.includes(row.id) ? '收起' : '查看'} ${row.symbol} ${formatDate(row.transaction_date)} 的交易备注`"
              :aria-expanded="expanded.includes(row.id)"
              :aria-controls="
                expanded.includes(row.id) ? `transaction-mobile-notes-${row.id}` : undefined
              "
              @click="
                expanded = expanded.includes(row.id)
                  ? expanded.filter((id) => id !== row.id)
                  : [...expanded, row.id]
              "
            >
              {{ expanded.includes(row.id) ? '收起交易备注' : '查看交易备注' }}
            </button>
          </div>
          <p
            v-if="row.notes && expanded.includes(row.id)"
            :id="`transaction-mobile-notes-${row.id}`"
            class="transaction-notes mobile-notes"
            data-testid="transaction-notes"
          >
            {{ row.notes }}
          </p>
        </article>
        <NEmpty
          v-if="!list.loading && list.transactions.length === 0"
          :description="emptyDescription"
        /></div
    ></NSpin>
  </div>
</template>
<style scoped>
.transactions-list {
  min-width: 0;
}
:deep(.notes-expand-button) {
  display: inline-grid;
  place-items: center;
  width: 24px;
  height: 28px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--app-primary-strong);
  cursor: pointer;
  font: inherit;
}
:deep(.notes-expand-button:focus-visible),
.mobile-notes-trigger:focus-visible {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: 2px;
}
:deep(.transaction-notes),
.transaction-notes {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
:deep(.transaction-notes) {
  padding: 12px;
}
:deep(.transaction-notes p) {
  margin: 8px 0 0;
}
:deep(.price-cell) {
  white-space: nowrap;
}
:deep(.currency-label) {
  font-size: 12px;
  color: var(--app-text-muted);
  margin-top: 4px;
}
.mobile-notes-trigger {
  min-height: 44px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--app-primary-strong);
  font: inherit;
  cursor: pointer;
}
.mobile-notes {
  min-width: 0;
  margin: 8px 0 0;
  text-align: left;
}
.mobile-card-head .mobile-card-top-actions {
  margin: 0;
  padding: 0;
  border: 0;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
  max-width: 65%;
}
.mobile-card-title {
  flex: 1;
}
.mobile-card-name {
  white-space: normal;
  overflow-wrap: anywhere;
  overflow: visible;
  text-overflow: clip;
}
.mobile-card-meta {
  align-items: center;
}
:deep(.transaction-number-column) {
  white-space: nowrap;
}

:deep(.security-cell) {
  display: grid;
  gap: 4px;
}
:deep(.security-name) {
  color: var(--app-text-muted);
  font-size: 13px;
  overflow-wrap: anywhere;
}
:deep(.account-unassigned) {
  color: var(--app-warning-text);
}
:deep(.symbol-link) {
  color: var(--app-primary-strong);
  text-decoration: none;
  font-weight: 600;
}
:deep(.symbol-link:hover),
:deep(.symbol-link:focus-visible) {
  text-decoration: underline;
}
:deep(.n-data-table-td) {
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
:deep(.row-actions) {
  display: flex;
  gap: 12px;
}
:deep(.row-actions .n-button) {
  min-height: 24px;
}
.mobile-spin {
  width: 100%;
}
.mobile-card-list {
  display: grid;
  gap: 12px;
}
.mobile-card {
  padding: 16px;
  background: var(--app-surface);
  border: 1px solid var(--app-border-soft);
}
.transaction-amount {
  display: grid;
  gap: 6px;
}
.transaction-amount span {
  color: var(--app-text-muted);
  font-size: 13px;
}
.transaction-amount strong {
  color: var(--app-text);
  font-size: 20px;
  line-height: 1.4;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.mobile-card-meta {
  color: var(--app-text-muted);
  font-size: 13px;
}
.mobile-card-actions :deep(.n-button) {
  min-height: 44px;
  padding-inline: 12px;
}
</style>
