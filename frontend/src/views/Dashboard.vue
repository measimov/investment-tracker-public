<template>
  <div class="dashboard" :aria-busy="loading">
    <header class="dashboard-heading">
      <div class="dashboard-title">
        <h1 class="page-title">仪表盘</h1>
        <NPopover
          trigger="click"
          :show="activeHelp === 'methodology'"
          @update:show="setHelp('methodology', $event)"
          placement="bottom-start"
          :width="isMobile ? 220 : 360"
          :style="popoverStyle"
        >
          <template #trigger>
            <button
              class="help-button"
              @keydown.esc.stop.prevent="activeHelp = null"
              type="button"
              aria-label="查看仪表盘口径说明"
              aria-describedby="dashboard-methodology"
            >
              <InfoFilled />
            </button>
          </template>
          {{ dashboardMethodology }}
        </NPopover>
        <span id="dashboard-methodology" class="accessible-description">{{
          dashboardMethodology
        }}</span>
      </div>
      <div class="dashboard-actions">
        <router-link class="dashboard-link" to="/statistics">查看统计明细</router-link>
        <NButton quaternary :loading="loading" @click="loadData()">重新加载</NButton>
      </div>
    </header>
    <NAlert
      v-if="loadError"
      type="error"
      class="load-error"
      title="仪表盘加载失败"
      data-testid="dashboard-error"
    >
      {{ hasSnapshot ? '以下保留上次成功加载的数据。' : '汇总暂不可用，未知金额显示为 —。' }}
      <NButton text @click="loadData()">重试</NButton>
    </NAlert>
    <NSpin :show="loading && hasSnapshot">
      <PriceIssuesAlert
        :freshness="snapshot?.prices?.freshness"
        :refreshing="refreshingPrices"
        @refresh="refreshPricesAndReload"
      />
      <NAlert
        v-for="(warning, index) in warnings"
        :key="index"
        type="warning"
        class="quality-alert"
        >{{ warning }}</NAlert
      >
      <NAlert
        v-if="periodLoadError"
        type="warning"
        class="quality-alert"
        title="期间损益暂不可用"
        data-testid="dashboard-period-error"
      >
        当日、本月与本年损益未完成加载。<NButton text @click="loadData()">重试</NButton>
      </NAlert>
      <section
        class="portfolio-overview"
        :class="{ 'with-fallback': !snapshot?.performance?.receivable_return }"
        aria-label="账户表现"
      >
        <div class="overview-primary">
          <div class="metric-label">已知持仓市值 <span class="currency-unit">CNY</span></div>
          <NSkeleton v-if="initialLoading" height="44px" width="85%" />
          <div v-else class="hero-value" data-testid="dashboard-market-value">
            <span class="dashboard-number">{{ formatCurrency(performance.market_value) }}</span>
          </div>
          <div class="metric-caption usd-value">
            {{ initialLoading ? '正在读取…' : formatCurrency(performance.market_value_usd, 'USD') }}
          </div>
        </div>
        <div class="overview-metric daily-metric">
          <template v-if="periodPnl">
            <div class="metric-label period-label">
              当日实收损益
              <NPopover
                trigger="click"
                :show="activeHelp === 'daily'"
                @update:show="setHelp('daily', $event)"
                placement="bottom-end"
                :width="isMobile ? 220 : 360"
                :style="popoverStyle"
              >
                <template #trigger
                  ><button
                    class="help-button"
                    @keydown.esc.stop.prevent="activeHelp = null"
                    type="button"
                    :aria-label="`查看${periodPnl.periods.daily.label}损益口径`"
                  >
                    <InfoFilled /></button
                ></template>
                {{ periodTooltip('daily') }}
              </NPopover>
            </div>
            <div
              class="period-value metric-value"
              :style="{
                color: profitColor(
                  periodPnl.periods.daily.status === 'unavailable'
                    ? null
                    : periodPnl.periods.daily.pnl_cny
                )
              }"
            >
              <span class="dashboard-number" data-testid="period-pnl-daily">{{
                periodPnl.periods.daily.status === 'unavailable'
                  ? '无法计算'
                  : formatCurrency(periodPnl.periods.daily.pnl_cny)
              }}</span>
              <NTag
                v-if="periodIsEstimated(periodPnl.periods.daily)"
                type="warning"
                size="small"
                :bordered="false"
                >估算</NTag
              >
            </div>
            <div class="metric-caption">
              收益率（实验）<span class="dashboard-number">{{
                formatPercent(periodPnl.periods.daily.return_rate)
              }}</span
              ><template v-if="periodPnl.periods.daily.dividend_income_cny">
                · 含股息
                <span class="dashboard-number">{{
                  formatCurrency(periodPnl.periods.daily.dividend_income_cny)
                }}</span></template
              >
            </div>
          </template>
          <template v-else>
            <div class="metric-label">当日实收损益</div>
            <NSkeleton v-if="initialLoading" height="30px" />
            <div v-else class="metric-value" data-testid="period-pnl-daily">—</div>
          </template>
        </div>
        <section
          v-if="periodPnl"
          class="period-pnl-row"
          data-testid="period-pnl-row"
          aria-label="期间损益"
        >
          <article v-for="key in PERIOD_KEYS" :key="key" class="period-card">
            <div class="metric-label period-label">
              {{ periodPnl.periods[key].label }}损益（实收）
              <NPopover
                trigger="click"
                :show="activeHelp === key"
                @update:show="setHelp(key, $event)"
                placement="bottom-end"
                :width="isMobile ? 220 : 360"
                :style="popoverStyle"
              >
                <template #trigger
                  ><button
                    class="help-button"
                    @keydown.esc.stop.prevent="activeHelp = null"
                    type="button"
                    :aria-label="`查看${periodPnl.periods[key].label}损益口径`"
                  >
                    <InfoFilled /></button
                ></template>
                {{ periodTooltip(key) }}
              </NPopover>
            </div>
            <div
              class="period-value"
              :style="{
                color: profitColor(
                  periodPnl.periods[key].status === 'unavailable'
                    ? null
                    : periodPnl.periods[key].pnl_cny
                )
              }"
            >
              <span class="dashboard-number" :data-testid="`period-pnl-${key}`">{{
                periodPnl.periods[key].status === 'unavailable'
                  ? '无法计算'
                  : formatCurrency(periodPnl.periods[key].pnl_cny)
              }}</span>
              <NTag
                v-if="periodIsEstimated(periodPnl.periods[key])"
                type="warning"
                size="small"
                :bordered="false"
                >估算</NTag
              >
            </div>
            <div class="metric-caption">
              收益率（实验）<span class="dashboard-number">{{
                formatPercent(periodPnl.periods[key].return_rate)
              }}</span
              ><template v-if="periodPnl.periods[key].dividend_income_cny">
                · 含股息
                <span class="dashboard-number">{{
                  formatCurrency(periodPnl.periods[key].dividend_income_cny)
                }}</span></template
              >
            </div>
            <PeriodReceivablePnl
              v-if="periodPnl.periods[key].receivable_pnl"
              compact
              :summary="periodPnl.periods[key].receivable_pnl!"
              :period-key="key"
            />
          </article>
        </section>
        <div class="overview-secondary">
          <div class="overview-return">
            <ReceivableReturnCard
              embedded
              compact
              :summary="snapshot?.performance?.receivable_return"
              :legacy-unreviewed-count="
                snapshot?.performance?.dividend_summary?.legacy_unreviewed_count
              "
              :return-rate="performance.total_return_rate ?? null"
              :annualized-rate="performance.annualized_rate"
            />
            <div v-if="!snapshot?.performance?.receivable_return" class="overview-metric">
              <div class="metric-label">总收益（权益仓）</div>
              <NSkeleton v-if="initialLoading" height="30px" />
              <div
                v-else
                class="metric-value"
                :style="{ color: profitColor(performance.total_return) }"
              >
                <span class="dashboard-number">{{ formatCurrency(performance.total_return) }}</span>
              </div>
              <div class="metric-caption">
                收益率
                <span class="dashboard-number">{{
                  formatPercent(performance.total_return_rate)
                }}</span
                ><template v-if="performance.annualized_rate !== null">
                  · 年化（XIRR）
                  <span class="dashboard-number">{{
                    formatPercent(performance.annualized_rate)
                  }}</span></template
                >
              </div>
            </div>
          </div>
          <div class="overview-composition" aria-label="FIFO 收益组成">
            <div class="composition-grid">
              <div class="composition-metric">
                <div class="metric-label">
                  未实现盈亏 <span>FIFO</span>
                  <NPopover
                    trigger="click"
                    :show="activeHelp === 'fifo'"
                    @update:show="setHelp('fifo', $event)"
                    placement="bottom-end"
                    :width="isMobile ? 220 : 360"
                    :style="popoverStyle"
                  >
                    <template #trigger
                      ><button
                        class="help-button"
                        @keydown.esc.stop.prevent="activeHelp = null"
                        type="button"
                        aria-label="查看仪表盘FIFO盈亏口径"
                      >
                        <InfoFilled /></button
                    ></template>
                    按 FIFO
                    剩余批次成本计算（与已实现盈亏同一套批次）。持仓页的「浮动盈亏」按摊薄平均成本，部分卖出过的标的两者会有差异；两种口径下已实现
                    + 未实现的总收益一致，只是拆分归属不同。
                  </NPopover>
                </div>
                <NSkeleton v-if="initialLoading" height="30px" />
                <div
                  v-else
                  class="metric-value"
                  :style="{ color: profitColor(performance.unrealized_pnl) }"
                >
                  <span class="dashboard-number">{{
                    formatCurrency(performance.unrealized_pnl)
                  }}</span>
                </div>
                <div class="metric-caption">
                  未实现盈亏率
                  <span class="dashboard-number">{{
                    formatPercent(performance.unrealized_rate)
                  }}</span>
                </div>
              </div>
              <div class="composition-metric">
                <div class="metric-label">已实现收益（含股息）</div>
                <NSkeleton v-if="initialLoading" height="30px" />
                <div
                  v-else
                  class="metric-value"
                  :style="{ color: profitColor(performance.realized_return) }"
                >
                  <span class="dashboard-number">{{
                    formatCurrency(performance.realized_return)
                  }}</span>
                </div>
                <div class="metric-caption">
                  含税后股息
                  <span class="dashboard-number">{{
                    formatCurrency(performance.net_dividends)
                  }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>
      <div class="activity-grid">
        <section class="activity-panel" aria-label="市场分布">
          <div class="section-heading">
            <h2>市场分布（按成本）</h2>
            <NPopover
              trigger="click"
              :show="activeHelp === 'market'"
              @update:show="setHelp('market', $event)"
              placement="bottom-end"
              :width="isMobile ? 220 : 360"
              :style="popoverStyle"
            >
              <template #trigger
                ><button
                  class="help-button"
                  @keydown.esc.stop.prevent="activeHelp = null"
                  type="button"
                  aria-label="查看市场成本口径"
                  aria-describedby="market-cost-summary"
                >
                  <InfoFilled /></button
              ></template>
              市场分布按持仓成本（非市值），以下金额为人民币；缺少汇率时仅含可折算部分。
            </NPopover>
          </div>
          <NSkeleton
            v-if="initialLoading"
            circle
            width="180px"
            height="180px"
            class="chart-skeleton"
          />
          <NEmpty v-else-if="loadError && !hasSnapshot" description="市场分布暂不可用" />
          <NEmpty v-else-if="marketStats.length === 0" description="暂无市场分布数据" />
          <div
            v-else
            class="market-distribution"
            role="img"
            aria-label="按市场分布的持仓成本"
            aria-describedby="market-cost-summary"
          >
            <MarketPieChart :option="marketChartOption" class="dashboard-chart" />
            <p id="market-cost-summary" class="accessible-description">{{ marketCostSummary }}</p>
            <ul class="market-cost-summary">
              <li v-for="item in marketDistribution" :key="item.market">
                <span class="market-name">
                  <span
                    class="market-swatch"
                    :style="{ background: item.color }"
                    aria-hidden="true"
                  />
                  {{ item.market }}
                </span>
                <span class="market-share">{{ item.share }}</span>
                <strong
                  ><span class="dashboard-number">{{
                    formatCurrency(item.total_cost)
                  }}</span></strong
                >
                <span v-if="item.missing_rate_currencies?.length" class="market-cost-warning"
                  >缺少 {{ item.missing_rate_currencies.join('/') }} 汇率，仅含可折算部分</span
                >
              </li>
            </ul>
          </div>
        </section>
        <section class="activity-panel" aria-label="最近交易">
          <div class="section-heading">
            <h2>最近交易</h2>
            <router-link class="dashboard-link" to="/transactions">查看全部</router-link>
          </div>
          <NSkeleton v-if="initialLoading" text :repeat="5" />
          <NEmpty v-else-if="loadError && !hasSnapshot" description="最近交易暂不可用" />
          <NDataTable
            v-else
            :columns="transactionColumns"
            :data="recentTransactions"
            :scroll-x="464"
            :bordered="false"
            class="recent-transactions"
          >
            <template #empty><NEmpty description="暂无最近交易" /></template>
          </NDataTable>
        </section>
      </div>
      <section v-if="accounts.length" class="reconciliation-strip" aria-label="账户对账状态">
        <div class="section-heading">
          <h2>账户对账状态</h2>
          <div class="section-links">
            <router-link class="dashboard-link" to="/reports">AI 复盘</router-link
            ><router-link class="dashboard-link" to="/account-data">账户数据</router-link>
          </div>
        </div>
        <div class="account-badges">
          <div v-for="account in accounts" :key="account.id" class="account-badge">
            <span class="account-name">{{ accountShortName(account) }}</span
            ><NTag
              :type="reconciliationTag(account.latest_reconciliation)"
              size="small"
              :bordered="false"
              >{{ reconciliationLabel(account.latest_reconciliation) }}</NTag
            >
          </div>
        </div>
      </section>
    </NSpin>
  </div>
</template>

<script setup lang="ts">
import PriceIssuesAlert from '@/components/PriceIssuesAlert.vue'
import ReceivableReturnCard from '@/components/ReceivableReturnCard.vue'
import PeriodReceivablePnl from '@/components/PeriodReceivablePnl.vue'
import type { PortfolioSnapshot, RecentTransaction, ReconciliationBadge } from '@/types'
import {
  NAlert,
  NButton,
  NDataTable,
  NEmpty,
  NPopover,
  NSkeleton,
  NSpin,
  NTag,
  type DataTableColumns
} from 'naive-ui'
import { RouterLink } from 'vue-router'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { useAliveGuard } from '@/composables/useAliveGuard'
import { useRefreshPrices } from '@/composables/useRefreshPrices'
import { isPriceIssueWarning } from '@/utils/priceIssues'
import { showApiError } from '@/utils/showApiError'
import { accountShortName, transactionTypeLabel, transactionTypeTag } from '@/utils/labels'
import { ref, onMounted, computed, defineAsyncComponent, h } from 'vue'
import { CircleHelp as InfoFilled } from '@lucide/vue'
import api from '../api'
import { useAutoReload } from '../composables/useAutoReload'
import type { PeriodPnlKey, PeriodPnlResponse } from '../types'
import {
  profitColor,
  formatPercent,
  formatDate,
  formatCurrency,
  formatNumber,
  formatQuantity,
  formatPrice
} from '../utils/helpers'
import { holdingsLink } from '../utils/securities'
import { mergeDashboardWarnings, periodIsEstimated } from './dashboard/helpers'
import { CHART_FONT_FAMILY } from '@/styles/tokens'
import { useChartColors } from '@/styles/chartTheme'
const chartColors = useChartColors()

const MarketPieChart = defineAsyncComponent(() => import('./dashboard/MarketPieChart.vue'))

const snapshot = ref<PortfolioSnapshot | null>(null)
// 区间损益单独请求：失败不影响看板主体，保留明确失败与重试入口。
const periodPnl = ref<PeriodPnlResponse | null>(null)
const PERIOD_KEYS = ['mtd', 'ytd'] as const
const dashboardMethodology =
  '账户表现与近期活动，按既有权益仓口径汇总。总收益与年化（XIRR）为权益仓口径（仅证券投入，闲置现金与外部出入金不计入、不稀释收益率）；市场分布按持仓成本（非市值）；详情见统计分析页。'

function periodTooltip(key: PeriodPnlKey): string {
  const period = periodPnl.value?.periods[key]
  if (!period) return ''
  let text =
    `${formatDate(period.start_date)} 至 ${formatDate(period.end_date)}：期末市值 + 卖出与分红流出 − 期初市值 − 买入流入。` +
    '期初按区间起点前最近收盘价估值；股息按到账日计入（实收口径），除息至到账期间可能暂时拉低收益；权益仓口径（不含闲置现金与出入金），收益率为时间加权（实验）。'
  if (period.status === 'unavailable') {
    const names = period.opening_unpriced_positions.map((p) => p.symbol).join('、')
    text += `无法计算：${names} 在区间起点前没有任何价格，期初市值无从估值。`
    return text
  }
  if ((period.estimated_inflow_events ?? 0) > 0) {
    text +=
      `估算：区间内有 ${period.estimated_inflow_events} 笔成本未知的实物转入，` +
      '按估值价补记为投入，损益随估值价浮动。'
  }
  if (period.status === 'estimated' && period.stale_opening_basis.length > 0) {
    const bases = period.stale_opening_basis
      .map(
        (p) =>
          `${p.symbol}（${formatDate(p.basis_date)}${p.basis_source === 'transaction' ? '成交价' : '收盘'}）`
      )
      .join('、')
    text += `估算：以下持仓的期初价早于区间起点，损益含此前累积涨跌：${bases}。`
  }
  const staleClosing = period.stale_closing_prices ?? []
  if (staleClosing.length > 0) {
    const names = staleClosing
      .map(
        (p) => `${p.symbol}（现价 ${formatDate(p.price_date)}，期初 ${formatDate(p.basis_date)}）`
      )
      .join('、')
    text += `估算：以下持仓的估值价早于期初基准，期末按旧价计：${names}。`
  }
  return text
}
const activeHelp = ref<string | null>(null)
function setHelp(name: string, show: boolean) {
  if (show) activeHelp.value = name
  else if (activeHelp.value === name) activeHelp.value = null
}
const isMobile = useMediaQuery('(max-width: 640px)')
const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)')
const popoverStyle = {
  maxWidth: 'calc(100vw - 32px)',
  maxHeight: 'calc(100vh - 32px)',
  overflowY: 'auto' as const
}
const loadError = ref(false)
const periodLoadError = ref(false)
const hasSnapshot = computed(() => snapshot.value !== null)
const loading = ref(false)
const hasLoaded = ref(false)

const initialLoading = computed(() => loading.value && !hasLoaded.value)

const performance = computed(() => {
  const perf = snapshot.value?.performance
  if (!perf) {
    // 加载失败时是「不知道」而不是「0」：卡片显示 —（#284，此前显示 ¥0.00 / +0.00%）
    return {
      market_value: null,
      market_value_usd: null,
      total_return: null,
      total_return_rate: null,
      annualized_rate: null,
      unrealized_pnl: null,
      unrealized_rate: null,
      realized_return: null,
      net_dividends: null
    }
  }
  return {
    market_value: perf.current_performance.current_market_value_cny,
    market_value_usd: perf.current_performance.current_market_value_usd,
    total_return: perf.account_return.total_return_cny,
    total_return_rate: perf.account_return.total_return_rate,
    annualized_rate: perf.account_return.annualized_return_rate ?? null,
    unrealized_pnl: perf.current_performance.unrealized_pnl_cny,
    unrealized_rate: perf.current_performance.unrealized_pnl_rate,
    realized_return: perf.total_realized_return.total_realized_return_cny,
    net_dividends: perf.total_realized_return.net_dividend_income_cny
  }
})

const marketStats = computed(() => snapshot.value?.markets || [])
const marketDistribution = computed(() => {
  const total = marketStats.value.reduce((sum, item) => sum + item.total_cost, 0)
  return marketStats.value.map((item, index) => ({
    ...item,
    color: chartColors.value.marketPalette[index % chartColors.value.marketPalette.length],
    share: total > 0 ? `${formatNumber((item.total_cost / total) * 100, 1)}%` : '—'
  }))
})
const marketCostSummary = computed(
  () =>
    '持仓成本（CNY）：' +
    marketStats.value
      .map(
        (item) =>
          `${item.market} ${formatCurrency(item.total_cost)}${item.missing_rate_currencies?.length ? `（缺少 ${item.missing_rate_currencies.join('/')} 汇率，仅含可折算部分）` : ''}`
      )
      .join(' · ')
)
const recentTransactions = computed(() => snapshot.value?.recent_transactions || [])
const accounts = computed(() => snapshot.value?.accounts || [])
const transactionColumns: DataTableColumns<RecentTransaction> = [
  {
    title: '日期',
    key: 'transaction_date',
    width: 92,
    className: 'txn-date-column',
    render: (row) => formatDate(row.transaction_date)
  },
  {
    title: '标的',
    key: 'symbol',
    minWidth: 140,
    render: (row) =>
      h(
        RouterLink,
        { to: holdingsLink(row), class: 'txn-link', 'data-testid': 'recent-txn-symbol-link' },
        {
          default: () => [
            h('span', { class: 'txn-name' }, row.name || row.symbol),
            h('span', { class: 'txn-sub' }, [
              h('span', { class: 'dashboard-number' }, row.symbol),
              ` · ${row.market}`
            ])
          ]
        }
      )
  },
  {
    title: '类型',
    key: 'transaction_type',
    width: 54,
    render: (row) => {
      const type = transactionTypeTag(row.transaction_type)
      return h(
        NTag,
        {
          type: type === 'danger' ? 'error' : type === 'info' ? 'default' : type,
          size: 'small',
          bordered: false
        },
        { default: () => transactionTypeLabel(row.transaction_type) }
      )
    }
  },
  {
    title: '数量',
    key: 'quantity',
    className: 'txn-number-column',
    align: 'right',
    width: 74,
    render: (row) => formatQuantity(row.quantity)
  },
  {
    title: '价格 / 币种',
    key: 'price',
    className: 'txn-number-column',
    align: 'right',
    width: 104,
    render: (row) => `${formatPrice(row.price)} ${row.currency}`
  }
]

// 两个端点都会报缺价，同一批标的只留一条（去重规则见 dashboard/helpers.ts）
const warnings = computed(() =>
  mergeDashboardWarnings({
    snapshotWarnings: snapshot.value?.data_quality?.warnings,
    snapshotMissingKeys: snapshot.value?.prices?.missing_keys,
    periodWarnings: periodPnl.value?.data_quality?.warnings,
    periodUnpriced: periodPnl.value?.periods?.daily?.unpriced_positions
  }).filter((text) => !isPriceIssueWarning(text))
)

const reconciliationLabel = (latest: ReconciliationBadge | null | undefined) => {
  if (!latest) return '未对账'
  // 聚合语义：最新快照日全部 scope 的最差状态；全为分范围快照时绿色仅代表持仓一致
  if (latest.status === 'MATCHED') return latest.all_scoped ? '持仓一致' : '比对一致'
  if (latest.status === 'MISMATCHED') return '有差异'
  return '待比对'
}
const reconciliationTag = (latest: ReconciliationBadge | null | undefined) => {
  if (!latest) return 'default'
  if (latest.status === 'MATCHED') return 'success'
  if (latest.status === 'MISMATCHED') return 'error'
  return 'warning'
}

const marketChartOption = computed(() => ({
  animation: !reducedMotion.value,
  color: chartColors.value.marketPalette,
  textStyle: { fontFamily: CHART_FONT_FAMILY, color: chartColors.value.text },
  tooltip: {
    backgroundColor: chartColors.value.surface,
    borderColor: chartColors.value.border,
    textStyle: { color: chartColors.value.text },
    trigger: 'item',
    formatter: (params: { name: string; value: number; percent: number }) => {
      const share = marketDistribution.value.find((item) => item.market === params.name)?.share
      return `${params.name}: ${formatCurrency(params.value)} (${share ?? `${formatNumber(params.percent, 1)}%`})`
    }
  },
  legend: {
    show: false
  },
  series: [
    {
      type: 'pie',
      percentPrecision: 1,
      startAngle: 0,
      radius: isMobile.value ? ['46%', '70%'] : ['56%', '84%'],
      center: ['50%', '50%'],
      stillShowZeroSum: false,
      data: marketStats.value.map((item) => ({
        name: item.market,
        value: item.total_cost
      })),
      itemStyle: { borderRadius: 0, borderColor: chartColors.value.surface, borderWidth: 2 },
      labelLine: { show: true, length: 8, length2: 8 },
      label: {
        show: true,
        position: 'outside',
        alignTo: 'none',
        bleedMargin: 6,
        distanceToLabelLine: 3,
        formatter: (params: { name: string; percent: number }) => {
          const share = marketDistribution.value.find((item) => item.market === params.name)?.share
          const value = `{value|${share ?? `${formatNumber(params.percent, 1)}%`}}`
          return isMobile.value ? value : `{name|${params.name}}\n${value}`
        },
        rich: {
          name: {
            fontSize: 12,
            lineHeight: 18,
            color: chartColors.value.text
          },
          value: {
            fontSize: 14,
            lineHeight: 18,
            color: chartColors.value.text
          }
        }
      },
      emphasis: {
        scale: false
      }
    }
  ]
}))

// 陈价提示里的「刷新价格」：提交刷新 job、轮询到终态后重读看板（与统计页同一编排）
const { isUnmounted } = useAliveGuard()
const { refreshPrices, notifyRefreshResult } = useRefreshPrices(isUnmounted)
const refreshingPrices = ref(false)
async function refreshPricesAndReload() {
  refreshingPrices.value = true
  try {
    const result = await refreshPrices()
    if (!result || isUnmounted()) return
    await loadData()
    notifyRefreshResult(result)
  } catch (error) {
    if (!isUnmounted()) showApiError(error, { prefix: '刷新价格失败' })
  } finally {
    if (!isUnmounted()) refreshingPrices.value = false
  }
}

async function loadData(options: { silent?: boolean } = {}) {
  // silent：自动重读——不转圈、不弹错（失败保留当前数据，下一轮再试）
  if (!options.silent) loading.value = true
  try {
    const [response, periodResponse] = await Promise.all([
      api.getPortfolioSnapshot(),
      api.getPeriodPnl().catch(() => null)
    ])
    loadError.value = false
    snapshot.value = response.data
    periodPnl.value = periodResponse?.data ?? null
    periodLoadError.value = periodResponse === null
  } catch (error) {
    if (options.silent) throw error
    loadError.value = true
    showApiError(error, '加载仪表盘失败')
  } finally {
    if (!options.silent) {
      loading.value = false
      hasLoaded.value = true
    }
  }
}

// 报价由后端交易时段每 15 分钟刷新；页面可见时每 5 分钟静默重读一次（只读库）
useAutoReload(() => loadData({ silent: true }), { paused: () => loading.value })

onMounted(() => {
  loadData()
})
</script>

<style scoped>
.dashboard {
  width: 100%;
  padding: 4px 0 16px;
}
.dashboard-heading,
.dashboard-title {
  display: flex;
  align-items: center;
  gap: 12px;
}
.dashboard-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}
.dashboard-heading {
  justify-content: space-between;
  margin-bottom: 12px;
}
.accessible-description {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}
.portfolio-overview {
  padding: 12px 16px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius);
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  align-items: start;
  gap: 12px 16px;
  margin: 12px 0;
  font-family: var(--app-font-sans);
}
.overview-primary,
.overview-metric,
.overview-return,
.overview-composition,
.period-card {
  min-width: 0;
}
.overview-metric {
  border-left: 1px solid var(--app-border);
  padding-left: 16px;
}
.overview-secondary {
  grid-column: 1 / -1;
  border-top: 1px solid var(--app-border);
  padding-top: 10px;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
  min-width: 0;
}
.overview-return .overview-metric {
  padding-left: 0;
  border-left: 0;
}
.section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 6px;
}
h2 {
  font-size: 16px;
  font-weight: 500;
  margin: 0;
}
.dashboard-actions a,
.section-heading a {
  color: var(--app-primary-strong);
  font-size: 13px;
  min-height: 24px;
  display: inline-flex;
  align-items: center;
}
.dashboard :deep(.dashboard-link) {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  font-family: var(--app-font-sans);
  font-size: 13px;
  font-weight: 500;
  line-height: 1.5;
  color: var(--app-primary-strong);
  text-decoration: none;
  text-underline-offset: 3px;
}
.dashboard :deep(.dashboard-link:hover),
.dashboard :deep(.dashboard-link:focus-visible) {
  text-decoration: underline;
}
.dashboard :deep(.dashboard-link:focus-visible) {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: 3px;
}
.composition-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}
.composition-metric {
  min-width: 0;
}
.metric-label {
  min-height: 24px;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--app-text);
  margin-bottom: 4px;
  line-height: 1.4;
}
.metric-label > span {
  font-size: 13px;
}
.hero-value {
  font-size: clamp(30px, 13cqi, 44px);
  font-weight: 600;
  line-height: 1.4;
  letter-spacing: -0.5px;
  overflow-wrap: anywhere;
  font-variant-numeric: tabular-nums;
}
.overview-primary,
.daily-metric {
  container-type: inline-size;
}
.overview-primary .hero-value,
.daily-metric .period-value {
  margin-block: 8px;
}
.daily-metric .period-value {
  font-size: clamp(28px, 12cqi, 40px);
}
.overview-primary .metric-label,
.overview-primary .metric-caption,
.daily-metric .metric-label,
.daily-metric .metric-caption {
  font-size: 14px;
}
.metric-value,
.period-value {
  font-size: var(--app-number-secondary);
  font-weight: 600;
  line-height: 1.4;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.metric-caption {
  font-size: 13px;
  color: var(--app-text);
  line-height: 1.5;
  margin-top: 4px;
}
.overview-primary .usd-value {
  font-size: clamp(22px, 9cqi, 30px);
  font-weight: 500;
  line-height: 1.4;
  margin-top: 10px;
}
.hero-value,
.usd-value,
.metric-value:not(.period-value),
:deep(.dashboard-number),
:deep(.compact-return-value),
:deep(.compact-period-heading .receivable-value),
:deep(.compact-period-balances strong),
:deep(.txn-date-column),
:deep(.txn-number-column),
.market-share,
.market-cost-summary strong {
  font-family: var(--app-font-serif);
  font-variant-numeric: lining-nums tabular-nums;
}
:deep(.n-data-table-th.txn-date-column),
:deep(.n-data-table-th.txn-number-column) {
  font-family: var(--app-font-sans);
}
.help-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: 0;
  padding: 4px;
  width: 24px;
  height: 24px;
  color: var(--app-text-muted);
  cursor: pointer;
  margin-left: auto;
  flex-shrink: 0;
}
.help-button svg {
  width: 14px;
  height: 14px;
}
.help-button:focus-visible {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: 2px;
}
.period-pnl-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  grid-column: 1 / -1;
  gap: 16px;
  min-width: 0;
}
.period-card + .period-card {
  border-left: 1px solid var(--app-border);
  padding-left: 16px;
}
.period-value {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.quality-alert :deep(.n-button),
.load-error :deep(.n-button) {
  min-height: 24px;
}
.quality-alert,
.load-error {
  margin-bottom: 10px;
}
.reconciliation-strip {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 16px;
  border-top: 1px solid var(--app-border);
  padding-top: 10px;
  margin: 12px 0 0;
}
.reconciliation-strip .section-heading {
  margin-bottom: 0;
}
.section-links,
.account-badges,
.account-badge {
  display: flex;
  align-items: center;
}
.section-links {
  gap: 12px;
}
.account-badges {
  flex: 1;
  flex-wrap: wrap;
  gap: 6px 12px;
}
.account-badge {
  gap: 6px;
}
.account-name {
  font-size: 13px;
}
.activity-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 20px;
  border-top: 1px solid var(--app-border);
  padding-top: 10px;
}
.activity-panel {
  min-width: 0;
}
.dashboard-chart {
  width: 100%;
  max-width: 560px;
  height: 320px;
  margin-inline: auto;
}
.market-distribution {
  padding-block: 4px;
}
.chart-skeleton {
  display: block;
  margin: 12px auto;
}
.market-cost-summary {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 16px;
  padding: 0;
  margin: 0;
  list-style: none;
  font-size: 13px;
  color: var(--app-text);
  line-height: 1.5;
}
.market-cost-summary li {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-items: baseline;
  gap: 4px 12px;
  padding-block: 6px;
  border-bottom: 1px solid var(--app-border-soft);
}
.market-cost-summary li:last-child {
  border-bottom: 0;
}
.market-name {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.market-swatch {
  flex: 0 0 8px;
  width: 8px;
  height: 8px;
  border-radius: 2px;
}
.market-share {
  color: var(--app-text-muted);
  font-variant-numeric: tabular-nums;
  text-align: right;
}
.market-cost-summary strong {
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
  text-align: right;
}
.market-cost-warning {
  grid-column: 1 / -1;
  color: var(--app-warning-text);
}
:deep(.txn-link) {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0 6px;
  text-decoration: none;
  line-height: 1.4;
}
:deep(.txn-name) {
  color: var(--app-primary-strong);
  overflow-wrap: anywhere;
}
:deep(.txn-link:hover .txn-name),
:deep(.txn-link:focus-visible .txn-name) {
  text-decoration: underline;
}
:deep(.txn-sub) {
  color: var(--app-text);
  font-size: 13px;
  line-height: 1.4;
}
.dashboard .activity-panel .recent-transactions :deep(.n-data-table-td),
.dashboard .activity-panel .recent-transactions :deep(.n-data-table-th) {
  padding: 6px 8px;
  font-size: 13px;
  line-height: 1.4;
}
.recent-transactions :deep(.txn-date-column) {
  white-space: nowrap;
}
@media (min-width: 1100px) {
  .dashboard-chart {
    height: clamp(220px, calc(100dvh - 560px), 320px);
  }
  .portfolio-overview {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }
  .period-pnl-row {
    grid-column: span 2;
  }
  .period-card:first-child {
    border-left: 1px solid var(--app-border);
    padding-left: 16px;
  }
  .overview-secondary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 16px;
  }
  .composition-grid {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 16px;
  }
  .composition-metric {
    border-left: 1px solid var(--app-border);
    padding-left: 16px;
  }
}
@media (max-width: 1099px) {
  .activity-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .dashboard-chart {
    height: 320px;
  }
}
@media (max-width: 640px) {
  .market-cost-summary {
    grid-template-columns: minmax(0, 1fr);
  }
  .dashboard-chart {
    height: 280px;
    max-width: none;
  }
  .dashboard :deep(.dashboard-link) {
    min-height: 44px;
  }
  .dashboard {
    padding: 8px 0 20px;
  }
  .dashboard-heading :deep(.n-button),
  .quality-alert :deep(.n-button),
  .load-error :deep(.n-button),
  .dashboard-actions a,
  .section-heading a {
    min-height: 44px;
  }
  .dashboard-heading {
    flex-wrap: wrap;
    gap: 8px 12px;
    margin-bottom: 12px;
  }
  .dashboard-actions {
    margin-left: auto;
  }
  .hero-value {
    font-size: 32px;
  }
  .portfolio-overview,
  .period-pnl-row,
  .composition-grid {
    grid-template-columns: minmax(0, 1fr);
    gap: 12px;
  }
  .portfolio-overview {
    padding: 16px;
  }
  .overview-metric,
  .period-card + .period-card {
    border-left: 0;
    padding-left: 0;
  }
  .period-pnl-row,
  .period-card + .period-card,
  .overview-composition {
    border-top: 1px solid var(--app-border);
    padding-top: 12px;
  }
  .help-button {
    width: 44px;
    height: 44px;
  }
  .reconciliation-strip {
    display: block;
  }
  .reconciliation-strip .section-heading {
    margin-bottom: 8px;
  }
  .recent-transactions :deep(.txn-link) {
    min-height: 44px;
    align-content: center;
  }
}
</style>
