<template>
  <div class="statistics-page" v-loading="loading && !initialLoading">
    <template v-if="initialLoading">
      <el-row :gutter="20" class="performance-cards">
        <el-col :span="24">
          <el-card class="stat-card" shadow="never">
            <template #header>
              <el-skeleton animated>
                <template #template>
                  <el-skeleton-item variant="text" class="skeleton-heading" />
                </template>
              </el-skeleton>
            </template>
            <el-row :gutter="20">
              <el-col v-for="index in 3" :key="index" :xs="24" :sm="8">
                <div class="statistic-skeleton-block">
                  <el-skeleton animated>
                    <template #template>
                      <el-skeleton-item variant="text" class="skeleton-label" />
                      <el-skeleton-item variant="h3" class="skeleton-number" />
                    </template>
                  </el-skeleton>
                </div>
              </el-col>
            </el-row>
            <el-divider />
            <el-skeleton animated :rows="5" />
          </el-card>
        </el-col>
      </el-row>

      <el-row :gutter="20" class="performance-cards">
        <el-col v-for="index in 2" :key="index" :xs="24" :md="12">
          <el-card class="stat-card" shadow="never">
            <el-skeleton animated>
              <template #template>
                <el-skeleton-item variant="text" class="skeleton-heading" />
                <div class="stat-card-skeleton-grid">
                  <el-skeleton-item variant="h3" />
                  <el-skeleton-item variant="h3" />
                </div>
                <el-skeleton-item v-for="row in 4" :key="row" variant="text" />
              </template>
            </el-skeleton>
          </el-card>
        </el-col>
      </el-row>

      <el-row :gutter="20">
        <el-col :xs="24" :md="12">
          <el-card class="stat-card">
            <el-skeleton animated>
              <template #template>
                <el-skeleton-item variant="text" class="skeleton-heading" />
                <el-skeleton-item variant="circle" class="chart-circle-skeleton" />
              </template>
            </el-skeleton>
          </el-card>
        </el-col>
        <el-col :xs="24" :md="12">
          <el-card class="stat-card">
            <el-skeleton animated :rows="8" />
          </el-card>
        </el-col>
      </el-row>
    </template>

    <template v-else>
      <el-alert
        v-for="warning in summaryWarnings"
        :key="warning"
        :title="warning"
        type="error"
        show-icon
        :closable="false"
        class="summary-warning"
      />

      <EquityReturnCard :account-return="summary.state.accountReturn" />

      <AnalyticsCard :analytics="analytics" />

      <FifoPerformanceCards
        :current-performance="summary.state.currentPerformance"
        :realized-pn-l="summary.state.realizedPnL"
        :total-realized-return="summary.state.totalRealizedReturn"
        :summary-stats="dist.state.summaryStats"
        :refreshing="refreshing"
        @refresh-prices="refreshPricesAndCalculate"
        @open-price-dialog="prices.state.dialogVisible = true"
      />

      <DividendSummaryCard :dividend-summary="summary.state.dividendSummary" />

      <DistributionSection :dist="dist" />

      <PriceInputDialog :prices="prices" :loading="loading" @calculate="calculatePerformance" />
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useExchangeRates } from '../composables/useExchangeRates'
import { useAliveGuard } from '../composables/useAliveGuard'
import { useRefreshPrices } from '../composables/useRefreshPrices'
import { getApiErrorMessage } from '../utils/apiErrors'
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
    ...(dist.state.marketStats || []).map((row) => row?.missing_rate_currencies)
  ]
  for (const list of sources) {
    for (const currency of list || []) collected.add(currency)
  }
  return Array.from(collected).sort()
})

const summaryWarnings = computed(() => {
  const warnings = [...(summary.state.realizedPnL.data_quality?.warnings || [])]
  const currencies = missingRateCurrencies.value
  // realized 那条已含币种名，避免同一批币种重复提示两遍
  const alreadyMentioned = warnings.some((text) =>
    currencies.every((currency) => text.includes(currency))
  )
  if (currencies.length && !alreadyMentioned) {
    warnings.push(
      `缺少 ${currencies.join('/')} 对 CNY 的汇率，这些币种的金额未计入 CNY 汇总（不会按原值混入）。` +
        '请在「汇率管理」补录后重新查看。'
    )
  }
  return warnings
})

async function loadAllData() {
  initialLoading.value = true
  try {
    const supportingDataPromise = Promise.all([
      dist.loadMarketStats(),
      dist.loadTimeStats(),
      dist.loadProfitLoss(),
      dist.loadSummaryStats(),
      loadExchangeRates(),
      analytics.loadBenchmarkCatalog()
    ])

    // Server-side pricing (issue #46) lets these run concurrently: the two
    // heavy endpoints no longer wait for the holdings price payload.
    await Promise.all([prices.loadHoldingsForPrice(), summary.load(), analytics.load()])
    initialLoading.value = false
    await supportingDataPromise
  } finally {
    initialLoading.value = false
  }
}

async function calculatePerformance(showSuccess = true) {
  loading.value = true
  try {
    // Dialog what-if: value using the (possibly unsaved) dialog prices.
    const currentPrices = prices.getCurrentPrices()
    await summary.load(currentPrices)
    await analytics.load({ prices: currentPrices })
    prices.state.dialogVisible = false
    if (showSuccess) {
      ElMessage.success('计算完成')
    }
  } catch (error) {
    ElMessage.error('计算失败：' + getApiErrorMessage(error))
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

    // Step 2: Reload holdings with updated prices
    await prices.loadHoldingsForPrice({ force: true })

    // Step 3: Auto-calculate performance
    await calculatePerformance(false)

    notifyRefreshResult(refreshResult, '，并完成计算')
  } catch (error) {
    if (!isUnmounted()) {
      ElMessage.error('刷新失败：' + getApiErrorMessage(error))
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

.skeleton-heading {
  width: 160px;
  height: 18px;
}

.statistic-skeleton-block {
  min-height: 86px;
  padding: 8px 0;
}

.skeleton-label {
  width: 90px;
  height: 14px;
  margin-bottom: 12px;
}

.skeleton-number {
  width: 70%;
  height: 28px;
}

.stat-card-skeleton-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin: 24px 0;
}

.chart-circle-skeleton {
  display: block;
  width: 180px;
  height: 180px;
  margin: 32px auto 16px;
}

.summary-warning {
  margin-bottom: 16px;
}
</style>
