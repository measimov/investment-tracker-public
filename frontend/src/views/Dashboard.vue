<template>
  <div class="dashboard">
    <el-row :gutter="20">
      <!-- Summary Cards -->
      <el-col :xs="24" :sm="12" :md="6">
        <el-card class="summary-card summary-card-primary">
          <div class="card-content">
            <div class="card-icon-wrap">
              <el-icon class="card-icon"><Wallet /></el-icon>
            </div>
            <div class="card-info">
              <div class="card-title">总投入</div>
              <div class="card-value">{{ formatCurrency(summary.total_invested) }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :md="6">
        <el-card class="summary-card summary-card-success">
          <div class="card-content">
            <div class="card-icon-wrap">
              <el-icon class="card-icon"><TrendCharts /></el-icon>
            </div>
            <div class="card-info">
              <div class="card-title">持仓数量</div>
              <div class="card-value">{{ summary.total_holdings }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :md="6">
        <el-card class="summary-card summary-card-warning">
          <div class="card-content">
            <div class="card-icon-wrap">
              <el-icon class="card-icon"><List /></el-icon>
            </div>
            <div class="card-info">
              <div class="card-title">交易记录</div>
              <div class="card-value">{{ summary.total_transactions }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :md="6">
        <el-card class="summary-card summary-card-danger">
          <div class="card-content">
            <div class="card-icon-wrap">
              <el-icon class="card-icon"><PieChart /></el-icon>
            </div>
            <div class="card-info">
              <div class="card-title">市场数量</div>
              <div class="card-value">{{ summary.markets_count }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" style="margin-top: 20px;">
      <!-- Market Distribution Chart -->
      <el-col :xs="24" :md="12">
        <el-card class="panel-card">
          <template #header>
            <div class="card-header">
              <span>市场分布</span>
            </div>
          </template>
          <v-chart :option="marketChartOption" class="dashboard-chart" />
        </el-card>
      </el-col>

      <!-- Recent Transactions -->
      <el-col :xs="24" :md="12">
        <el-card class="panel-card">
          <template #header>
            <div class="card-header">
              <span>最近交易</span>
              <el-button type="primary" text @click="$router.push('/transactions')">查看全部</el-button>
            </div>
          </template>
          <div class="responsive-table">
            <el-table :data="recentTransactions" style="width: 100%" max-height="300">
              <el-table-column prop="transaction_date" label="日期" width="100">
                <template #default="{ row }">
                  {{ formatDate(row.transaction_date) }}
                </template>
              </el-table-column>
              <el-table-column prop="symbol" label="代码" width="80" />
              <el-table-column prop="transaction_type" label="类型" width="70">
                <template #default="{ row }">
                  <el-tag :type="row.transaction_type === 'BUY' ? 'success' : 'danger'" size="small">
                    {{ row.transaction_type === 'BUY' ? '买入' : '卖出' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="quantity" label="数量" width="90">
                <template #default="{ row }">
                  {{ formatNumber(row.quantity, 2) }}
                </template>
              </el-table-column>
              <el-table-column prop="price" label="价格" width="90">
                <template #default="{ row }">
                  {{ formatNumber(row.price, 2) }}
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, onMounted, computed } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { PieChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import api from '../api'
import { formatNumber, formatDate, formatCurrency } from '../utils/helpers'

use([CanvasRenderer, PieChart, TitleComponent, TooltipComponent, LegendComponent])

const summary = ref({
  total_invested: 0,
  total_holdings: 0,
  total_transactions: 0,
  markets_count: 0
})

const marketStats = ref([])
const recentTransactions = ref([])

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
      radius: '50%',
      data: marketStats.value.map(item => ({
        name: item.market,
        value: item.total_cost
      })),
      emphasis: {
        itemStyle: {
          shadowBlur: 10,
          shadowOffsetX: 0,
          shadowColor: 'rgba(0, 0, 0, 0.5)'
        }
      }
    }
  ]
}))

async function loadData() {
  try {
    const [summaryRes, marketRes, transactionsRes] = await Promise.all([
      api.getSummary(),
      api.getStatsByMarket(),
      api.getTransactions({ limit: 10 })
    ])

    summary.value = summaryRes.data
    marketStats.value = marketRes.data
    recentTransactions.value = transactionsRes.data
  } catch (error) {
    console.error('Failed to load dashboard data:', error)
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
  border-left: 3px solid var(--card-accent);
}

.summary-card-primary {
  --card-accent: var(--app-primary);
  --card-soft: var(--app-primary-soft);
}

.summary-card-success {
  --card-accent: var(--app-success);
  --card-soft: #ecfdf5;
}

.summary-card-warning {
  --card-accent: var(--app-warning);
  --card-soft: #fffbeb;
}

.summary-card-danger {
  --card-accent: var(--app-danger);
  --card-soft: #fef2f2;
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
  color: var(--card-accent);
  background: var(--card-soft);
  border-radius: 8px;
}

.card-icon {
  font-size: 28px;
}

.card-info {
  flex: 1;
}

.card-title {
  font-size: 14px;
  color: var(--app-text-muted);
  margin-bottom: 8px;
}

.card-value {
  font-size: 26px;
  font-weight: 760;
  color: var(--app-text);
  line-height: 1.1;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.panel-card {
  min-height: 210px;
}

.dashboard-chart {
  width: 100%;
  height: 300px;
}

:deep(.el-table__empty-block) {
  min-height: 88px;
}

@media (max-width: 640px) {
  .card-content {
    align-items: flex-start;
  }

  .card-value {
    font-size: 23px;
    overflow-wrap: anywhere;
  }

  .dashboard-chart {
    height: 260px;
  }
}
</style>
