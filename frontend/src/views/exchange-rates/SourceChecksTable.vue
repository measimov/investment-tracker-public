<script setup lang="ts">
import { h } from 'vue'
import { NDataTable, NEmpty, NSpin, type DataTableColumns } from 'naive-ui'
import type { ExchangeRateCheck } from '@/types'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { formatDate, formatNumber, formatPercent } from '@/utils/helpers'
import { isDiffAbnormal, sourceLabel } from './sources'
defineProps<{ rows: ExchangeRateCheck[]; loading: boolean; emptyDescription: string }>()
const isMobileView = useMediaQuery('(max-width:640px)')
const columns: DataTableColumns<ExchangeRateCheck> = [
  {
    title: '比对日',
    key: 'check_date',
    width: 110,
    render: (row) => h('span', { class: 'num' }, formatDate(row.check_date))
  },
  { title: '币种', key: 'from_currency', width: 80 },
  {
    title: '官方中间价',
    key: 'official_rate',
    width: 180,
    align: 'right',
    render: (row) =>
      h('div', [
        h('span', { class: 'num' }, formatNumber(row.official_rate, 4)),
        h('div', { class: 'cell-sub num' }, formatDate(row.official_date))
      ])
  },
  {
    title: '第三方报价',
    key: 'reference_rate',
    width: 200,
    align: 'right',
    render: (row) =>
      h('div', [
        h('span', { class: 'num' }, formatNumber(row.reference_rate, 4)),
        h('div', { class: 'cell-sub' }, sourceLabel(row.reference_source))
      ])
  },
  {
    title: '差异',
    key: 'diff_pct',
    width: 100,
    align: 'right',
    render: (row) =>
      h(
        'span',
        { class: ['num', { 'is-abnormal': isDiffAbnormal(row.diff_pct) }] },
        formatPercent(row.diff_pct)
      )
  }
]
</script>
<template>
  <NSpin :show="loading">
    <NEmpty
      v-if="!rows.length"
      :description="emptyDescription"
      :theme-overrides="{ textColor: 'var(--app-text-muted)' }"
    />
    <div v-else-if="isMobileView" class="check-cards">
      <details
        v-for="row in rows"
        :key="`${row.check_date}-${row.from_currency}`"
        class="check-card"
      >
        <summary>
          <span
            >{{ row.from_currency }}
            <span class="cell-sub num">{{ formatDate(row.check_date) }}</span></span
          ><span class="num" :class="{ 'is-abnormal': isDiffAbnormal(row.diff_pct) }">{{
            formatPercent(row.diff_pct)
          }}</span>
        </summary>
        <dl>
          <div>
            <dt>官方中间价</dt>
            <dd class="num">
              {{ formatNumber(row.official_rate, 4)
              }}<span class="cell-sub">官方日期 {{ formatDate(row.official_date) }}</span>
            </dd>
          </div>
          <div>
            <dt>第三方报价</dt>
            <dd class="num">
              {{ formatNumber(row.reference_rate, 4)
              }}<span class="cell-sub">{{ sourceLabel(row.reference_source) }}</span>
            </dd>
          </div>
        </dl>
      </details>
    </div>
    <NDataTable
      v-else
      class="checks-table"
      :data="rows"
      :columns="columns"
      :row-key="(row: ExchangeRateCheck) => `${row.check_date}-${row.from_currency}`"
      :scroll-x="670"
      :max-height="320"
      :bordered="false"
      ><template #empty><NEmpty :description="emptyDescription" /></template
    ></NDataTable>
  </NSpin>
</template>
<style scoped>
.num,
.checks-table :deep(.num) {
  font-variant-numeric: tabular-nums;
}
.cell-sub,
.checks-table :deep(.cell-sub) {
  font-size: 12px;
  color: var(--app-text-muted);
  line-height: 1.6;
}
.is-abnormal,
.checks-table :deep(.is-abnormal) {
  color: var(--app-danger-text);
}
.check-cards {
  display: grid;
  gap: 8px;
}
.check-card {
  padding: 0 16px;
  border: 1px solid var(--app-border);
  border-radius: 6px;
  background: var(--app-surface);
  min-width: 0;
}
.check-card summary {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  min-height: 48px;
  cursor: pointer;
  font-size: 14px;
}
.check-card summary > span:first-child {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
.check-card summary::after {
  content: '+';
  color: var(--app-text-muted);
}
.check-card[open] summary::after {
  content: '−';
}
.check-card dl {
  margin: 0;
  padding: 0 0 12px;
  display: grid;
  gap: 12px;
}
.check-card dl > div {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}
.check-card dt {
  font-size: 13px;
  color: var(--app-text-muted);
}
.check-card dd {
  margin: 0;
  text-align: right;
  overflow-wrap: anywhere;
}
.check-card dd .cell-sub {
  display: block;
}
</style>
