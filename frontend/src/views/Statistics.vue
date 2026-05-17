<template>
  <div class="statistics-page">
    <el-row :gutter="20" class="performance-cards">
      <el-col :span="24">
        <el-card class="stat-card" shadow="hover">
          <template #header>
            <div class="card-header">
              <span>账户总收益</span>
            </div>
          </template>

          <el-row :gutter="15">
            <el-col :xs="24" :sm="8">
              <el-statistic
                title="总收益"
                :value="accountReturn.total_return"
                :precision="2"
                suffix="元"
                :value-style="{ color: getProfitColor(accountReturn.total_return) }"
              />
            </el-col>
            <el-col :xs="24" :sm="8">
              <el-statistic
                title="总收益率"
                :value="accountReturn.total_return_rate"
                :precision="2"
                suffix="%"
                :value-style="{ color: getProfitColor(accountReturn.total_return) }"
              />
            </el-col>
            <el-col :xs="24" :sm="8">
              <el-statistic
                title="年化收益率 (XIRR)"
                :value="accountReturn.annualized_return_rate || 0"
                :precision="2"
                suffix="%"
                :value-style="{ color: getProfitColor(accountReturn.annualized_return_rate || 0) }"
              />
            </el-col>
          </el-row>

          <el-divider />

          <div class="stat-detail">
            <div class="stat-item">
              <span>净投入本金：</span>
              <span class="value">{{ formatCurrency(accountReturn.net_invested_principal_cny) }}</span>
            </div>
            <div class="stat-item">
              <span>当前市值：</span>
              <span class="value">{{ formatCurrency(accountReturn.current_market_value_cny) }}</span>
            </div>
            <div class="stat-item">
              <span>已平仓资本利得：</span>
              <span class="value" :style="{ color: getProfitColor(accountReturn.realized_trading_pnl_cny) }">
                {{ formatCurrency(accountReturn.realized_trading_pnl_cny) }}
              </span>
            </div>
            <div class="stat-item">
              <span>未实现盈亏：</span>
              <span class="value" :style="{ color: getProfitColor(accountReturn.unrealized_pnl_cny) }">
                {{ formatCurrency(accountReturn.unrealized_pnl_cny) }}
              </span>
            </div>
            <div class="stat-item">
              <span>税后股息：</span>
              <span class="value" style="color: #67C23A">
                {{ formatCurrency(accountReturn.net_dividend_income_cny) }}
              </span>
            </div>
            <div class="stat-note">
              总收益 = 已平仓资本利得 + 未实现盈亏 + 税后股息；总收益率分母为净投入本金，年化收益率使用 XIRR。
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 新增：投资表现卡片 -->
    <el-row :gutter="20" class="performance-cards" style="margin-bottom: 20px;">
      <!-- 卡片1：当前持仓表现 -->
      <el-col :xs="24" :md="12">
        <el-card class="stat-card" shadow="hover">
          <template #header>
            <div class="card-header">
              <span>当前持仓表现</span>
              <div>
                <el-button
                  type="success"
                  size="small"
                  :icon="Refresh"
                  @click="refreshPricesAndCalculate"
                  :loading="refreshing"
                  class="refresh-button"
                >
                  一键刷新股价
                </el-button>
                <el-button type="primary" size="small" @click="showPriceDialog = true">
                  输入价格
                </el-button>
              </div>
            </div>
          </template>

          <el-row :gutter="15">
            <el-col :xs="24" :sm="12">
              <el-statistic
                title="未实现盈亏"
                :value="currentPerformance.unrealized_pnl"
                :precision="2"
                suffix="元"
                :value-style="{ color: getProfitColor(currentPerformance.unrealized_pnl) }"
              />
            </el-col>
            <el-col :xs="24" :sm="12">
              <el-statistic
                title="浮盈率"
                :value="currentPerformance.unrealized_pnl_rate"
                :precision="2"
                suffix="%"
                :value-style="{ color: getProfitColor(currentPerformance.unrealized_pnl) }"
              />
            </el-col>
          </el-row>

          <el-divider />

          <div class="stat-detail">
            <div class="stat-item">
              <span>当前持仓成本：</span>
              <span class="value">
                {{ formatCurrency(currentPerformance.current_holdings_cost || summaryStats.total_invested_cny || 0) }}
              </span>
            </div>
            <div class="stat-item">
              <span>当前市值：</span>
              <span class="value">
                {{ formatCurrency(currentPerformance.current_market_value) }}
                <el-text v-if="!currentPerformance.current_market_value" type="info" size="small">
                  (需输入价格计算)
                </el-text>
              </span>
            </div>
          </div>
        </el-card>
      </el-col>

      <!-- 卡片2：历史交易能力 -->
      <el-col :xs="24" :md="12">
        <el-card class="stat-card" shadow="hover">
          <template #header>
            <div class="card-header">
              <span>已平仓交易能力 (FIFO)</span>
            </div>
          </template>

          <el-row :gutter="15">
            <el-col :xs="24" :sm="12">
              <el-statistic
                title="已平仓盈亏"
                :value="realizedPnL.realized_pnl"
                :precision="2"
                suffix="元"
                :value-style="{ color: getProfitColor(realizedPnL.realized_pnl) }"
              />
            </el-col>
            <el-col :xs="24" :sm="12">
              <el-statistic
                title="已平仓收益率"
                :value="realizedPnL.realized_pnl_rate"
                :precision="2"
                suffix="%"
                :value-style="{ color: getProfitColor(realizedPnL.realized_pnl) }"
              />
            </el-col>
          </el-row>

          <el-divider />

          <div class="stat-detail">
            <div class="stat-item">
              <span>含股息已实现收益：</span>
              <span class="value" :style="{ color: getProfitColor(totalRealizedReturn.total_realized_return) }">
                {{ formatCurrency(totalRealizedReturn.total_realized_return) }}
              </span>
            </div>
            <div class="stat-item">
              <span>含股息已实现收益率：</span>
              <span class="value" :style="{ color: getProfitColor(totalRealizedReturn.total_realized_return) }">
                {{ formatNumber(totalRealizedReturn.total_realized_return_rate, 2) }}%
              </span>
            </div>
            <div class="stat-item">
              <span>已卖出FIFO成本：</span>
              <span class="value">{{ formatCurrency(realizedPnL.sold_cost) }}</span>
            </div>
            <div class="stat-item">
              <span>资本利得：</span>
              <span class="value" :style="{ color: getProfitColor(realizedPnL.realized_pnl) }">
                {{ formatCurrency(realizedPnL.realized_pnl) }}
              </span>
            </div>
            <div class="stat-item">
              <span>税后股息：</span>
              <span class="value" style="color: #67C23A">
                {{ formatCurrency(totalRealizedReturn.net_dividend_income_cny) }}
              </span>
            </div>
            <div class="stat-note">
              已平仓收益率只评价已卖出的交易；分母为被卖出部分的 FIFO 成本。
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 股息统计（独立卡片） -->
    <el-row :gutter="20" style="margin-bottom: 20px;">
      <el-col :span="24">
        <el-card class="stat-card" shadow="hover">
          <template #header>
            <div class="card-header">
              <span>股息收入统计</span>
            </div>
          </template>

          <el-row :gutter="15">
            <el-col :xs="24" :sm="8">
              <el-statistic
                title="累计股息（税前）"
                :value="dividendSummary.total_dividend_gross"
                :precision="2"
                suffix="元"
              />
            </el-col>
            <el-col :xs="24" :sm="8">
              <el-statistic
                title="累计税费"
                :value="dividendSummary.total_tax"
                :precision="2"
                suffix="元"
              />
            </el-col>
            <el-col :xs="24" :sm="8">
              <el-statistic
                title="累计股息（税后）"
                :value="dividendSummary.total_dividend_net"
                :precision="2"
                suffix="元"
                :value-style="{ color: '#67C23A' }"
              />
            </el-col>
          </el-row>
        </el-card>
      </el-col>
    </el-row>

    <!-- 原有的图表和表格 -->
    <el-row :gutter="20">
      <!-- Market Distribution -->
      <el-col :xs="24" :md="12">
        <el-card class="stat-card">
          <template #header>
            <span>市场分布统计</span>
          </template>
          <v-chart :option="marketChartOption" class="chart chart-market" />
        </el-card>
      </el-col>

      <!-- Market Table -->
      <el-col :xs="24" :md="12">
        <el-card class="stat-card">
          <template #header>
            <span>市场详细数据</span>
          </template>
          <div class="responsive-table">
            <el-table :data="marketStats" style="width: 100%">
              <el-table-column prop="market" label="市场" width="120" />
              <el-table-column prop="holdings_count" label="持仓数" width="100" align="right" />
              <el-table-column prop="total_cost" label="总成本" width="140" align="right">
                <template #default="{ row }">
                  <span style="font-weight: bold">
                    {{ formatCurrency(row.total_cost) }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="占比" width="100" align="right">
                <template #default="{ row }">
                  {{ formatNumber((row.total_cost / totalInvested) * 100, 2) }}%
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- Transaction Timeline -->
    <el-row :gutter="20" style="margin-top: 20px;">
      <el-col :span="24">
        <el-card class="stat-card">
          <template #header>
            <div class="chart-header">
              <span>交易时间趋势</span>
              <el-radio-group v-model="timeGroupBy" @change="loadTimeStats">
                <el-radio-button label="month">按月</el-radio-button>
                <el-radio-button label="year">按年</el-radio-button>
              </el-radio-group>
            </div>
          </template>
          <v-chart :option="timeChartOption" class="chart chart-time" />
        </el-card>
      </el-col>
    </el-row>

    <!-- Holdings Ranking -->
    <el-row :gutter="20" style="margin-top: 20px;">
      <el-col :span="24">
        <el-card class="stat-card">
          <template #header>
            <span>持仓排行</span>
          </template>
          <div class="responsive-table">
            <el-table :data="profitLossData" style="width: 100%">
              <el-table-column type="index" label="排名" width="80" />
              <el-table-column prop="symbol" label="代码" width="120" />
              <el-table-column prop="name" label="名称" width="150" />
              <el-table-column prop="market" label="市场" width="100" />
              <el-table-column prop="quantity" label="数量" width="120" align="right">
                <template #default="{ row }">
                  {{ formatNumber(row.quantity, 4) }}
                </template>
              </el-table-column>
              <el-table-column prop="avg_cost" label="成本价" width="120" align="right">
                <template #default="{ row }">
                  {{ formatNumber(row.avg_cost, 4) }}
                </template>
              </el-table-column>
              <el-table-column prop="total_cost" label="总成本" width="140" align="right">
                <template #default="{ row }">
                  <span style="font-weight: bold; color: #409EFF">
                    {{ formatCurrency(row.total_cost, row.currency) }}
                  </span>
                  <div v-if="row.currency !== 'CNY'" style="font-size: 12px; color: #909399; margin-top: 2px">
                    ≈ {{ formatCurrency(convertToCNY(row.total_cost, row.currency)) }}
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="占比" width="100" align="right">
                <template #default="{ row }">
                  {{ formatNumber((convertToCNY(row.total_cost, row.currency) / totalInvestedCNY) * 100, 2) }}%
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 价格输入对话框 -->
    <el-dialog
      v-model="showPriceDialog"
      title="输入当前价格"
      width="800px"
    >
      <div class="responsive-table">
        <el-table :data="priceInputData" max-height="500" v-loading="loading">
          <el-table-column prop="symbol" label="代码" width="100" />
          <el-table-column prop="name" label="名称" width="150" />
          <el-table-column prop="market" label="市场" width="100" />
          <el-table-column label="持仓数量" width="120" align="right">
            <template #default="{ row }">
              {{ formatNumber(row.quantity, 2) }}
            </template>
          </el-table-column>
          <el-table-column label="平均成本" width="120" align="right">
            <template #default="{ row }">
              {{ formatNumber(row.avg_cost, 4) }}
            </template>
          </el-table-column>
          <el-table-column label="当前价格" width="180">
            <template #default="{ row }">
              <el-input-number
                v-model="row.current_price"
                :min="0"
                :precision="4"
                size="small"
                style="width: 100%"
              />
            </template>
          </el-table-column>
        </el-table>
      </div>

      <template #footer>
        <div class="mobile-dialog-footer price-dialog-footer">
        <el-button @click="showPriceDialog = false">取消</el-button>
        <el-button @click="savePrices" :loading="saving">保存价格</el-button>
        <el-button type="primary" @click="calculatePerformance" :loading="loading">计算</el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { PieChart, BarChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent, LegendComponent, GridComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import api from '../api'
import { formatNumber, formatCurrency } from '../utils/helpers'

use([CanvasRenderer, PieChart, BarChart, TitleComponent, TooltipComponent, LegendComponent, GridComponent])

const marketStats = ref([])
const timeStats = ref([])
const profitLossData = ref([])
const timeGroupBy = ref('month')
const summaryStats = ref({})

// 新增：性能统计数据
const currentPerformance = ref({
  unrealized_pnl: 0,
  current_holdings_cost: 0,
  unrealized_pnl_rate: 0,
  current_market_value: 0,
  holdings_detail: []
})

const realizedPnL = ref({
  realized_pnl: 0,
  sold_cost: 0,
  realized_pnl_rate: 0,
  trades_detail: []
})

const dividendSummary = ref({
  total_dividend_gross: 0,
  total_tax: 0,
  total_dividend_net: 0,
  by_symbol: []
})

const totalRealizedReturn = ref({
  realized_trading_pnl_cny: 0,
  net_dividend_income_cny: 0,
  total_realized_return: 0,
  total_realized_return_rate: 0,
  sold_cost_cny: 0
})

const accountReturn = ref({
  total_return: 0,
  total_return_rate: 0,
  annualized_return_rate: null,
  net_invested_principal_cny: 0,
  current_market_value_cny: 0,
  realized_trading_pnl_cny: 0,
  unrealized_pnl_cny: 0,
  net_dividend_income_cny: 0
})

const showPriceDialog = ref(false)
const priceInputData = ref([])
const loading = ref(false)
const saving = ref(false)
const refreshing = ref(false)
const exchangeRates = ref({})

const totalInvested = computed(() => {
  return marketStats.value.reduce((sum, item) => sum + item.total_cost, 0)
})

const totalInvestedCNY = computed(() => {
  return profitLossData.value.reduce((sum, item) => {
    return sum + convertToCNY(item.total_cost, item.currency)
  }, 0)
})

function convertToCNY(amount, currency) {
  if (!currency || currency === 'CNY') return amount
  const rate = exchangeRates.value[currency]
  if (!rate) return amount
  return amount * rate
}

const marketChartOption = computed(() => ({
  tooltip: {
    trigger: 'item',
    formatter: '{b}: ¥{c} ({d}%)'
  },
  legend: {
    orient: 'vertical',
    left: 'left'
  },
  series: [
    {
      type: 'pie',
      radius: ['40%', '70%'],
      avoidLabelOverlap: false,
      itemStyle: {
        borderRadius: 10,
        borderColor: '#fff',
        borderWidth: 2
      },
      label: {
        show: true,
        formatter: '{b}: {d}%'
      },
      emphasis: {
        label: {
          show: true,
          fontSize: 16,
          fontWeight: 'bold'
        }
      },
      data: marketStats.value.map(item => ({
        name: item.market,
        value: item.total_cost
      }))
    }
  ]
}))

const timeChartOption = computed(() => {
  const periods = timeStats.value.map(item => item.period)
  const buyAmounts = timeStats.value.map(item => item.buy_amount)
  const sellAmounts = timeStats.value.map(item => item.sell_amount)

  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'shadow'
      }
    },
    legend: {
      data: ['买入金额', '卖出金额']
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '3%',
      containLabel: true
    },
    xAxis: {
      type: 'category',
      data: periods
    },
    yAxis: {
      type: 'value',
      axisLabel: {
        formatter: '¥{value}'
      }
    },
    series: [
      {
        name: '买入金额',
        type: 'bar',
        data: buyAmounts,
        itemStyle: {
          color: '#67C23A'
        }
      },
      {
        name: '卖出金额',
        type: 'bar',
        data: sellAmounts,
        itemStyle: {
          color: '#F56C6C'
        }
      }
    ]
  }
})

async function loadMarketStats() {
  try {
    const response = await api.getStatsByMarket()
    marketStats.value = response.data
  } catch (error) {
    ElMessage.error('加载市场统计失败')
  }
}

async function loadTimeStats() {
  try {
    const response = await api.getStatsByTime(timeGroupBy.value)
    timeStats.value = response.data
  } catch (error) {
    ElMessage.error('加载时间统计失败')
  }
}

async function loadProfitLoss() {
  try {
    const response = await api.getProfitLoss()
    profitLossData.value = response.data
  } catch (error) {
    ElMessage.error('加载盈亏分析失败')
  }
}

async function loadSummaryStats() {
  try {
    const response = await api.getSummary()
    summaryStats.value = response.data
  } catch (error) {
    console.error('加载统计摘要失败', error)
  }
}

async function loadAllData() {
  await Promise.all([
    loadMarketStats(),
    loadTimeStats(),
    loadProfitLoss(),
    loadSummaryStats(),
    loadHoldingsForPrice(),
    loadRealizedPnL(),
    loadDividendSummary(),
    loadTotalRealizedReturn(),
    loadExchangeRates()
  ])
  await calculatePerformance(false)
}

// 新增：性能统计方法
async function loadHoldingsForPrice() {
  try {
    const response = await api.getHoldings()
    priceInputData.value = response.data.map(h => ({
      symbol: h.symbol,
      name: h.name,
      market: h.market,
      avg_cost: parseFloat(h.avg_cost),
      // Use the database price if available; missing prices should stay empty.
      current_price: h.current_price && parseFloat(h.current_price) > 0
        ? parseFloat(h.current_price)
        : null,
      quantity: parseFloat(h.quantity)
    }))
  } catch (error) {
    ElMessage.error('加载持仓失败')
  }
}

async function calculatePerformance(showSuccess = true) {
  loading.value = true
  try {
    const prices = {}
    priceInputData.value.forEach(item => {
      if (item.current_price > 0) {
        prices[item.symbol] = item.current_price
      }
    })

    const response = await api.getCurrentHoldingsPerformance(prices)
    currentPerformance.value = response.data
    const accountResponse = await api.getAccountTotalReturn(prices)
    accountReturn.value = accountResponse.data
    showPriceDialog.value = false
    if (showSuccess) {
      ElMessage.success('计算完成')
    }
  } catch (error) {
    ElMessage.error('计算失败：' + (error.response?.data?.detail || error.message))
  } finally {
    loading.value = false
  }
}

async function savePrices() {
  saving.value = true
  try {
    // Build updates array with symbol, market, and price
    const updates = []
    priceInputData.value.forEach(item => {
      if (item.current_price > 0) {
        updates.push({
          symbol: item.symbol,
          market: item.market,
          price: item.current_price
        })
      }
    })

    if (updates.length === 0) {
      ElMessage.warning('没有可保存的价格')
      return
    }

    const response = await api.batchUpdatePrices(updates)
    const result = response.data
    ElMessage.success(`成功保存 ${result.success_count} 个价格`)

    if (result.failed_count > 0) {
      console.error('保存失败的项:', result.failed_list)
    }
  } catch (error) {
    ElMessage.error('保存失败：' + (error.response?.data?.detail || error.message))
  } finally {
    saving.value = false
  }
}

async function refreshPricesAndCalculate() {
  refreshing.value = true
  try {
    // Step 1: Refresh prices from API
    const refreshResponse = await api.refreshAllPrices()
    const refreshResult = refreshResponse.data

    // Step 2: Reload holdings with updated prices
    await loadHoldingsForPrice()

    // Step 3: Auto-calculate performance
    await calculatePerformance(false)

    // Show success message
    const successMsg = `股价已更新 (成功${refreshResult.success_count}只`
    const failedMsg = refreshResult.failed_count > 0 ? `, 失败${refreshResult.failed_count}只` : ''
    const skippedMsg = refreshResult.skipped_count > 0 ? `, 跳过${refreshResult.skipped_count}只` : ''
    ElMessage.success(successMsg + failedMsg + skippedMsg + ') 并完成计算')

    if (refreshResult.failed_list && refreshResult.failed_list.length > 0) {
      console.error('刷新失败的股票:', refreshResult.failed_list)
    }
  } catch (error) {
    ElMessage.error('刷新失败：' + (error.response?.data?.detail || error.message))
  } finally {
    refreshing.value = false
  }
}

async function loadRealizedPnL() {
  try {
    const response = await api.getRealizedPnLFifo()
    realizedPnL.value = response.data
  } catch (error) {
    console.error('加载已实现盈亏失败', error)
  }
}

async function loadDividendSummary() {
  try {
    const response = await api.getDividendSummary()
    dividendSummary.value = response.data
  } catch (error) {
    console.error('加载股息统计失败', error)
  }
}

async function loadTotalRealizedReturn() {
  try {
    const response = await api.getTotalRealizedReturn()
    totalRealizedReturn.value = response.data
  } catch (error) {
    console.error('加载综合已实现收益失败', error)
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

function getProfitColor(value) {
  return value >= 0 ? '#67C23A' : '#F56C6C'
}

onMounted(() => {
  loadAllData()
})
</script>

<style scoped>
.statistics-page {
  width: 100%;
}

.performance-cards {
  margin-bottom: 20px;
}

.stat-card {
  height: 100%;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.card-header > div,
.chart-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.refresh-button {
  margin-right: 8px;
}

.chart {
  width: 100%;
  height: 400px;
}

.stat-detail {
  margin-top: 16px;
  padding: 4px 0;
}

.stat-item {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 8px 0;
  font-size: 14px;
  color: var(--app-text-muted);
  border-bottom: 1px solid var(--app-border-soft);
}

.stat-item:last-of-type {
  border-bottom: 0;
}

.stat-item .value {
  color: var(--app-text);
  font-weight: 700;
  text-align: right;
}

.stat-item .value.success {
  color: var(--app-success);
}

.stat-item .value.danger {
  color: var(--app-danger);
}

.stat-note {
  color: var(--app-text-muted);
  font-size: 12px;
  line-height: 1.5;
  padding: 10px 12px;
  margin-top: 8px;
  background: var(--app-surface-muted);
  border-radius: 6px;
}

:deep(.el-statistic__head) {
  color: var(--app-text-muted);
  font-size: 13px;
}

:deep(.el-statistic__content) {
  font-weight: 760;
}

@media (max-width: 760px) {
  .card-header {
    align-items: flex-start;
    flex-direction: column;
  }

  .card-header > div,
  .chart-header {
    align-items: stretch;
    flex-direction: column;
    width: 100%;
  }

  .refresh-button {
    margin-right: 0;
  }

  .card-header .el-button {
    width: 100%;
    margin-left: 0;
  }

  .chart {
    height: 300px;
  }

  .stat-item {
    align-items: flex-start;
    flex-direction: column;
    gap: 4px;
  }

  .stat-item .value {
    max-width: 100%;
    text-align: left;
    overflow-wrap: anywhere;
  }

  .price-dialog-footer {
    grid-template-columns: 1fr;
  }
}
</style>
