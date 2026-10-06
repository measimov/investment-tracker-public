<template>
  <div class="statistics-page" v-loading="loading && !initialLoading">
    <header class="statistics-heading">
      <div class="statistics-title">
        <h1 class="page-title">统计分析</h1>
        <HelpTip label="查看统计分析与价格试算范围"
          >刷新价格与输入价格试算影响当前业绩、月/年损益、TTWR
          与风险指标；区间选择仅影响区间分析。</HelpTip
        >
      </div>
      <div class="statistics-actions">
        <NButton :loading="refreshing" :disabled="initialLoading" @click="refreshPricesAndCalculate"
          >刷新价格</NButton
        >
        <NButton
          type="primary"
          :disabled="initialLoading"
          @click="prices.openDialog(summary.pricing.serverPrices)"
          >输入价格试算</NButton
        >
      </div>
    </header>
    <section
      v-if="initialLoading"
      class="statistics-loading"
      aria-label="统计数据加载中"
      aria-busy="true"
    >
      <NSkeleton text :repeat="2" />
      <div class="loading-metrics"><NSkeleton v-for="index in 3" :key="index" height="42px" /></div>
      <NSkeleton height="260px" />
    </section>
    <template v-else>
      <NAlert
        v-if="summary.state.error"
        data-testid="summary-error"
        type="warning"
        :closable="false"
        :title="`业绩摘要加载失败：${summary.state.error}${summary.state.loaded ? '；以下保留上次成功数据' : '；尚无可用数据'}`"
      />
      <PriceIssuesAlert
        :freshness="summary.pricing.priceFreshness"
        :refreshing="refreshing"
        @refresh="refreshPricesAndCalculate"
      />
      <!-- 与仪表盘同一严重度（#286：此前这里红色、仪表盘黄色） -->
      <NAlert
        v-for="warning in summaryWarnings"
        :key="warning"
        :title="warning"
        type="warning"
        :closable="false"
        class="summary-warning"
      />

      <!-- 手工价试算中：摘要与 TTWR 曲线都按弹窗里的价格计算，直到退出 -->
      <div v-if="whatIfActive" class="what-if-bar" data-testid="what-if-bar">
        <NTag type="warning" :bordered="false" size="small">手工价试算中</NTag>
        <span class="what-if-text">
          当前业绩、月/年损益、TTWR 与风险指标按「输入价格」里的价格估值（{{ whatIfCount }}
          只标的），不是服务端最新价；切换区间/基准仍沿用这组价格。
        </span>
        <NButton size="small" :loading="loading" @click="exitWhatIf">退出试算</NButton>
      </div>

      <AnalyticsCard :analytics="analytics" />

      <section class="cumulative-section" aria-labelledby="cumulative-title">
        <header class="cumulative-heading">
          <h2 id="cumulative-title">累计收益与组成</h2>
          <HelpTip label="查看累计收益统计范围">自建账累计，不随上方区间选择变化。</HelpTip>
          <router-link class="section-link" to="/corporate-actions">核对股息</router-link>
        </header>
        <div class="cumulative-grid" :class="{ 'has-receivable': summary.state.receivableReturn }">
          <ReceivableReturnCard
            compact
            embedded
            hide-review-link
            :summary="summary.state.receivableReturn"
            :return-rate="summary.state.accountReturn.total_return_rate"
            :annualized-rate="summary.state.accountReturn.annualized_return_rate"
            :legacy-unreviewed-count="summary.state.dividendSummary.legacy_unreviewed_count"
          />
          <EquityReturnCard
            :account-return="summary.state.accountReturn"
            :show-metrics="!summary.state.receivableReturn"
          />
        </div>
      </section>

      <PeriodPnlSection :summary="periods.state.data" :error="periods.state.error" />

      <FifoPerformanceCards
        :current-performance="summary.state.currentPerformance"
        :realized-pn-l="summary.state.realizedPnL"
        :total-realized-return="summary.state.totalRealizedReturn"
      />

      <DividendSummaryCard :dividend-summary="summary.state.dividendSummary" />

      <DistributionSection :dist="dist" />

      <PriceInputDialog :prices="prices" :loading="loading" @calculate="calculatePerformance" />
    </template>
  </div>
</template>

<script setup lang="ts">
import HelpTip from '@/components/HelpTip.vue'
import { NAlert, NButton, NSkeleton, NTag } from 'naive-ui'
import PriceIssuesAlert from '@/components/PriceIssuesAlert.vue'
import ReceivableReturnCard from '@/components/ReceivableReturnCard.vue'
import PeriodPnlSection from './statistics/PeriodPnlSection.vue'
import { usePeriodPnl } from './statistics/usePeriodPnl'
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useExchangeRates } from '../composables/useExchangeRates'
import { useAliveGuard } from '../composables/useAliveGuard'
import { useRefreshPrices } from '../composables/useRefreshPrices'
import EquityReturnCard from './statistics/EquityReturnCard.vue'
import AnalyticsCard from './statistics/AnalyticsCard.vue'
import FifoPerformanceCards from './statistics/FifoPerformanceCards.vue'
import DividendSummaryCard from './statistics/DividendSummaryCard.vue'
import DistributionSection from './statistics/DistributionSection.vue'
import PriceInputDialog from './statistics/PriceInputDialog.vue'
import { useAnalytics } from './statistics/useAnalytics'
import { useDistributionStats } from './statistics/useDistributionStats'
import { usePerformanceSummary } from './statistics/usePerformanceSummary'
import { usePriceInputs } from './statistics/usePriceInputs'
import { buildSummaryWarnings } from './statistics/warnings'
import { showApiError } from '@/utils/showApiError'

// 壳层职责（issue #140）：骨架屏 + 顶层警示 + 五个 feature 的编排。
// script 里原先的五件事（业绩摘要 / analytics 曲线+基准+历史同步 /
// 价格 what-if / 分布统计）各自成 composable，卡片只做展示与交互绑定。
const loading = ref(false)
const initialLoading = ref(true)
const refreshing = ref(false)

const { loadExchangeRates } = useExchangeRates()
const { isUnmounted } = useAliveGuard()
const { refreshPrices: runPriceRefresh, notifyRefreshResult } = useRefreshPrices(isUnmounted)

const summary = usePerformanceSummary()
const periods = usePeriodPnl()
const analytics = useAnalytics({ isUnmounted })
const prices = usePriceInputs()
const dist = useDistributionStats()

// 缺汇率的币种：后端在这些块里各自剔除了无法折算的金额（不再按原值当 CNY 混入），
// 但只有 realized 一块带 data_quality.warnings。其余三块只给 missing_rate_currencies，
// 不在这里合成提示的话，用户只会看到「累计投入变成 0」而不知道原因。
const missingRateCurrencies = computed(() => {
  const collected = new Set<string>()
  const sources = [
    dist.state.summaryStats?.missing_rate_currencies,
    summary.state.dividendSummary?.missing_rate_currencies,
    summary.state.currentPerformance?.missing_rate_currencies,
    summary.state.realizedPnL?.missing_rate_currencies,
    ...(dist.state.marketStats || []).map((row) => row?.missing_rate_currencies)
  ]
  for (const list of sources) {
    for (const currency of list || []) collected.add(currency)
  }
  return Array.from(collected).sort()
})

// #218：持仓表现的数据质量与服务端定价的陈价/缺价也要进顶部警告（仪表盘对
// 同一份数据会报警，统计页此前只取了已实现那一块）；合成与去重在 warnings.ts
const summaryWarnings = computed(() =>
  buildSummaryWarnings({
    realizedWarnings: summary.state.realizedPnL.data_quality?.warnings,
    currentWarnings: summary.state.currentPerformance.data_quality?.warnings,
    missingRateCurrencies: missingRateCurrencies.value,
    priceFreshness: summary.pricing.priceFreshness
  })
)

const whatIfActive = computed(() => analytics.state.whatIfPrices !== null)
const whatIfCount = computed(() => Object.keys(analytics.state.whatIfPrices || {}).length)

async function loadAllData() {
  initialLoading.value = true
  try {
    const supportingDataPromise = Promise.all([
      dist.loadAll(),
      loadExchangeRates(),
      analytics.loadBenchmarkCatalog()
    ])

    // Server-side pricing (issue #46) lets these run concurrently: the two
    // heavy endpoints no longer wait for the holdings price payload.
    await Promise.all([
      prices.loadHoldingsForPrice(),
      summary.load(),
      periods.load(),
      analytics.load()
    ])
    initialLoading.value = false
    await supportingDataPromise
  } finally {
    initialLoading.value = false
  }
}

async function calculatePerformance() {
  loading.value = true
  try {
    // Dialog what-if: value using the (possibly unsaved) dialog prices. 价格存进
    // analytics 状态，之后切区间/基准的重算都沿用，直到「退出试算」
    const currentPrices = prices.getCurrentPrices()
    analytics.state.whatIfPrices = currentPrices
    const results = await Promise.all([
      summary.load(currentPrices),
      periods.load(currentPrices),
      analytics.load()
    ])
    if (!results.every(Boolean)) return
    prices.state.dialogVisible = false
    ElMessage.success('计算完成')
  } catch (error) {
    showApiError(error, { prefix: '计算失败' })
  } finally {
    loading.value = false
  }
}

// 回到服务端定价（GET：Holding 现价优先、历史收盘兜底，附陈价/缺价标记）
async function reloadServerPriced() {
  analytics.state.whatIfPrices = null
  return (await Promise.all([summary.load(), periods.load(), analytics.load()])).every(Boolean)
}

async function exitWhatIf() {
  loading.value = true
  try {
    await reloadServerPriced()
  } catch (error) {
    showApiError(error, { prefix: '重新计算失败' })
  } finally {
    loading.value = false
  }
}

async function refreshPricesAndCalculate() {
  refreshing.value = true
  try {
    // Step 1: 刷新价格（轮询与消息拼装在 useRefreshPrices 收敛一处，
    // 此前与持仓页各写一版且已分叉）
    const refreshResult = await runPriceRefresh()
    if (!refreshResult || isUnmounted()) return

    // Step 2: 刷新后走服务端定价重算（GET）——不能再把弹窗里的 Holding 现价当
    // what-if 价 POST 上去：那会丢掉服务端的历史收盘兜底，只靠历史收盘定价的
    // 持仓会被当成「无价」剔除（#218）。刷新即退出手工价试算。
    loading.value = true
    let calculated = false
    try {
      const [, result] = await Promise.all([
        prices.loadHoldingsForPrice({ force: true }),
        reloadServerPriced()
      ])
      calculated = result
    } finally {
      loading.value = false
    }

    if (calculated) notifyRefreshResult(refreshResult, '，并完成计算')
    else ElMessage.warning('股价刷新已完成，但业绩摘要、期间损益或曲线重算失败，请重试')
  } catch (error) {
    if (!isUnmounted()) {
      showApiError(error, { prefix: '刷新失败' })
    }
  } finally {
    if (!isUnmounted()) {
      refreshing.value = false
    }
  }
}

onMounted(() => {
  loadAllData()
})
</script>

<style scoped>
.statistics-page {
  width: 100%;
}

.statistics-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 16px;
  margin-bottom: 28px;
}
.statistics-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.statistics-title {
  display: flex;
  align-items: center;
  gap: 4px;
}
.section-link {
  margin-left: auto;
  color: var(--app-primary-strong);
  font-size: 13px;
  font-weight: 500;
  text-decoration: none;
}
.section-link:hover,
.section-link:focus-visible {
  text-decoration: underline;
}
.statistics-page :deep(.n-alert) {
  background: var(--app-surface-secondary);
  font-size: 13px;
}
.statistics-page :deep(.n-alert__border) {
  border-color: var(--app-border);
}
.statistics-page :deep(.n-alert-body__title) {
  font-weight: 400;
  font-size: 13px;
}
.cumulative-section {
  margin: 16px 0;
  padding: 16px;
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius);
  background: var(--app-surface);
}
.cumulative-heading {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 16px;
}
.cumulative-heading h2 {
  margin: 0;
  font-size: 18px;
  font-weight: 500;
  font-family: var(--app-font-sans);
}
.cumulative-heading span {
  font-size: 13px;
  line-height: 1.7;
  color: var(--app-text);
}
.cumulative-grid {
  display: grid;
  min-width: 0;
}
.cumulative-grid.has-receivable {
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 24px;
}
.cumulative-grid :deep(.equity-return) {
  padding: 0;
  border-top: 0;
  min-width: 0;
}
.cumulative-grid.has-receivable :deep(.return-components) {
  grid-template-columns: minmax(0, 1fr);
}
.cumulative-grid :deep(.stat-item) {
  flex-direction: row;
  align-items: baseline;
}
.cumulative-grid :deep(.compact-return-grid) {
  grid-template-columns: minmax(0, 1fr);
  gap: 12px;
}
.cumulative-grid :deep(.compact-return-grid > div + div) {
  border-left: 0;
  padding: 12px 0 0;
  border-top: 1px solid var(--app-border-soft);
}
.cumulative-grid :deep(.compact-return-value) {
  font-size: var(--app-number-cumulative);
}
.cumulative-grid :deep(.compact-return-label) {
  gap: 4px;
}
.cumulative-grid :deep(.compact-return-tools) {
  margin-left: 0;
}
.statistics-page :deep(.period-heading) {
  align-items: center;
  gap: 4px;
}
.statistics-page :deep(.compact-period-heading) {
  grid-template-columns: minmax(0, max-content) auto minmax(0, 1fr);
  align-items: center;
  column-gap: 4px;
}
.statistics-page :deep(.compact-explanation-trigger) {
  width: 24px;
  height: 24px;
  min-height: 24px;
}
.statistics-loading {
  display: grid;
  gap: 24px;
  padding: 24px 0;
}
.loading-metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 24px;
}
.statistics-page :deep(.receivable-card) {
  margin: 0;
}
.summary-warning {
  margin-bottom: 16px;
}

.what-if-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-bottom: 16px;
  padding: 10px 14px;
  border: 1px solid var(--el-color-warning-light-5);
  border-radius: 8px;
  background: var(--el-color-warning-light-9);
}

.what-if-text {
  flex: 1;
  min-width: 200px;
  color: var(--app-text-muted);
  font-size: 13px;
}
@media (max-width: 640px) {
  .cumulative-grid.has-receivable {
    grid-template-columns: minmax(0, 1fr);
    gap: 20px;
  }
  .cumulative-grid.has-receivable :deep(.equity-return) {
    padding-top: 16px;
    border-top: 1px solid var(--app-border);
  }
  .cumulative-grid :deep(.stat-item) {
    flex-direction: column;
    align-items: flex-start;
  }
  .statistics-actions .n-button,
  .what-if-bar .n-button {
    min-height: 44px;
  }
}

@media (pointer: coarse) {
  .statistics-page :deep(.compact-explanation-trigger) {
    width: 44px;
    height: 44px;
    min-height: 44px;
    margin-block: -10px;
  }
}

@media (min-width: 1025px) {
  .statistics-heading {
    margin-bottom: 16px;
  }
  .statistics-loading {
    gap: 16px;
    padding: 16px 0;
  }
  .loading-metrics {
    gap: 16px;
  }
}
</style>
