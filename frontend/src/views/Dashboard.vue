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
        <el-card class="summary-card" :class="toneClass(performance.total_return)">
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
                  · 年化 {{ formatPercent(performance.annualized_rate) }}
                </template>
              </div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :md="6">
        <el-card class="summary-card" :class="toneClass(performance.unrealized_pnl)">
          <div class="card-content">
            <div class="card-icon-wrap">
              <el-icon class="card-icon"><DataLine /></el-icon>
            </div>
            <div class="card-info">
              <div class="card-title">未实现盈亏</div>
              <div class="card-value" :style="{ color: profitColor(performance.unrealized_pnl) }">
                {{ formatCurrency(performance.unrealized_pnl) }}
              </div>
              <div class="card-sub">浮盈率 {{ formatPercent(performance.unrealized_rate) }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :md="6">
        <el-card class="summary-card" :class="toneClass(performance.realized_return)">
          <div class="card-content">
            <div class="card-icon-wrap">
              <el-icon class="card-icon"><Coin /></el-icon>
            </div>
            <div class="card-info">
              <div class="card-title">已实现收益</div>
              <div class="card-value" :style="{ color: profitColor(performance.realized_return) }">
                {{ formatCurrency(performance.realized_return) }}
              </div>
              <div class="card-sub">含税后股息 {{ formatCurrency(performance.net_dividends) }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 数据质量警告（陈价/缺价/超卖等） -->
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
              <span>市场分布</span>
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
          <v-chart v-else :option="marketChartOption" class="dashboard-chart" autoresize />
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
              <el-table-column prop="symbol" label="代码" min-width="90" />
              <el-table-column prop="transaction_type" label="类型" width="80">
                <template #default="{ row }">
                  <el-tag :type="typeTagKind(row.transaction_type)" size="small">
                    {{ typeLabel(row.transaction_type) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="quantity" label="数量" min-width="110" align="right">
                <template #default="{ row }">
                  {{ formatNumber(row.quantity, 2) }}
                </template>
              </el-table-column>
              <el-table-column prop="price" label="价格" min-width="100" align="right">
                <template #default="{ row }">
                  {{ formatNumber(row.price, 2) }}
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <p class="methodology-note">
      总收益与年化为权益仓口径（仅证券投入，闲置现金与外部出入金不计入、不稀释收益率）；详情见统计分析页。
    </p>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { PieChart as EChartsPieChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import { ElMessage } from 'element-plus'
import { Wallet, TrendCharts, DataLine, Coin } from '@element-plus/icons-vue'
import api from '../api'
import { getApiErrorMessage } from '../utils/apiErrors'
import {
  profitColor,
  formatNumber,
  formatPercent,
  formatDate,
  formatCurrency
} from '../utils/helpers'
import { CHART_FONT_FAMILY, CHART_PALETTE, chartTooltipCurrency } from '@/styles/tokens'

use([CanvasRenderer, EChartsPieChart, TitleComponent, TooltipComponent, LegendComponent])

interface MarketStat {
  market: string
  total_cost: number
  [key: string]: unknown
}

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
  markets?: MarketStat[]
  recent_transactions?: Array<Record<string, unknown>>
  accounts?: AccountBadge[]
  data_quality?: { warnings?: string[] }
  [key: string]: unknown
}

const snapshot = ref<PortfolioSnapshot | null>(null)
const loading = ref(false)
const hasLoaded = ref(false)

const initialLoading = computed(() => loading.value && !hasLoaded.value)

const performance = computed(() => {
  const perf = snapshot.value?.performance
  if (!perf) {
    return {
      market_value: 0,
      market_value_usd: 0,
      total_return: 0,
      total_return_rate: 0,
      annualized_rate: null,
      unrealized_pnl: 0,
      unrealized_rate: 0,
      realized_return: 0,
      net_dividends: 0
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
const warnings = computed(() => snapshot.value?.data_quality?.warnings || [])

const TYPE_LABELS: Record<string, string> = {
  BUY: '买入',
  SELL: '卖出',
  TRANSFER_OUT: '转出',
  TRANSFER_IN: '转入'
}
const TYPE_TAG_KINDS: Record<string, 'success' | 'danger' | 'warning' | 'info'> = {
  BUY: 'success',
  SELL: 'danger',
  TRANSFER_OUT: 'warning',
  TRANSFER_IN: 'info'
}
const typeLabel = (type: string) => TYPE_LABELS[type] || type
const typeTagKind = (type: string) => TYPE_TAG_KINDS[type] || 'info'

const toneClass = (value: number | string | null | undefined) =>
  Number(value) >= 0 ? 'summary-card-success' : 'summary-card-danger'

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

async function loadData() {
  loading.value = true
  try {
    const response = await api.getPortfolioSnapshot()
    snapshot.value = response.data
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '加载仪表盘失败'))
  } finally {
    loading.value = false
    hasLoaded.value = true
  }
}

onMounted(() => {
  loadData()
})
</script>

<style scoped>
.dashboard {
  width: 100%;
}

.summary-card {
  margin-bottom: 20px;
  overflow: hidden;
  background:
    linear-gradient(135deg, var(--card-tint), transparent 62%), var(--app-surface) !important;
  transition:
    box-shadow var(--app-duration) var(--apple-ease),
    transform var(--app-duration) var(--apple-ease);
  animation: cardIn 0.5s var(--apple-spring) both;
}

.el-col:nth-child(1) .summary-card {
  animation-delay: 0.02s;
}

.el-col:nth-child(2) .summary-card {
  animation-delay: 0.08s;
}

.el-col:nth-child(3) .summary-card {
  animation-delay: 0.14s;
}

.el-col:nth-child(4) .summary-card {
  animation-delay: 0.2s;
}

@keyframes cardIn {
  from {
    opacity: 0;
    transform: translateY(12px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.summary-card-primary {
  --card-accent: var(--app-primary);
  --card-tint: var(--app-primary-soft);
  --card-chip: var(--app-chip-gradient-primary);
  --card-chip-shadow: rgba(79, 70, 229, 0.35);
}

.summary-card-success {
  --card-accent: var(--app-success);
  --card-tint: var(--app-success-soft);
  --card-chip: var(--app-chip-gradient-success);
  --card-chip-shadow: rgba(16, 185, 129, 0.32);
}

.summary-card-danger {
  --card-accent: var(--app-danger);
  --card-tint: var(--app-danger-soft);
  --card-chip: var(--app-chip-gradient-danger);
  --card-chip-shadow: rgba(244, 63, 94, 0.32);
}

.summary-card-skeleton {
  --card-accent: var(--app-border);
  --card-tint: var(--app-surface-secondary);
}

.summary-card:hover {
  box-shadow: var(--app-shadow-md);
  transform: translateY(-3px);
}

.card-content {
  display: flex;
  align-items: center;
  gap: 16px;
}

.card-icon-wrap {
  display: grid;
  place-items: center;
  width: 48px;
  height: 48px;
  color: #fff;
  background: var(--card-chip, var(--app-surface-secondary));
  border-radius: 14px;
  box-shadow: 0 6px 14px -4px var(--card-chip-shadow, transparent);
  flex-shrink: 0;
}

.card-icon {
  font-size: 24px;
}

.summary-icon-skeleton {
  width: 48px;
  height: 48px;
  border-radius: 14px;
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
  border-radius: 8px;
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
