<script setup lang="ts">
import { computed, h, ref } from 'vue'
import { useMediaQuery } from '@vueuse/core'
import { NDataTable, NEmpty, NSpin, NTag, type DataTableColumns } from 'naive-ui'
import type { AdminHolding } from '@/types'
import { UNASSIGNED_ACCOUNT_LABEL } from '@/utils/labels'
import {
  EMPTY,
  formatCurrency,
  formatDateTime,
  formatPercent,
  formatPrice,
  formatQuantity,
  pnlClass
} from '@/utils/helpers'
import { rowMarketValue, rowProfit, rowProfitPercent } from '../adminHoldings'
const props = defineProps<{
  rows: AdminHolding[]
  loading: boolean
  allUsers: boolean
  emptyDescription: string
}>()
const mobile = useMediaQuery('(max-width: 640px)')
const sort = ref<'ascending' | 'descending' | null>(null)
const sortLabel = computed(() =>
  sort.value === 'ascending' ? '升序' : sort.value === 'descending' ? '降序' : '未排序'
)
function cycleSort() {
  sort.value = sort.value === null ? 'ascending' : sort.value === 'ascending' ? 'descending' : null
}
const data = computed(() =>
  !sort.value
    ? props.rows
    : [...props.rows].sort(
        (a, b) =>
          (new Date(a.updated_at).getTime() - new Date(b.updated_at).getTime()) *
          (sort.value === 'ascending' ? 1 : -1)
      )
)
function accountLabel(row: AdminHolding) {
  return row.broker_account_id === null || row.broker_account_id === undefined
    ? UNASSIGNED_ACCOUNT_LABEL
    : `账户 ${row.broker_account_id}`
}
function money(value: number | string | null | undefined, row: AdminHolding) {
  return formatCurrency(value, row.currency)
}
const columns = computed<DataTableColumns<AdminHolding>>(() => {
  const columns: DataTableColumns<AdminHolding> = []
  if (props.allUsers)
    columns.push({
      key: 'user',
      title: '用户',
      width: 180,
      render: (row) =>
        h('div', { class: 'user-cell' }, [
          h('strong', {}, row.username || EMPTY),
          h('span', { class: 'subline' }, `ID ${row.user_id}`)
        ])
    })
  columns.push(
    {
      key: 'security',
      title: '标的',
      width: 230,
      render: (row) =>
        h('div', { class: 'security-cell' }, [
          h('strong', {}, row.symbol),
          h('span', { class: 'security-name' }, row.name || EMPTY),
          h('span', { class: 'subline' }, accountLabel(row))
        ])
    },
    {
      key: 'market',
      title: '市场',
      width: 80,
      render: (row) => h(NTag, { size: 'small', bordered: false }, { default: () => row.market })
    },
    {
      key: 'quantity',
      title: '持仓量',
      width: 110,
      align: 'right',
      render: (row) => formatQuantity(row.quantity)
    },
    {
      key: 'avg_cost',
      title: '平均成本',
      width: 110,
      align: 'right',
      render: (row) => formatPrice(row.avg_cost)
    },
    {
      key: 'current_price',
      title: '现价',
      width: 110,
      align: 'right',
      render: (row) => formatPrice(row.current_price)
    },
    { key: 'currency', title: '币种', width: 70 },
    {
      key: 'total_cost',
      title: '总成本',
      width: 135,
      align: 'right',
      render: (row) => money(row.total_cost, row)
    },
    {
      key: 'marketValue',
      title: '当前市值',
      width: 135,
      align: 'right',
      render: (row) => money(rowMarketValue(row), row)
    },
    {
      key: 'profit',
      title: '浮动盈亏',
      width: 135,
      align: 'right',
      render: (row) => h('span', { class: pnlClass(rowProfit(row)) }, money(rowProfit(row), row))
    },
    {
      key: 'profitPercent',
      title: '浮动盈亏率',
      width: 100,
      align: 'right',
      render: (row) =>
        h('span', { class: pnlClass(rowProfit(row)) }, formatPercent(rowProfitPercent(row)))
    },
    {
      key: 'updated_at',
      title: () =>
        h(
          'button',
          {
            type: 'button',
            class: 'sort-button',
            'aria-label': `最后更新排序：${sortLabel.value}`,
            onClick: cycleSort
          },
          `最后更新 ${sort.value === 'ascending' ? '↑' : sort.value === 'descending' ? '↓' : '↕'}`
        ),
      width: 165,
      render: (row) => formatDateTime(row.updated_at)
    }
  )
  return columns
})
</script>
<template>
  <NSpin :show="loading">
    <div v-if="!mobile" class="desktop-holdings">
      <NDataTable
        :columns="columns"
        :data="data"
        :row-key="(row: AdminHolding) => row.id"
        :scroll-x="allUsers ? 1660 : 1480"
        :bordered="true"
        :single-line="true"
        aria-label="管理员持仓明细"
        data-testid="admin-holdings-table"
      >
        <template #empty
          ><NEmpty
            :description="emptyDescription"
            :theme-overrides="{ textColor: 'var(--app-text-muted)' }"
        /></template>
      </NDataTable>
      <p class="scroll-note">表格可横向滚动查看完整明细。单行金额为原币，汇总折为人民币。</p>
    </div>
    <div v-else class="mobile-holdings">
      <button
        v-if="rows.length"
        type="button"
        class="sort-button mobile-sort"
        :aria-label="`最后更新排序：${sortLabel}`"
        @click="cycleSort"
      >
        最后更新：{{ sortLabel }} <span aria-hidden="true">↕</span>
      </button>
      <NEmpty
        v-if="!rows.length"
        :description="emptyDescription"
        :theme-overrides="{ textColor: 'var(--app-text-muted)' }"
      />
      <article
        v-for="row in data"
        :key="row.id"
        class="holding-card"
        data-testid="admin-holding-card"
      >
        <header>
          <div>
            <strong class="symbol">{{ row.symbol }}</strong>
            <h3>{{ row.name || EMPTY }}</h3>
          </div>
          <NTag size="small" :bordered="false">{{ row.market }}</NTag>
        </header>
        <p v-if="allUsers" class="user-line">
          {{ row.username || EMPTY }} · 用户ID {{ row.user_id }}
        </p>
        <p class="account-line">{{ accountLabel(row) }} · {{ row.currency }}</p>
        <dl>
          <div>
            <dt>持仓量</dt>
            <dd>{{ formatQuantity(row.quantity) }}</dd>
          </div>
          <div>
            <dt>平均成本</dt>
            <dd>
              {{ formatPrice(row.avg_cost) }} <span class="unit">{{ row.currency }}</span>
            </dd>
          </div>
          <div>
            <dt>现价</dt>
            <dd>
              {{ formatPrice(row.current_price) }} <span class="unit">{{ row.currency }}</span>
            </dd>
          </div>
          <div>
            <dt>总成本</dt>
            <dd>{{ money(row.total_cost, row) }}</dd>
          </div>
          <div>
            <dt>当前市值</dt>
            <dd>{{ money(rowMarketValue(row), row) }}</dd>
          </div>
          <div>
            <dt>浮动盈亏</dt>
            <dd :class="pnlClass(rowProfit(row))">{{ money(rowProfit(row), row) }}</dd>
          </div>
          <div>
            <dt>浮动盈亏率</dt>
            <dd :class="pnlClass(rowProfit(row))">{{ formatPercent(rowProfitPercent(row)) }}</dd>
          </div>
          <div class="updated">
            <dt>最后更新</dt>
            <dd>{{ formatDateTime(row.updated_at) }}</dd>
          </div>
        </dl>
      </article>
    </div>
  </NSpin>
</template>
<style scoped>
.desktop-holdings,
.mobile-holdings {
  min-width: 0;
}
.desktop-holdings :deep(.user-cell),
.desktop-holdings :deep(.security-cell) {
  display: grid;
  gap: 6px;
  overflow-wrap: anywhere;
}
.desktop-holdings :deep(.subline) {
  color: var(--app-text-muted);
  font-size: 12px;
}
.desktop-holdings :deep(.security-name) {
  line-height: 1.6;
}
.desktop-holdings :deep(.sort-button),
.sort-button {
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  padding: 0;
  min-height: 24px;
  cursor: pointer;
}
.desktop-holdings :deep(.sort-button:focus-visible),
.sort-button:focus-visible {
  outline: 2px solid var(--app-primary);
  outline-offset: 4px;
}
.scroll-note {
  color: var(--app-text-muted);
  font-size: 12px;
  line-height: 1.7;
  margin: 10px 0 0;
}
.mobile-sort {
  display: flex;
  gap: 12px;
  min-height: 44px;
  margin-bottom: 12px;
  color: var(--app-text-muted);
}
.holding-card {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 12px;
}
.holding-card header {
  display: flex;
  gap: 12px;
  justify-content: space-between;
  align-items: flex-start;
}
.holding-card header > div {
  min-width: 0;
}
.symbol {
  font-size: 16px;
}
h3 {
  margin: 6px 0 0;
  font-size: 15px;
  line-height: 1.7;
  overflow-wrap: anywhere;
  font-weight: 500;
}
.user-line,
.account-line {
  color: var(--app-text-muted);
  font-size: 12px;
  line-height: 1.7;
  overflow-wrap: anywhere;
  margin: 12px 0 0;
}
.account-line {
  margin-top: 6px;
}
dl {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin: 20px 0 0;
}
dt {
  color: var(--app-text-muted);
  font-size: 12px;
  line-height: 1.7;
}
dd {
  margin: 4px 0 0;
  font-size: 14px;
  line-height: 1.6;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.unit {
  font-size: 11px;
  color: var(--app-text-muted);
}
.updated {
  grid-column: 1 / -1;
}
</style>
