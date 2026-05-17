<template>
  <div class="holdings-page">
    <el-card class="holdings-card">
      <template #header>
        <div class="page-header">
          <span>当前持仓</span>
          <div class="header-actions">
            <el-button type="success" :icon="Refresh" @click="refreshPrices" :loading="refreshing">
              一键刷新股价
            </el-button>
            <el-select
              v-model="selectedMarket"
              placeholder="选择市场"
              clearable
              @change="loadHoldings"
              class="market-select"
            >
              <el-option label="全部市场" value="" />
              <el-option label="A股" value="A股" />
              <el-option label="B股" value="B股" />
              <el-option label="港股" value="港股" />
              <el-option label="美股" value="美股" />
              <el-option label="新加坡股" value="新加坡股" />
              <el-option label="加密货币" value="加密货币" />
            </el-select>
          </div>
        </div>
      </template>

      <div v-if="!isMobileView" class="responsive-table desktop-data-table">
        <el-table :data="holdings" v-loading="loading" stripe>
          <el-table-column prop="symbol" label="代码" width="120" />
          <el-table-column prop="name" label="名称" width="150" />
          <el-table-column prop="market" label="市场" width="100" />
          <el-table-column prop="quantity" label="持仓数量" width="120" align="right">
            <template #default="{ row }">
              {{ formatNumber(row.quantity, 4) }}
            </template>
          </el-table-column>
          <el-table-column prop="avg_cost" label="平均成本" width="120" align="right">
            <template #default="{ row }">
              {{ formatNumber(row.avg_cost, 4) }}
            </template>
          </el-table-column>
          <el-table-column prop="total_cost" label="总成本" width="140" align="right">
            <template #default="{ row }">
              <span style="font-weight: bold; color: #409eff">
                {{ formatCurrency(row.total_cost, row.currency) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="当前价格" width="132" align="right">
            <template #default="{ row }">
              <el-input-number
                v-model="currentPrices[row.symbol]"
                :min="0"
                :precision="4"
                size="small"
                @change="savePriceToDatabase(row)"
              />
            </template>
          </el-table-column>
          <el-table-column label="当前市值" width="140" align="right">
            <template #default="{ row }">
              <span v-if="currentPrices[row.symbol]" style="font-weight: bold">
                {{ formatCurrency(currentPrices[row.symbol] * row.quantity, row.currency) }}
              </span>
              <span v-else style="color: #909399">-</span>
            </template>
          </el-table-column>
          <el-table-column label="浮动盈亏" width="140" align="right">
            <template #default="{ row }">
              <span
                v-if="currentPrices[row.symbol]"
                :style="{ fontWeight: 'bold', color: getProfitColor(row) }"
              >
                {{ formatCurrency(calculateProfitAmount(row), row.currency) }}
              </span>
              <span v-else style="color: #909399">-</span>
            </template>
          </el-table-column>
          <el-table-column label="收益率" width="100" align="right">
            <template #default="{ row }">
              <span
                v-if="currentPrices[row.symbol]"
                :style="{ fontWeight: 'bold', color: getProfitColor(row) }"
              >
                {{ formatNumber(calculateProfitRate(row), 2) }}%
              </span>
              <span v-else style="color: #909399">-</span>
            </template>
          </el-table-column>
          <el-table-column prop="currency" label="币种" width="80" />
          <el-table-column prop="updated_at" label="更新时间" width="180">
            <template #default="{ row }">
              {{ new Date(row.updated_at).toLocaleString('zh-CN') }}
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div v-else v-loading="loading" class="mobile-holding-list">
        <article v-for="row in holdings" :key="`${row.market}-${row.symbol}`" class="mobile-holding-card">
          <div class="mobile-row-head">
            <div class="asset-title">
              <span class="asset-symbol">{{ row.symbol }}</span>
              <span class="asset-name">{{ row.name }}</span>
            </div>
            <el-tag size="small" effect="plain">{{ row.market }}</el-tag>
          </div>

          <div class="mobile-metrics">
            <div>
              <span class="metric-label">总成本</span>
              <strong>{{ formatCurrency(row.total_cost, row.currency) }}</strong>
            </div>
            <div>
              <span class="metric-label">当前市值</span>
              <strong v-if="currentPrices[row.symbol]">
                {{ formatCurrency(currentPrices[row.symbol] * row.quantity, row.currency) }}
              </strong>
              <strong v-else>-</strong>
            </div>
            <div>
              <span class="metric-label">浮动盈亏</span>
              <strong
                v-if="currentPrices[row.symbol]"
                :style="{ color: getProfitColor(row) }"
              >
                {{ formatCurrency(calculateProfitAmount(row), row.currency) }}
              </strong>
              <strong v-else>-</strong>
            </div>
            <div>
              <span class="metric-label">收益率</span>
              <strong
                v-if="currentPrices[row.symbol]"
                :style="{ color: getProfitColor(row) }"
              >
                {{ formatNumber(calculateProfitRate(row), 2) }}%
              </strong>
              <strong v-else>-</strong>
            </div>
          </div>

          <div class="mobile-holding-meta">
            <span>数量 {{ formatNumber(row.quantity, 4) }}</span>
            <span>成本 {{ formatNumber(row.avg_cost, 4) }}</span>
            <span>{{ row.currency }}</span>
          </div>

          <div class="mobile-price-row">
            <span>当前价格</span>
            <el-input-number
              v-model="currentPrices[row.symbol]"
              :min="0"
              :precision="4"
              size="small"
              @change="savePriceToDatabase(row)"
            />
          </div>
        </article>
      </div>

      <!-- Summary -->
      <el-divider />
      <div class="summary-section">
        <el-row :gutter="20">
          <el-col :xs="24" :sm="12" :lg="6">
            <div class="summary-item summary-cost">
              <div class="summary-label">总成本</div>
              <div class="summary-value">{{ formatCurrency(totalCostCNY) }}</div>
              <div class="summary-sub-value">{{ formatCurrency(totalCostUSD, 'USD') }}</div>
            </div>
          </el-col>
          <el-col :xs="24" :sm="12" :lg="6">
            <div class="summary-item summary-market">
              <div class="summary-label">总市值</div>
              <div class="summary-value">{{ formatCurrency(totalMarketValueCNY) }}</div>
              <div class="summary-sub-value">{{ formatCurrency(totalMarketValueUSD, 'USD') }}</div>
            </div>
          </el-col>
          <el-col :xs="24" :sm="12" :lg="6">
            <div class="summary-item summary-profit">
              <div class="summary-label">总盈亏</div>
              <div
                class="summary-value"
                :style="{ color: totalProfit >= 0 ? '#67C23A' : '#F56C6C' }"
              >
                {{ formatCurrency(totalProfit) }}
              </div>
              <div
                class="summary-sub-value"
                :style="{ color: totalProfit >= 0 ? '#67C23A' : '#F56C6C' }"
              >
                {{ formatCurrency(convertToUSD(totalProfit), 'USD') }}
              </div>
            </div>
          </el-col>
          <el-col :xs="24" :sm="12" :lg="6">
            <div class="summary-item summary-rate">
              <div class="summary-label">总收益率</div>
              <div
                class="summary-value"
                :style="{ color: totalProfitRate >= 0 ? '#67C23A' : '#F56C6C' }"
              >
                {{ formatNumber(totalProfitRate, 2) }}%
              </div>
            </div>
          </el-col>
        </el-row>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import api from '../api'
import { formatNumber, formatCurrency } from '../utils/helpers'

const loading = ref(false)
const refreshing = ref(false)
const holdings = ref([])
const selectedMarket = ref('')
const currentPrices = reactive({})
const exchangeRates = ref({})
const summaryStats = ref({})
const isMobileView = ref(false)

function updateResponsiveState() {
  isMobileView.value = window.matchMedia('(max-width: 640px)').matches
}

const totalCostCNY = computed(() => {
  return holdings.value.reduce((sum, h) => {
    const costCNY = convertToCNY(parseFloat(h.total_cost), h.currency)
    return sum + costCNY
  }, 0)
})

const totalCostUSD = computed(() => {
  return convertToUSD(totalCostCNY.value)
})

const totalCost = computed(() => totalCostCNY.value)

const totalMarketValueCNY = computed(() => {
  return holdings.value.reduce((sum, h) => {
    const price = currentPrices[h.symbol]
    if (!price) return sum
    const marketValue = price * parseFloat(h.quantity)
    const marketValueCNY = convertToCNY(marketValue, h.currency)
    return sum + marketValueCNY
  }, 0)
})

const totalMarketValueUSD = computed(() => {
  return convertToUSD(totalMarketValueCNY.value)
})

const totalMarketValue = computed(() => totalMarketValueCNY.value)

const totalProfit = computed(() => {
  return totalMarketValue.value - totalCost.value
})

const totalProfitRate = computed(() => {
  if (totalCost.value === 0) return 0
  return (totalProfit.value / totalCost.value) * 100
})

async function loadHoldings() {
  loading.value = true
  try {
    const params = {}
    if (selectedMarket.value) params.market = selectedMarket.value

    const response = await api.getHoldings(params)
    holdings.value = response.data

    // Initialize current prices from persisted market prices only.
    holdings.value.forEach((h) => {
      if (h.current_price && parseFloat(h.current_price) > 0) {
        currentPrices[h.symbol] = parseFloat(h.current_price)
      } else {
        currentPrices[h.symbol] = null
      }
    })
  } catch (error) {
    ElMessage.error('加载持仓数据失败')
  } finally {
    loading.value = false
  }
}

async function loadExchangeRates() {
  try {
    const response = await api.getLatestRates()
    const rates = {}
    Object.entries(response.data.rates).forEach(([currency, rate]) => {
      rates[currency] = parseFloat(rate)
    })
    exchangeRates.value = rates
  } catch (error) {
    console.error('加载汇率失败', error)
  }
}

async function savePriceToDatabase(row) {
  try {
    const price = currentPrices[row.symbol]
    if (!price || price <= 0) return

    await api.updateHoldingPrice(row.id, price)
    // Silent success - don't show message for every input
  } catch (error) {
    ElMessage.error(`保存${row.symbol}价格失败: ${error.message}`)
  }
}

async function refreshPrices() {
  refreshing.value = true

  const loadingMsg = ElMessage({
    message: '股价刷新已提交，正在后台处理...',
    type: 'info',
    duration: 0,
    showClose: true
  })

  try {
    const response = await api.refreshAllPrices()
    const job = response.data
    const result = await pollPriceRefreshJob(job.id)

    loadingMsg.close()

    await loadHoldings()

    let message = `成功更新 ${result.success_count} 只股票`

    if (result.skipped_count > 0) {
      message += `，跳过 ${result.skipped_count} 只（最近已更新）`
    }

    if (result.failed_count > 0) {
      message += `，${result.failed_count} 只更新失败`
    }

    if (result.failed_count === 0) {
      ElMessage.success(message)
    } else if (result.success_count > 0) {
      ElMessage.warning(message)
    } else {
      ElMessage.error('刷新失败，请稍后重试')
    }

    if (import.meta.env.DEV && result.failed_list && result.failed_list.length > 0) {
      console.group('📊 刷新失败详情')
      result.failed_list.forEach((item) => {
        console.error(`${item.symbol} (${item.market}): ${item.error}`)
      })
      console.groupEnd()
    }

    if (import.meta.env.DEV && result.success_list && result.success_list.length > 0) {
      console.group('📈 刷新成功详情')
      result.success_list.forEach((item) => {
        console.log(`${item.symbol} (${item.market}): ${item.price} [${item.source}]`)
      })
      console.groupEnd()
    }
  } catch (error) {
    loadingMsg.close()

    let errorMessage = '刷新股价失败'

    if (error.response?.data?.detail) {
      errorMessage = error.response.data.detail
    } else if (error.message) {
      errorMessage = error.message
    }

    ElMessage.error(errorMessage)
    console.error('Refresh error:', error)
  } finally {
    refreshing.value = false
  }
}

async function pollPriceRefreshJob(jobId) {
  const maxAttempts = 240

  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const response = await api.getPriceRefreshJob(jobId)
    const job = response.data

    if (job.status === 'succeeded') {
      return job.result
    }

    if (job.status === 'failed') {
      throw new Error(job.error || job.result?.error || '后台刷新失败')
    }

    await new Promise((resolve) => setTimeout(resolve, 2000))
  }

  throw new Error('刷新仍在后台运行，请稍后重新查看持仓价格')
}

async function loadSummaryStats() {
  try {
    const response = await api.getSummary()
    summaryStats.value = response.data
  } catch (error) {
    console.error('加载统计摘要失败', error)
  }
}

function convertToCNY(amount, currency) {
  if (!currency || currency === 'CNY') return amount
  const rate = exchangeRates.value[currency]
  if (!rate) return amount
  return amount * rate
}

function convertToUSD(amountCNY) {
  const usdRate = exchangeRates.value['USD']
  if (!usdRate || usdRate === 0) return 0
  return amountCNY / usdRate
}

function calculateProfit(row) {
  // Profit calculation is reactive through computed properties
}

function calculateProfitAmount(row) {
  const currentPrice = currentPrices[row.symbol]
  if (!currentPrice) return 0
  const marketValue = currentPrice * parseFloat(row.quantity)
  return marketValue - parseFloat(row.total_cost)
}

function calculateProfitRate(row) {
  const profitAmount = calculateProfitAmount(row)
  const totalCost = parseFloat(row.total_cost)
  if (totalCost === 0) return 0
  return (profitAmount / totalCost) * 100
}

function getProfitColor(row) {
  const profit = calculateProfitAmount(row)
  return profit >= 0 ? '#67C23A' : '#F56C6C'
}

onMounted(async () => {
  updateResponsiveState()
  window.addEventListener('resize', updateResponsiveState)
  await Promise.all([loadExchangeRates(), loadSummaryStats(), loadHoldings()])
})

onUnmounted(() => {
  window.removeEventListener('resize', updateResponsiveState)
})
</script>

<style scoped>
.holdings-page {
  width: 100%;
}

.holdings-card {
  overflow: hidden;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.header-actions {
  display: flex;
  align-items: center;
}

.market-select {
  width: 150px;
}

.summary-section {
  margin-top: 24px;
}

.mobile-holding-list {
  display: none;
}

.summary-item {
  min-height: 116px;
  padding: 18px;
  background-color: var(--app-surface-muted);
  border: 1px solid var(--app-border-soft);
  border-left: 3px solid var(--summary-accent);
  border-radius: 8px;
}

.summary-cost {
  --summary-accent: var(--app-primary);
}

.summary-market {
  --summary-accent: #0891b2;
}

.summary-profit {
  --summary-accent: var(--app-success);
}

.summary-rate {
  --summary-accent: var(--app-warning);
}

.summary-label {
  font-size: 14px;
  color: var(--app-text-muted);
  margin-bottom: 8px;
}

.summary-value {
  font-size: 25px;
  font-weight: 760;
  color: var(--app-text);
  line-height: 1.2;
}

.summary-sub-value {
  font-size: 14px;
  color: var(--app-text-muted);
  margin-top: 4px;
}

:deep(.el-input-number) {
  width: 112px;
}

@media (max-width: 900px) {
  .header-actions {
    align-items: stretch;
    width: 100%;
  }

  .market-select {
    width: 100%;
  }

  .summary-section {
    margin-top: 18px;
  }

  .summary-value {
    font-size: 22px;
    overflow-wrap: anywhere;
  }
}

@media (max-width: 640px) {
  .holdings-card :deep(.el-card__header) {
    text-align: center;
  }

  .desktop-data-table {
    display: none;
  }

  .mobile-holding-list {
    display: grid;
    gap: 12px;
  }

  .mobile-holding-card {
    padding: 14px;
    background: var(--app-surface);
    border: 1px solid var(--app-border-soft);
    border-radius: 8px;
    box-shadow: 0 2px 10px rgba(15, 23, 42, 0.04);
  }

  .mobile-row-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 12px;
  }

  .asset-title {
    min-width: 0;
  }

  .asset-symbol {
    display: block;
    color: var(--app-text);
    font-size: 18px;
    font-weight: 760;
    line-height: 1.2;
  }

  .asset-name {
    display: block;
    margin-top: 3px;
    color: var(--app-text-muted);
    font-size: 13px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .mobile-metrics {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
  }

  .mobile-metrics > div {
    min-width: 0;
    padding: 10px;
    background: var(--app-surface-muted);
    border-radius: 8px;
  }

  .metric-label {
    display: block;
    margin-bottom: 5px;
    color: var(--app-text-muted);
    font-size: 12px;
  }

  .mobile-metrics strong {
    display: block;
    color: var(--app-text);
    font-size: 16px;
    line-height: 1.25;
    overflow-wrap: anywhere;
  }

  .mobile-holding-meta {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    margin-top: 12px;
    color: var(--app-text-muted);
    font-size: 12px;
  }

  .mobile-price-row {
    display: grid;
    grid-template-columns: 72px minmax(0, 1fr);
    align-items: center;
    gap: 10px;
    margin-top: 12px;
    color: var(--app-text-muted);
    font-size: 13px;
  }

  .mobile-price-row :deep(.el-input-number) {
    width: 100%;
  }
}
</style>
