<template>
  <div class="all-holdings-page">
    <el-card>
      <template #header>
        <div class="page-header">
          <span>全部持仓查看</span>
        </div>
      </template>

      <!-- User Selector -->
      <div class="user-selector">
        <el-form :inline="true">
          <el-form-item label="选择用户">
            <el-select
              v-model="selectedUserId"
              placeholder="请选择用户"
              clearable
              @change="handleUserChange"
              class="user-select"
            >
              <el-option label="所有用户 (汇总)" :value="null" />
              <el-option
                v-for="user in users"
                :key="user.id"
                :label="`${user.username} (${user.email})`"
                :value="user.id"
              />
            </el-select>
          </el-form-item>
        </el-form>
      </div>

      <!-- Summary Statistics -->
      <div class="summary-stats" v-if="holdings.length > 0">
        <el-row :gutter="20">
          <el-col :xs="12" :md="6">
            <el-statistic title="持仓品种数" :value="holdings.length" />
          </el-col>
          <el-col :xs="12" :md="6">
            <el-statistic title="总持仓量" :value="totalQuantity" :precision="2" />
          </el-col>
          <el-col :xs="12" :md="6">
            <el-statistic title="总成本 (CNY)" :value="totalCost" :precision="2" />
          </el-col>
          <el-col :xs="12" :md="6">
            <el-statistic title="总市值 (CNY)" :value="totalValue" :precision="2" />
          </el-col>
        </el-row>
      </div>

      <!-- Holdings Table -->
      <div class="responsive-table holdings-table">
        <el-table :data="holdings" v-loading="loading" stripe>
          <el-table-column prop="user_id" label="用户ID" width="80" v-if="selectedUserId === null" />
          <el-table-column prop="username" label="用户名" width="120" v-if="selectedUserId === null" />
          <el-table-column prop="symbol" label="代码" width="100" />
          <el-table-column prop="name" label="名称" width="120" />
          <el-table-column prop="market" label="市场" width="100" />
          <el-table-column prop="quantity" label="持仓量" width="120" align="right">
            <template #default="{ row }">
              {{ formatNumber(row.quantity, 4) }}
            </template>
          </el-table-column>
          <el-table-column prop="avg_cost" label="平均成本" width="120" align="right">
            <template #default="{ row }">
              {{ formatNumber(row.avg_cost, 4) }}
            </template>
          </el-table-column>
          <el-table-column prop="current_price" label="当前价格" width="120" align="right">
            <template #default="{ row }">
              {{ formatNumber(row.current_price, 4) }}
            </template>
          </el-table-column>
          <el-table-column prop="currency" label="币种" width="80" />
          <el-table-column label="总成本" width="120" align="right">
            <template #default="{ row }">
              {{ formatNumber(row.total_cost, 2) }}
            </template>
          </el-table-column>
          <el-table-column label="当前市值" width="120" align="right">
            <template #default="{ row }">
              {{ formatNumber(row.quantity * (row.current_price || 0), 2) }}
            </template>
          </el-table-column>
          <el-table-column label="盈亏" width="120" align="right">
            <template #default="{ row }">
              <span :class="getProfitClass(row)">
                {{ formatNumber(calculateProfit(row), 2) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="盈亏率" width="100" align="right">
            <template #default="{ row }">
              <span :class="getProfitClass(row)">
                {{ formatPercent(calculateProfitPercent(row)) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="updated_at" label="最后更新" width="180" sortable>
            <template #default="{ row }">
              {{ formatDateTime(row.updated_at) }}
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api'

const loading = ref(false)
const users = ref([])
const holdings = ref([])
const selectedUserId = ref(null)

const totalQuantity = computed(() => {
  return holdings.value.reduce((sum, h) => sum + parseFloat(h.quantity || 0), 0)
})

const totalCost = computed(() => {
  return holdings.value.reduce((sum, h) => {
    const cost = parseFloat(h.total_cost || 0)
    return sum + cost
  }, 0)
})

const totalValue = computed(() => {
  return holdings.value.reduce((sum, h) => {
    const value = parseFloat(h.quantity || 0) * parseFloat(h.current_price || 0)
    return sum + value
  }, 0)
})

function formatNumber(value, precision = 2) {
  if (value === null || value === undefined) return '0.00'
  const numberValue = parseFloat(value)
  if (Number.isNaN(numberValue)) return '0.00'
  return numberValue.toLocaleString('zh-CN', {
    minimumFractionDigits: precision,
    maximumFractionDigits: precision
  })
}

function formatPercent(value) {
  if (value === null || value === undefined || isNaN(value)) return '0.00%'
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`
}

function formatDateTime(dateString) {
  if (!dateString) return ''
  const date = new Date(dateString)
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  })
}

function calculateProfit(row) {
  const cost = parseFloat(row.total_cost || 0)
  const value = parseFloat(row.quantity || 0) * parseFloat(row.current_price || 0)
  return value - cost
}

function calculateProfitPercent(row) {
  const cost = parseFloat(row.avg_cost || 0)
  const currentPrice = parseFloat(row.current_price || 0)
  if (cost === 0) return 0
  return ((currentPrice - cost) / cost) * 100
}

function getProfitClass(row) {
  const profit = calculateProfit(row)
  if (profit > 0) return 'profit-positive'
  if (profit < 0) return 'profit-negative'
  return ''
}

async function loadUsers() {
  try {
    const response = await api.getUsers()
    users.value = response.data.filter(user => user.is_active)
  } catch (error) {
    ElMessage.error('加载用户列表失败')
  }
}

async function loadHoldings() {
  loading.value = true
  try {
    let response
    if (selectedUserId.value === null) {
      // Load all holdings (aggregate view)
      response = await api.getAllHoldingsAdmin()
    } else {
      // Load holdings for specific user
      response = await api.getUserHoldingsAdmin(selectedUserId.value)
    }
    holdings.value = response.data
  } catch (error) {
    ElMessage.error('加载持仓数据失败')
    holdings.value = []
  } finally {
    loading.value = false
  }
}

function handleUserChange() {
  loadHoldings()
}

onMounted(() => {
  loadUsers()
  loadHoldings()
})
</script>

<style scoped>
.all-holdings-page {
  width: 100%;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.user-selector {
  margin-bottom: 20px;
}

.user-select {
  width: 250px;
}

.summary-stats {
  padding: 20px;
  background-color: #f5f7fa;
  border-radius: 4px;
  margin-bottom: 20px;
}

.holdings-table {
  margin-top: 20px;
}

.profit-positive {
  color: #67c23a;
  font-weight: bold;
}

.profit-negative {
  color: #f56c6c;
  font-weight: bold;
}

@media (max-width: 900px) {
  .user-select {
    width: 100%;
  }

  .summary-stats {
    padding: 14px;
  }

  .summary-stats :deep(.el-col) {
    margin-bottom: 12px;
  }
}
</style>
