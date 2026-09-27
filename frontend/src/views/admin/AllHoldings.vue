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
            <!--
              「所有用户」用哨兵 0 而不是 null：EP 2.13 把 null 当空值，初始会显示占位符，
              清空后还会请求 /holdings/admin/users/undefined（#219）。清空即回到汇总。
            -->
            <el-select
              v-model="selectedUserId"
              placeholder="请选择用户"
              clearable
              :value-on-clear="ALL_USERS"
              @change="handleUserChange"
              class="user-select"
            >
              <el-option label="所有用户（汇总）" :value="ALL_USERS" />
              <el-option
                v-for="user in users"
                :key="user.id"
                :label="userLabel(user)"
                :value="user.id"
              />
            </el-select>
          </el-form-item>
        </el-form>
      </div>

      <!-- Summary Statistics：各币种先折人民币再相加；缺汇率/缺现价的行不计入并提示 -->
      <div class="summary-stats" v-if="holdings.length > 0">
        <el-row :gutter="20">
          <el-col :xs="12" :md="6">
            <el-statistic title="持仓品种数" :value="holdings.length" />
          </el-col>
          <el-col :xs="12" :md="6">
            <el-statistic title="总成本 (CNY)" :value="summary.totalCostCNY" :precision="2" />
          </el-col>
          <el-col :xs="12" :md="6">
            <el-statistic title="总市值 (CNY)" :value="summary.totalValueCNY" :precision="2" />
          </el-col>
          <el-col :xs="12" :md="6">
            <el-statistic title="浮动盈亏 (CNY)" :value="summary.profitCNY" :precision="2" />
          </el-col>
        </el-row>
        <el-alert
          v-for="note in summaryNotes"
          :key="note"
          :title="note"
          type="warning"
          :closable="false"
          show-icon
          class="summary-note"
        />
      </div>

      <!-- Holdings Table -->
      <div class="responsive-table holdings-table">
        <el-table :data="holdings" v-loading="loading" stripe>
          <el-table-column
            prop="user_id"
            label="用户ID"
            width="80"
            v-if="selectedUserId === ALL_USERS"
          />
          <el-table-column
            prop="username"
            label="用户名"
            width="120"
            v-if="selectedUserId === ALL_USERS"
          />
          <el-table-column prop="symbol" label="代码" min-width="90" />
          <el-table-column prop="name" label="名称" min-width="120" show-overflow-tooltip />
          <el-table-column prop="market" label="市场" width="80" />
          <el-table-column prop="quantity" label="持仓量" min-width="105" align="right">
            <template #default="{ row }">
              {{ formatQuantity(row.quantity) }}
            </template>
          </el-table-column>
          <el-table-column prop="avg_cost" label="平均成本" min-width="105" align="right">
            <template #default="{ row }">
              {{ formatPrice(row.avg_cost) }}
            </template>
          </el-table-column>
          <el-table-column prop="current_price" label="当前价格" min-width="105" align="right">
            <template #default="{ row }">
              {{ formatPrice(row.current_price) }}
            </template>
          </el-table-column>
          <el-table-column prop="currency" label="币种" width="70" />
          <el-table-column label="总成本" min-width="110" align="right">
            <template #default="{ row }">
              {{ formatNumber(row.total_cost, 2) }}
            </template>
          </el-table-column>
          <el-table-column label="当前市值" min-width="110" align="right">
            <template #default="{ row }">
              {{ formatNumber(rowMarketValue(row), 2) }}
            </template>
          </el-table-column>
          <el-table-column label="盈亏" min-width="105" align="right">
            <template #default="{ row }">
              <span :class="profitClass(row)">
                {{ formatNumber(rowProfit(row), 2) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="盈亏率" min-width="90" align="right">
            <template #default="{ row }">
              <span :class="profitClass(row)">
                {{ formatPercent(rowProfitPercent(row)) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="updated_at" label="最后更新" min-width="150" sortable>
            <template #default="{ row }">
              {{ formatDateTime(row.updated_at) }}
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import api from '../../api'
import {
  formatDateTime,
  formatNumber,
  formatPercent,
  formatPrice,
  formatQuantity
} from '../../utils/helpers'
import { showApiError } from '../../utils/showApiError'
import { useExchangeRates } from '../../composables/useExchangeRates'
import type { AdminHolding, User } from '../../types'
import {
  rowMarketValue,
  rowProfit,
  rowProfitPercent,
  summarizeAdminHoldings
} from './adminHoldings'

// 后端 schema 为准（PR #172 复审：此前手写并对 getUsers() 显式强转）
type AdminUser = User
type AdminHoldingRow = AdminHolding

/** 「所有用户（汇总）」的哨兵值：用户 id 从 1 起，0 不会与真实用户冲突 */
const ALL_USERS = 0

const loading = ref(false)
const users = ref<AdminUser[]>([])
const holdings = ref<AdminHoldingRow[]>([])
const selectedUserId = ref<number>(ALL_USERS)

const { loadExchangeRates, convertToCNY } = useExchangeRates()

const summary = computed(() => summarizeAdminHoldings(holdings.value, convertToCNY))

const summaryNotes = computed(() => {
  const notes: string[] = []
  const s = summary.value
  if (s.missingRateCount > 0) {
    notes.push(
      `缺少 ${s.missingRateCurrencies.join('、')} 对人民币的汇率，${s.missingRateCount} 个持仓未计入汇总`
    )
  }
  if (s.unpricedCount > 0) {
    notes.push(`${s.unpricedCount} 个持仓缺少当前价格，只计入总成本，未计入市值与盈亏`)
  }
  return notes
})

// email 可为空：不再拼出「demo (null)」
function userLabel(user: AdminUser): string {
  return user.email ? `${user.username} (${user.email})` : user.username
}

function profitClass(row: AdminHoldingRow): string {
  const profit = rowProfit(row)
  if (profit === null || profit === 0) return ''
  return profit > 0 ? 'profit-positive' : 'profit-negative'
}

async function loadUsers() {
  try {
    const response = await api.getUsers()
    users.value = response.data.filter((user) => user.is_active)
  } catch (error) {
    showApiError(error, '加载用户列表失败')
  }
}

async function loadHoldings() {
  loading.value = true
  try {
    const response =
      selectedUserId.value === ALL_USERS
        ? await api.getAllHoldingsAdmin()
        : await api.getUserHoldingsAdmin(selectedUserId.value)
    holdings.value = response.data
  } catch (error) {
    showApiError(error, '加载持仓数据失败')
    holdings.value = []
  } finally {
    loading.value = false
  }
}

function handleUserChange() {
  loadHoldings()
}

onMounted(() => {
  loadExchangeRates()
  loadUsers()
  loadHoldings()
})
</script>

<style scoped>
.all-holdings-page {
  width: 100%;
}

.user-selector {
  margin-bottom: 20px;
}

.user-select {
  width: 250px;
}

.summary-stats {
  padding: 20px;
  background-color: var(--app-surface-secondary);
  border-radius: var(--app-radius);
  margin-bottom: 20px;
}

.holdings-table {
  margin-top: 20px;
}

.profit-positive {
  color: var(--app-success);
  font-weight: 600;
}

.profit-negative {
  color: var(--app-danger);
  font-weight: 600;
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

<!-- #219 汇总提示的间距（单独成块：主样式块由扁平主题改造负责） -->
<style scoped>
.summary-note {
  margin-top: 12px;
}
</style>
