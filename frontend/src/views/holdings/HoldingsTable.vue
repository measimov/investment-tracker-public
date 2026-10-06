<script setup lang="ts">
import { computed, h, nextTick, onMounted, ref, watch } from 'vue'
import {
  NAlert,
  NButton,
  NDataTable,
  NEmpty,
  NSpin,
  NTag,
  type DataTableProps,
  type DataTableColumns
} from 'naive-ui'
import { ChevronRight as ArrowRight } from '@lucide/vue'
import { useMediaQuery } from '@/composables/useMediaQuery'
import {
  formatNumber,
  formatCurrency,
  formatPercent,
  formatPrice,
  formatQuantity,
  toNumber
} from '@/utils/helpers'
import type { Holding } from '@/stores/holdings'
import type { HoldingRow, HoldingsTableFeature, HoldingsSortProp } from './useHoldingsTable'
import type { SecurityBadgesFeature } from './useSecurityBadges'
import { industryTooltip } from './display'
import AnalysisBadges from './AnalysisBadges.vue'
import PriceEditor from './PriceEditor.vue'
import PriceFlags from './PriceFlags.vue'
import HoldingTableCell from './HoldingTableCell.vue'

const props = defineProps<{ table: HoldingsTableFeature; badges: SecurityBadgesFeature }>()
const emit = defineEmits<{ transfer: [row: Holding] }>()
const isMobileView = useMediaQuery('(max-width: 640px)')
const rootEl = ref<HTMLElement | null>(null)
const expanded = ref<string[]>([])
const hasExpandableRows = computed(() => props.table.rows.some((row) => row.accounts.length > 1))
const scrollX = computed(() =>
  props.table.state.viewMode === 'account' ? 1132 : 1060 + (hasExpandableRows.value ? 36 : 0)
)
const detailPath = (row: { symbol: string; market: string }) =>
  `/securities/${encodeURIComponent(row.market)}/${encodeURIComponent(row.symbol)}`
function sortTitle(label: string, prop: HoldingsSortProp) {
  const selected = props.table.state.sort.prop === prop
  const order = selected ? props.table.state.sort.order : null
  return h(
    'button',
    {
      type: 'button',
      class: 'table-sort-button',
      title: '按折人民币金额排序，缺值排末',
      'aria-label': `${label}：按折人民币金额排序，缺值排末${order === 'descending' ? '，当前降序' : order === 'ascending' ? '，当前升序' : ''}`,
      onClick: () =>
        props.table.setSort({
          prop,
          order: selected && order === 'descending' ? 'ascending' : 'descending'
        })
    },
    [
      label,
      h(
        'span',
        { 'aria-hidden': 'true' },
        order === 'descending' ? ' ↓' : order === 'ascending' ? ' ↑' : ' ↕'
      )
    ]
  )
}
const columns = computed<DataTableColumns<HoldingRow>>(() => {
  const cell =
    (column: 'security' | 'quantity' | 'price' | 'value' | 'profit' | 'analysis') =>
    (row: HoldingRow) =>
      h(HoldingTableCell, { table: props.table, badges: props.badges, row, column })
  const result: DataTableColumns<HoldingRow> = []
  if (props.table.state.viewMode === 'merged' && hasExpandableRows.value)
    result.push({
      type: 'expand',
      width: 36,
      expandable: (row) => row.accounts.length > 1,
      renderExpand: (row) =>
        h(
          'div',
          { class: 'account-breakdown', 'data-testid': 'holding-accounts' },
          row.accounts.map((holding) =>
            h('div', { class: 'account-line', key: holding.id }, [
              h(
                'span',
                { class: 'account-name' },
                props.table.accountLabel(holding.broker_account_id)
              ),
              h('span', { class: 'num' }, `${formatQuantity(holding.quantity)} 股`),
              h('span', { class: 'num muted' }, `均价 ${formatPrice(holding.avg_cost)}`),
              h(
                'span',
                { class: 'num muted' },
                `成本 ${formatCurrency(holding.total_cost, holding.currency)}`
              ),
              h(
                NButton,
                {
                  text: true,
                  type: 'primary',
                  size: 'small',
                  onClick: () => emit('transfer', holding)
                },
                { default: () => '转仓' }
              )
            ])
          )
        )
    })
  result.push(
    { key: 'security', title: '标的 / 账户', width: 220, minWidth: 220, render: cell('security') },
    { key: 'quantity', title: '持仓 / 均价', width: 140, align: 'right', render: cell('quantity') },
    { key: 'price', title: '现价 / 行情日', width: 170, align: 'right', render: cell('price') },
    {
      key: 'marketValue',
      className: 'sort-marketValue',
      title: () => sortTitle('市值 / 占比', 'marketValue'),
      width: 185,
      align: 'right',
      render: cell('value')
    },
    {
      key: 'profit',
      className: 'sort-profit',
      title: () => sortTitle('浮动盈亏', 'profit'),
      width: 185,
      align: 'right',
      render: cell('profit')
    },
    { key: 'analysis', title: '研究状态', width: 160, render: cell('analysis') }
  )
  if (props.table.state.viewMode === 'account')
    result.push({
      key: 'transfer',
      title: '操作',
      width: 72,
      fixed: 'right',
      render: (row) =>
        h(
          NButton,
          {
            text: true,
            type: 'primary',
            size: 'small',
            onClick: () => emit('transfer', row.accounts[0])
          },
          { default: () => '转仓' }
        )
    })
  return result
})
function rowProps(row: HoldingRow) {
  return {
    'data-testid': 'holding-row',
    class: props.table.isFocused(row) ? 'holding-focus-row' : undefined
  }
}
// 库默认展开触发器是 span；只在本表补原生按钮，保留库的展开内容与状态。
const renderExpandIcon: NonNullable<DataTableProps['renderExpandIcon']> = ({
  expanded: isExpanded,
  rowData
}) => {
  const key = String(rowData.key)
  return h(
    'button',
    {
      type: 'button',
      class: 'account-expand-button',
      'aria-label': `${isExpanded ? '收起' : '展开'} ${rowData.name || rowData.symbol} 的账户明细`,
      'aria-expanded': isExpanded,
      onClick: (event: MouseEvent) => {
        event.stopPropagation()
        expanded.value = isExpanded
          ? expanded.value.filter((item) => item !== key)
          : [...expanded.value, key]
      }
    },
    isExpanded ? '⌄' : '›'
  )
}
// Naive 当前没有表头 attrs API；局部补充 aria-sort，与可聚焦排序按钮同源。
function syncSortSemantics() {
  for (const prop of ['marketValue', 'profit']) {
    const order = props.table.state.sort.prop === prop ? props.table.state.sort.order : null
    rootEl.value?.querySelector(`th.sort-${prop}`)?.setAttribute('aria-sort', order || 'none')
  }
}
onMounted(syncSortSemantics)
watch(
  () => [
    props.table.state.sort.prop,
    props.table.state.sort.order,
    props.table.state.viewMode,
    isMobileView.value
  ],
  async () => {
    await nextTick()
    syncSortSemantics()
  },
  { flush: 'post' }
)
let scrolledFocus: object | null = null
watch(
  () => [props.table.state.focus, props.table.rows, isMobileView.value] as const,
  async ([focus]) => {
    if (!focus || focus === scrolledFocus) return
    await nextTick()
    const target = rootEl.value?.querySelector('.holding-focus-row, .mobile-card--focus')
    if (!target) return
    scrolledFocus = focus
    target.scrollIntoView?.({
      block: 'center',
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'
    })
  },
  { flush: 'post' }
)
</script>

<template>
  <div ref="rootEl" class="holdings-list" :aria-busy="table.state.loading">
    <NAlert v-if="table.state.loadError" type="error" title="持仓数据加载失败" class="load-error">
      {{ table.state.loadError
      }}<template v-if="table.rows.length">
        · 下方保留上次读取的持仓，请重试获取最新数据。</template
      >
      <div class="retry-action">
        <NButton size="small" @click="table.loadHoldings({ force: true })">重试加载持仓</NButton>
      </div>
    </NAlert>
    <NSpin :show="table.state.loading" description="正在读取持仓">
      <div v-if="!isMobileView" class="desktop-data-table">
        <NDataTable
          :theme-overrides="{ tdPaddingMedium: '10px 14px' }"
          :columns="columns"
          :data="table.rows"
          :row-key="(row: HoldingRow) => row.key"
          :row-props="rowProps"
          v-model:expanded-row-keys="expanded"
          :scroll-x="scrollX"
          :render-expand-icon="renderExpandIcon"
          class="holdings-table"
          :bordered="true"
          :single-line="true"
          aria-label="当前持仓明细"
        >
          <template #empty
            ><NEmpty
              :description="
                table.state.loadError
                  ? '数据尚未加载，请重试'
                  : table.state.loading
                    ? '正在读取持仓'
                    : table.isFiltered
                      ? '没有符合筛选条件的持仓'
                      : '暂无持仓数据'
              "
          /></template>
        </NDataTable>
      </div>
      <div v-else class="mobile-card-list">
        <article
          v-for="row in table.rows"
          :key="row.key"
          class="mobile-card"
          :class="{ 'mobile-card--focus': table.isFocused(row) }"
          data-testid="holding-card"
        >
          <div class="mobile-card-head">
            <!-- 移动端此前是纯 span：桌面端标题是 el-link 可进标的档案，手机上
             整页没有任何入口，AI 分析/财报摘要在手机上根本打不开 -->
            <router-link
              :to="detailPath(row)"
              class="mobile-card-title mobile-card-title-link"
              data-testid="holding-card-title"
              :aria-label="`查看 ${row.name || row.symbol} 的标的档案`"
            >
              <span class="mobile-card-symbol" data-testid="holding-card-symbol">
                {{ row.symbol }}
                <ArrowRight class="mobile-card-title-chevron" />
              </span>
              <span v-if="row.name" class="mobile-card-name">{{ row.name }}</span>
            </router-link>
            <div class="mobile-card-tags">
              <!-- 第一个 tag 必须是市场（移动端 E2E 据此拼标的档案路由） -->
              <NTag size="small" :bordered="true">{{ row.market }}</NTag>
              <NTag
                v-if="badges.industryFor(row)"
                size="small"
                type="default"
                :title="industryTooltip(badges.industryFor(row)!.source)"
                data-testid="holding-card-industry"
                >{{ badges.industryFor(row)!.industry }}</NTag
              >
              <NTag v-if="badges.upcomingEvent(row)" type="warning" size="small" :bordered="true">
                {{ badges.upcomingEvent(row)!.label }}·{{ badges.upcomingEvent(row)!.daysText }}
              </NTag>
              <NTag
                v-if="badges.announcementBadge(row)"
                type="error"
                size="small"
                :bordered="true"
                data-testid="holding-card-announcement"
              >
                {{ badges.announcementBadge(row)!.text }}
              </NTag>
              <AnalysisBadges :badges="badges" :row="row" risk-test-id="ai-tags" />
            </div>
          </div>

          <div class="mobile-card-metrics">
            <div>
              <span class="mobile-metric-label">市值</span>
              <strong v-if="table.marketValueOf(row) !== null">
                {{ formatCurrency(table.marketValueOf(row), row.currency) }}
              </strong>
              <strong v-else>—</strong>
              <small
                v-if="row.currency !== 'CNY' && table.marketValueOf(row) !== null"
                class="approx"
              >
                ≈{{ formatCurrency(table.marketValueCNYOf(row)) }}
              </small>
            </div>
            <div>
              <span class="mobile-metric-label">浮动盈亏</span>
              <strong
                v-if="table.profitOf(row) !== null"
                :style="{ color: table.getProfitColor(row) }"
              >
                {{ formatCurrency(table.profitOf(row), row.currency) }}
                <small v-if="table.profitRateOf(row) !== null">
                  {{ formatPercent(table.profitRateOf(row)) }}
                </small>
              </strong>
              <strong v-else>—</strong>
              <small
                v-if="row.currency !== 'CNY' && table.profitOf(row) !== null"
                class="approx"
                :style="{ color: table.getProfitColor(row) }"
              >
                ≈{{ formatCurrency(table.profitCNYOf(row)) }}
              </small>
            </div>
          </div>

          <div class="mobile-card-meta">
            <span>数量 {{ formatQuantity(row.quantity) }}</span>
            <span>均价 {{ formatPrice(row.avgCost) }}</span>
            <span v-if="table.weightOf(row) !== null"
              >占比 {{ formatNumber(table.weightOf(row), 1) }}%</span
            >
            <NTag
              v-if="row.unknownCost > 0"
              type="warning"
              size="small"
              :bordered="true"
              class="unknown-cost-tag"
            >
              成本未知 {{ formatQuantity(row.unknownCost) }}
            </NTag>
          </div>

          <div class="mobile-price-row">
            <span>现价 {{ row.currency }}</span>
            <div class="mobile-price-value">
              <PriceEditor :table="table" :row="row" />
              <span v-if="!table.isEditingPrice(row)" class="mobile-price-date">
                <PriceFlags :info="table.priceInfoOf(row)" />
                {{ table.priceInfoOf(row)?.label }}
              </span>
            </div>
          </div>

          <div class="mobile-card-accounts">
            <div v-for="holding in row.accounts" :key="holding.id" class="mobile-account-line">
              <span :class="{ 'account-unassigned': !holding.broker_account_id }">
                {{ table.accountLabel(holding.broker_account_id) }} ·
                {{ formatQuantity(toNumber(holding.quantity)) }}
              </span>
              <NButton type="primary" size="small" text @click="$emit('transfer', holding)">
                转仓到其他账户
              </NButton>
            </div>
          </div>
        </article>
        <NEmpty
          v-if="!table.state.loading && table.rows.length === 0"
          :description="
            table.state.loadError
              ? '数据尚未加载，请重试'
              : table.isFiltered
                ? '没有符合筛选条件的持仓'
                : '暂无持仓数据'
          "
        />
      </div>
    </NSpin>
  </div>
</template>

<style scoped>
.holdings-list {
  min-width: 0;
}
.load-error {
  margin-bottom: 16px;
}
.desktop-data-table {
  min-width: 0;
}
.holdings-table {
  font-size: 13px;
  border-radius: 6px;
  overflow: hidden;
}
.holdings-table :deep(.holding-focus-row td) {
  background: var(--app-primary-tint);
}
.holdings-table :deep(.table-sort-button) {
  border: 0;
  background: transparent;
  font: inherit;
  color: inherit;
  padding: 2px 0;
  cursor: pointer;
  white-space: nowrap;
}
.holdings-table :deep(.account-expand-button) {
  border: 0;
  background: none;
  color: var(--app-text-muted);
  cursor: pointer;
  width: 24px;
  height: 28px;
  font-size: 20px;
  padding: 0;
}
.retry-action {
  margin-top: 12px;
}
.holdings-table :deep(.account-breakdown) {
  padding: 8px 18px 8px 36px;
  background: var(--app-surface-muted);
}
.holdings-table :deep(.account-line) {
  display: grid;
  grid-template-columns: minmax(180px, 1.2fr) repeat(3, minmax(90px, 1fr)) auto;
  align-items: center;
  gap: 12px;
  padding: 8px 0;
}
.holdings-table :deep(.account-line + .account-line) {
  border-top: 1px dashed var(--app-border-soft);
}
.holdings-table :deep(.num) {
  font-variant-numeric: tabular-nums;
}
.holdings-table :deep(.muted) {
  color: var(--app-text-soft);
}
.mobile-card--focus {
  outline: 2px solid var(--app-primary);
  outline-offset: 2px;
}
.mobile-card-title-link {
  text-decoration: none;
}
.mobile-card-title-chevron {
  width: 14px;
  height: 14px;
}
@media (max-width: 640px) {
  .mobile-card {
    min-width: 0;
    padding: 18px 0;
    border: 0;
    border-bottom: 1px solid var(--app-border);
    border-radius: 0;
    background: transparent;
  }
  .mobile-card-list {
    grid-template-columns: minmax(0, 1fr);
    gap: 0;
  }
  .mobile-card-name {
    white-space: normal;
    overflow-wrap: anywhere;
    overflow: visible;
    text-overflow: clip;
  }
  .mobile-card-tags :deep(.n-tag) {
    max-width: 100%;
    min-height: var(--n-height);
    height: auto;
    white-space: normal;
  }
  .mobile-card-tags :deep(.n-tag__content) {
    min-width: 0;
    line-height: 1.5;
    overflow-wrap: anywhere;
  }
  .mobile-price-row {
    display: grid;
    grid-template-columns: 88px minmax(0, 1fr);
    align-items: center;
    gap: 10px;
    margin-top: 12px;
    color: var(--app-text-muted);
    font-size: 13px;
  }
  .mobile-price-value {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px 10px;
  }
  .mobile-price-value > .price-editor {
    flex: 1;
  }
  .mobile-price-date {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px;
    font-size: 12px;
    color: var(--app-text-soft);
  }
  .approx {
    display: block;
    margin-top: 2px;
    font-size: 12px;
    font-weight: 400;
    color: var(--app-text-soft);
  }
  .mobile-card-accounts {
    margin-top: 10px;
    font-size: 13px;
    color: var(--app-text-muted);
  }
  .mobile-account-line {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .mobile-account-line :deep(.n-button) {
    min-height: 44px;
  }
  .mobile-account-line > span {
    min-width: 0;
    max-width: 100%;
    overflow-wrap: anywhere;
  }
}
</style>
