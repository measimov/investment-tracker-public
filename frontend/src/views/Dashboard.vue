<template>
  <div class="dashboard" v-loading="loading && hasLoaded">
    <el-row v-if="initialLoading" :gutter="20">
      <el-col v-for="index in 4" :key="index" :xs="24" :sm="12" :md="6">
        <el-card class="summary-card summary-card-skeleton">
          <el-skeleton animated>
            <template #template>
              <div class="card-content">
                <el-skeleton-item variant="circle" class="summary-icon-skeleton" />
                <div class="card-info">
                  <el-skeleton-item variant="text" class="summary-title-skeleton" />
                  <el-skeleton-item variant="h3" class="summary-value-skeleton" />
                </div>
              </div>
            </template>
          </el-skeleton>
        </el-card>
      </el-col>
    </el-row>

    <el-row v-else :gutter="20">
      <!-- 收益看板核心卡片 -->
      <el-col :xs="24" :sm="12" :md="6">
        <el-card class="summary-card summary-card-primary">
          <div class="card-content">
            <div class="card-icon-wrap">
              <el-icon class="card-icon"><Wallet /></el-icon>
            </div>
            <div class="card-info">
              <div class="card-title">总市值</div>
              <div class="card-value">{{ formatCurrency(performance.market_value) }}</div>
              <div class="card-sub">{{ formatCurrency(performance.market_value_usd, 'USD') }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :md="6">
        <el-card class="summary-card" :class="cardTone(performance.total_return)">
          <div class="card-content">
            <div class="card-icon-wrap">
              <el-icon class="card-icon"><TrendCharts /></el-icon>
            </div>
            <div class="card-info">
              <div class="card-title">总收益（权益仓）</div>
              <div class="card-value" :style="{ color: profitColor(performance.total_return) }">
                {{ formatCurrency(performance.total_return) }}
              </div>
              <div class="card-sub">
                收益率 {{ formatPercent(performance.total_return_rate) }}
                <template v-if="performance.annualized_rate !== null">
                  · 年化（XIRR） {{ formatPercent(performance.annualized_rate) }}
                </template>
              </div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :md="6">
        <el-card class="summary-card" :class="cardTone(performance.unrealized_pnl)">
          <div class="card-content">
            <div class="card-icon-wrap">
              <el-icon class="card-icon"><DataLine /></el-icon>
            </div>
            <div class="card-info">
              <div class="card-title">
                未实现盈亏
                <el-tooltip
                  placement="top"
                  content="按 FIFO 剩余批次成本计算（与已实现盈亏同一套批次）。持仓页的「浮动盈亏」按摊薄平均成本，部分卖出过的证券两者会有差异；两种口径下已实现 + 未实现的总收益一致，只是拆分归属不同"
                >
                  <el-icon class="period-help"><InfoFilled /></el-icon>
                </el-tooltip>
              </div>
              <div class="card-value" :style="{ color: profitColor(performance.unrealized_pnl) }">
                {{ formatCurrency(performance.unrealized_pnl) }}
              </div>
              <div class="card-sub">浮盈率 {{ formatPercent(performance.unrealized_rate) }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :md="6">
        <el-card class="summary-card" :class="cardTone(performance.realized_return)">
          <div class="card-content">
            <div class="card-icon-wrap">
              <el-icon class="card-icon"><Coin /></el-icon>
            </div>
            <div class="card-info">
              <div class="card-title">已实现收益（含股息）</div>
              <div class="card-value" :style="{ color: profitColor(performance.realized_return) }">
                {{ formatCurrency(performance.realized_return) }}
              </div>
              <div class="card-sub">含税后股息 {{ formatCurrency(performance.net_dividends) }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 当日 / 本月 / 本年损益（权益仓口径，与收益曲线同一算法） -->
    <el-row v-if="periodPnl" :gutter="20" class="period-pnl-row" data-testid="period-pnl-row">
      <el-col v-for="key in PERIOD_KEYS" :key="key" :xs="24" :sm="8">
        <el-card
          class="summary-card period-card"
          :class="
            cardTone(
              periodPnl.periods[key].status === 'unavailable'
                ? null
                : periodPnl.periods[key].pnl_cny
            )
          "
        >
          <div class="card-info">
            <div class="card-title">
              {{ periodPnl.periods[key].label }}损益
              <el-tooltip :content="periodTooltip(key)" placement="top">
                <el-icon class="period-help"><InfoFilled /></el-icon>
              </el-tooltip>
            </div>
            <div
              v-if="periodPnl.periods[key].status === 'unavailable'"
              class="card-value period-unavailable"
              :data-testid="`period-pnl-${key}`"
            >
              无法计算
            </div>
            <div
              v-else
              class="card-value"
              :style="{ color: profitColor(periodPnl.periods[key].pnl_cny ?? 0) }"
              :data-testid="`period-pnl-${key}`"
            >
              {{ formatCurrency(periodPnl.periods[key].pnl_cny ?? 0) }}
              <el-tag
                v-if="periodIsEstimated(periodPnl.periods[key])"
                type="warning"
                size="small"
                effect="plain"
                class="period-estimated-tag"
              >
                估算
              </el-tag>
            </div>
            <div class="card-sub">
              <template v-if="periodPnl.periods[key].return_rate !== null">
                收益率 {{ formatPercent(periodPnl.periods[key].return_rate) }}
              </template>
              <template v-else>收益率 —</template>
              <template v-if="periodPnl.periods[key].dividend_income_cny">
                · 含股息 {{ formatCurrency(periodPnl.periods[key].dividend_income_cny) }}
              </template>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 陈价/缺价：一行摘要 + 展开看名称 + 刷新（#286，与统计页共用） -->
    <PriceIssuesAlert
      :freshness="snapshot?.prices?.freshness"
      :refreshing="refreshingPrices"
      @refresh="refreshPricesAndReload"
    />
    <!-- 其余数据质量警告（超卖、缺汇率、汇率来源、观点停摆等） -->
    <el-alert
      v-for="(warning, index) in warnings"
      :key="index"
      type="warning"
      :title="warning"
      :closable="false"
      show-icon
      class="quality-alert"
    />

    <!-- 账户对账状态 -->
    <el-card v-if="accounts.length" class="panel-card reconciliation-strip">
      <template #header>
        <div class="card-header">
          <span>账户对账状态</span>
          <div>
            <el-button type="primary" text @click="$router.push('/reports')"> AI 复盘 </el-button>
            <el-button type="primary" text @click="$router.push('/account-data')">
              账户数据
            </el-button>
          </div>
        </div>
      </template>
      <div class="account-badges">
        <div v-for="account in accounts" :key="account.id" class="account-badge">
          <span class="account-name">{{ account.account_name }}</span>
          <el-tag
            :type="reconciliationTag(account.latest_reconciliation)"
            size="small"
            effect="plain"
          >
            {{ reconciliationLabel(account.latest_reconciliation) }}
          </el-tag>
        </div>
      </div>
    </el-card>

    <el-row :gutter="20" class="section-gap">
      <!-- Market Distribution Chart -->
      <el-col :xs="24" :md="12">
        <el-card class="panel-card">
          <template #header>
            <div class="card-header">
              <span>市场分布（按成本）</span>
            </div>
          </template>
          <div v-if="initialLoading" class="chart-skeleton">
            <el-skeleton animated>
              <template #template>
                <el-skeleton-item variant="circle" class="chart-circle-skeleton" />
                <div class="chart-legend-skeleton">
                  <el-skeleton-item v-for="index in 4" :key="index" variant="text" />
                </div>
              </template>
            </el-skeleton>
          </div>
          <el-empty
            v-else-if="marketStats.length === 0"
            description="暂无市场分布数据"
            :image-size="88"
          />
          <market-pie-chart v-else :option="marketChartOption" class="dashboard-chart" />
        </el-card>
      </el-col>

      <!-- Recent Transactions -->
      <el-col :xs="24" :md="12">
        <el-card class="panel-card">
          <template #header>
            <div class="card-header">
              <span>最近交易</span>
              <el-button type="primary" text @click="$router.push('/transactions')"
                >查看全部</el-button
              >
            </div>
          </template>
          <div v-if="initialLoading" class="table-skeleton">
            <el-skeleton animated :rows="5" />
          </div>
          <div v-else class="responsive-table">
            <!-- 仪表盘摘要表刻意矮（300）：只展示最近几笔，完整列表在交易页 -->
            <el-table
              :data="recentTransactions"
              max-height="300"
              stripe
              v-loading="loading && hasLoaded"
            >
              <template #empty>
                <el-empty description="暂无最近交易" :image-size="88" />
              </template>
              <el-table-column prop="transaction_date" label="日期" min-width="110">
                <template #default="{ row }">
                  {{ formatDate(row.transaction_date) }}
                </template>
              </el-table-column>
              <el-table-column label="标的" min-width="140">
                <template #default="{ row }">
                  <!-- 点标的跳持仓页并定位到该行（已清仓时持仓页会提示并给档案入口） -->
                  <router-link
                    :to="holdingsLink(row)"
                    class="txn-link"
                    data-testid="recent-txn-symbol-link"
                  >
                    <div class="txn-name">{{ row.name || row.symbol }}</div>
                    <div class="txn-sub">{{ row.symbol }} · {{ row.market }}</div>
                  </router-link>
                </template>
              </el-table-column>
              <el-table-column prop="transaction_type" label="类型" width="80">
                <template #default="{ row }">
                  <el-tag :type="transactionTypeTag(row.transaction_type)" size="small">
                    {{ transactionTypeLabel(row.transaction_type) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="quantity" label="数量" min-width="110" align="right">
                <template #default="{ row }">
                  {{ formatQuantity(row.quantity) }}
                </template>
              </el-table-column>
              <el-table-column prop="price" label="价格" min-width="100" align="right">
                <template #default="{ row }">
                  {{ formatCurrency(row.price, row.currency) }}
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <p class="methodology-note">
      总收益与年化（XIRR）为权益仓口径（仅证券投入，闲置现金与外部出入金不计入、不稀释收益率）；市场分布按持仓成本（非市值）；详情见统计分析页。
    </p>
  </div>
</template>

<script setup lang="ts">
import PriceIssuesAlert from '@/components/PriceIssuesAlert.vue'
import { useAliveGuard } from '@/composables/useAliveGuard'
import { useRefreshPrices } from '@/composables/useRefreshPrices'
import { isPriceIssueWarning, type PriceFreshnessEntry } from '@/utils/priceIssues'
import { showApiError } from '@/utils/showApiError'
import { transactionTypeLabel, transactionTypeTag } from '@/utils/labels'
import { ref, onMounted, computed, defineAsyncComponent } from 'vue'
import { Wallet, TrendCharts, DataLine, Coin, InfoFilled } from '@element-plus/icons-vue'
import api from '../api'
import { useAutoReload } from '../composables/useAutoReload'
import type { MarketStat, PeriodPnlKey, PeriodPnlResponse } from '../types'
import {
  profitColor,
  formatPercent,
  formatDate,
  formatCurrency,
  formatQuantity
} from '../utils/helpers'
import { holdingsLink } from '../utils/securities'
import { cardTone, mergeDashboardWarnings, periodIsEstimated } from './dashboard/helpers'
import { CHART_FONT_FAMILY, CHART_PALETTE, chartTooltipCurrency } from '@/styles/tokens'

const MarketPieChart = defineAsyncComponent(() => import('./dashboard/MarketPieChart.vue'))

interface ReconciliationBadge {
  status?: string
  all_scoped?: boolean
  [key: string]: unknown
}

interface AccountBadge {
  id: number
  account_name: string
  latest_reconciliation?: ReconciliationBadge | null
  [key: string]: unknown
}

interface PortfolioSnapshot {
  performance?: {
    current_performance: {
      current_market_value_cny: number
      current_market_value_usd: number
      unrealized_pnl_cny: number
      unrealized_pnl_rate: number
    }
    account_return: {
      total_return_cny: number
      total_return_rate: number
      annualized_return_rate: number | null
    }
    total_realized_return: {
      total_realized_return_cny: number
      net_dividend_income_cny: number
    }
  } | null
  prices?: {
    missing_keys?: string[]
    stale_keys?: string[]
    freshness?: Record<string, PriceFreshnessEntry>
  }
  markets?: MarketStat[]
  recent_transactions?: Array<Record<string, unknown>>
  accounts?: AccountBadge[]
  data_quality?: { warnings?: string[] }
  [key: string]: unknown
}

const snapshot = ref<PortfolioSnapshot | null>(null)
// 区间损益单独请求：失败不影响看板主体（只是不显示这一行）
const periodPnl = ref<PeriodPnlResponse | null>(null)
const PERIOD_KEYS: PeriodPnlKey[] = ['daily', 'mtd', 'ytd']

function periodTooltip(key: PeriodPnlKey): string {
  const period = periodPnl.value?.periods[key]
  if (!period) return ''
  let text =
    `${period.start_date} 至 ${period.end_date}：期末市值 + 卖出与分红流出 − 期初市值 − 买入流入。` +
    '期初按区间起点前最近收盘价估值；权益仓口径（不含闲置现金与出入金），收益率为时间加权（实验）。'
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
          `${p.symbol}（${p.basis_date}${p.basis_source === 'transaction' ? '成交价' : '收盘'}）`
      )
      .join('、')
    text += `估算：以下持仓的期初价早于区间起点，损益含此前累积涨跌：${bases}。`
  }
  const staleClosing = period.stale_closing_prices ?? []
  if (staleClosing.length > 0) {
    const names = staleClosing
      .map((p) => `${p.symbol}（现价 ${p.price_date}，期初 ${p.basis_date}）`)
      .join('、')
    text += `估算：以下持仓的估值价早于期初基准，期末按旧价计：${names}。`
  }
  return text
}
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
    annualized_rate: perf.account_return.annualized_return_rate,
    unrealized_pnl: perf.current_performance.unrealized_pnl_cny,
    unrealized_rate: perf.current_performance.unrealized_pnl_rate,
    realized_return: perf.total_realized_return.total_realized_return_cny,
    net_dividends: perf.total_realized_return.net_dividend_income_cny
  }
})

const marketStats = computed(() => snapshot.value?.markets || [])
const recentTransactions = computed(() => snapshot.value?.recent_transactions || [])
const accounts = computed(() => snapshot.value?.accounts || [])
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
  if (!latest) return 'info'
  if (latest.status === 'MATCHED') return 'success'
  if (latest.status === 'MISMATCHED') return 'danger'
  return 'warning'
}

const marketChartOption = computed(() => ({
  color: CHART_PALETTE,
  textStyle: { fontFamily: CHART_FONT_FAMILY },
  tooltip: {
    trigger: 'item',
    formatter: (params: { name: string; value: number; percent: number }) =>
      `${params.name}: ${chartTooltipCurrency(params.value)} (${params.percent}%)`
  },
  legend: { bottom: 0, left: 'center' },
  series: [
    {
      type: 'pie',
      // 与统计页保持同一 donut 设计（同一份数据不该有两种图形语言）
      radius: ['42%', '68%'],
      center: ['50%', '44%'],
      data: marketStats.value.map((item) => ({
        name: item.market,
        value: item.total_cost
      })),
      label: { formatter: '{b}: {d}%' },
      emphasis: {
        itemStyle: { shadowBlur: 10, shadowOffsetX: 0, shadowColor: 'rgba(0, 0, 0, 0.35)' }
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
    snapshot.value = response.data
    periodPnl.value = periodResponse?.data ?? null
  } catch (error) {
    if (options.silent) throw error
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
}

.period-pnl-row {
  margin-top: 4px;
}

.period-card .card-title {
  display: flex;
  align-items: center;
  gap: 4px;
}

.period-unavailable {
  color: var(--el-text-color-secondary);
}

.period-estimated-tag {
  margin-left: 6px;
  vertical-align: middle;
}

.period-help {
  cursor: help;
  color: var(--el-text-color-secondary);
}

.summary-card {
  margin-bottom: 16px;
  overflow: hidden;
}

.summary-card-primary {
  --card-accent: var(--app-primary);
  --card-tint: var(--app-primary-soft);
}

.summary-card-success {
  --card-accent: var(--app-success);
  --card-tint: var(--app-success-soft);
}

.summary-card-danger {
  --card-accent: var(--app-danger);
  --card-tint: var(--app-danger-soft);
}

.summary-card-neutral {
  --card-accent: var(--app-border);
  --card-tint: var(--app-surface-secondary);
  --card-chip: var(--app-surface-secondary);
  --card-chip-shadow: transparent;
}

.summary-card-skeleton {
  --card-accent: var(--app-border);
  --card-tint: var(--app-surface-secondary);
}

.card-content {
  display: flex;
  align-items: center;
  gap: 12px;
}

.card-icon-wrap {
  display: grid;
  place-items: center;
  width: 40px;
  height: 40px;
  color: var(--card-accent, var(--app-text-muted));
  background: var(--card-tint, var(--app-surface-secondary));
  border-radius: var(--app-radius-inner);
  flex-shrink: 0;
}

.card-icon {
  font-size: 20px;
}

.summary-icon-skeleton {
  width: 40px;
  height: 40px;
  border-radius: var(--app-radius-inner);
}

.summary-title-skeleton {
  width: 44%;
  height: 14px;
  margin-bottom: 10px;
}

.summary-value-skeleton {
  width: 70%;
  height: 28px;
}

.card-info {
  flex: 1;
  min-width: 0;
}

.card-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--app-text-muted);
  margin-bottom: 4px;
}

.card-value {
  font-size: 22px;
  font-weight: 700;
  color: var(--app-text);
  line-height: 1.15;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}

.card-sub {
  margin-top: 3px;
  font-size: 12px;
  color: var(--app-text-muted);
  font-variant-numeric: tabular-nums;
}

.txn-link {
  display: block;
  text-decoration: none;
}

.txn-name {
  color: var(--app-text);
  line-height: 1.3;
}

.txn-link .txn-name {
  color: var(--app-primary);
}

.txn-link:hover .txn-name,
.txn-link:focus-visible .txn-name {
  text-decoration: underline;
}

.txn-sub {
  color: var(--app-text-soft);
  font-size: 12px;
  line-height: 1.3;
}

.quality-alert {
  margin-bottom: 12px;
}

.reconciliation-strip {
  margin-bottom: 8px;
}

.account-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
}

.account-badge {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  background: var(--app-surface-muted);
  border: 1px solid var(--app-border-soft);
  border-radius: var(--app-radius-sm);
}

.account-name {
  font-size: 13px;
  color: var(--app-text);
}

.panel-card {
  min-height: 120px;
}

.dashboard-chart {
  width: 100%;
  height: 300px;
}

.chart-skeleton,
.table-skeleton {
  min-height: 300px;
  display: grid;
  place-items: center;
}

.chart-skeleton :deep(.el-skeleton) {
  width: min(360px, 100%);
}

.chart-circle-skeleton {
  display: block;
  width: 180px;
  height: 180px;
  margin: 0 auto 24px;
}

.chart-legend-skeleton {
  display: grid;
  gap: 10px;
}

.table-skeleton {
  align-items: stretch;
  padding: 18px 0;
}

.methodology-note {
  margin: 16px 2px 0;
  color: var(--app-text-soft);
  font-size: 12px;
}

:deep(.el-table__empty-block) {
  min-height: 88px;
}

@media (max-width: 640px) {
  .card-content {
    align-items: flex-start;
  }

  .dashboard-chart {
    height: 260px;
  }
}
</style>
