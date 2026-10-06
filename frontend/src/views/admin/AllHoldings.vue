<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { NAlert, NButton } from 'naive-ui'
import api from '@/api'
import { formatCurrency, profitColor } from '@/utils/helpers'
import { showApiError } from '@/utils/showApiError'
import { useExchangeRates } from '@/composables/useExchangeRates'
import { useLatestRequest } from '@/composables/useLatestRequest'
import FinancialStatistic from '@/components/FinancialStatistic.vue'
import AdminHoldingsTable from './holdings/AdminHoldingsTable.vue'
import type { AdminHolding, User } from '@/types'
import { summarizeAdminHoldings } from './adminHoldings'

const ALL_USERS = 0
const loading = ref(false),
  loadError = ref(false)
const usersLoading = ref(false),
  usersLoaded = ref(false),
  usersError = ref(false)
const users = ref<User[]>([]),
  usersReturned = ref(0),
  holdings = ref<AdminHolding[]>([])
const selectedUserId = ref<number>(ALL_USERS)
const loadedUserId = ref<number | null>(null)
const holdingsRequest = useLatestRequest(),
  usersRequest = useLatestRequest()
const { loadExchangeRates, convertToCNY, loadFailed: ratesFailed } = useExchangeRates()
const hasLoaded = computed(() => loadedUserId.value !== null)
const scopeOutdated = computed(() => hasLoaded.value && loadedUserId.value !== selectedUserId.value)
const summary = computed(() => summarizeAdminHoldings(holdings.value, convertToCNY))
const costCovered = computed(() => holdings.value.length - summary.value.missingRateCount)
const valueCovered = computed(() => costCovered.value - summary.value.unpricedCount)
const knownCost = computed(
  () => hasLoaded.value && (!holdings.value.length || costCovered.value > 0)
)
const knownValue = computed(
  () => hasLoaded.value && (!holdings.value.length || valueCovered.value > 0)
)
const emptyDescription = computed(() =>
  loading.value
    ? '正在加载持仓记录'
    : !hasLoaded.value
      ? '持仓记录尚未加载成功'
      : '该范围暂无持仓记录'
)
function userLabel(user: User) {
  return user.email ? `${user.username} (${user.email})` : user.username
}
function scopeLabel(id: number | null) {
  if (id === null) return '尚未确认'
  if (id === ALL_USERS) return '所有用户（汇总）'
  const username =
    users.value.find((user) => user.id === id)?.username ||
    holdings.value.find((row) => row.user_id === id)?.username
  return username || `用户 ${id}`
}
const summaryNotes = computed(() => {
  const notes: string[] = [],
    s = summary.value
  if (s.missingRateCount > 0)
    notes.push(
      `缺少 ${s.missingRateCurrencies.join('、')} 对人民币的汇率，${s.missingRateCount} 个持仓未计入汇总`
    )
  if (s.unpricedCount > 0)
    notes.push(`${s.unpricedCount} 个持仓缺少现价，只计入总成本，未计入市值与盈亏`)
  return notes
})
async function loadUsers() {
  const request = usersRequest.begin()
  usersLoading.value = true
  usersError.value = false
  try {
    const response = await api.getUsers()
    if (!usersRequest.isCurrent(request)) return
    usersReturned.value = response.data.length
    users.value = response.data.filter((user) => user.is_active)
    usersLoaded.value = true
  } catch (error) {
    if (!usersRequest.isCurrent(request)) return
    usersError.value = true
    showApiError(error, '加载用户列表失败')
  } finally {
    if (usersRequest.isCurrent(request)) usersLoading.value = false
  }
}
async function loadHoldings() {
  const token = holdingsRequest.begin(),
    scope = selectedUserId.value
  loading.value = true
  loadError.value = false
  try {
    const response =
      scope === ALL_USERS ? await api.getAllHoldingsAdmin() : await api.getUserHoldingsAdmin(scope)
    if (!holdingsRequest.isCurrent(token)) return
    holdings.value = response.data
    loadedUserId.value = scope
  } catch (error) {
    if (!holdingsRequest.isCurrent(token)) return
    loadError.value = true
    showApiError(error, '加载持仓数据失败')
  } finally {
    if (holdingsRequest.isCurrent(token)) loading.value = false
  }
}
onMounted(() => {
  loadExchangeRates()
  loadUsers()
  loadHoldings()
})
</script>
<template>
  <div class="all-holdings-page">
    <header class="page-heading">
      <div>
        <h1 class="page-title">查看所有持仓</h1>
        <p class="page-intro page-description">按用户查看原币持仓明细，汇总统一折为人民币。</p>
      </div>
      <NButton :loading="loading" aria-label="重新加载管理员持仓" @click="loadHoldings"
        >重新加载</NButton
      >
    </header>
    <div class="user-selector">
      <label id="admin-holdings-user-label">选择用户</label>
      <el-select
        v-model="selectedUserId"
        aria-label="选择用户"
        placeholder="请选择用户"
        clearable
        :fit-input-width="true"
        :value-on-clear="ALL_USERS"
        @change="loadHoldings"
        class="user-select"
        :loading="usersLoading"
      >
        <el-option label="所有用户（汇总）" :value="ALL_USERS" />
        <el-option
          v-for="user in users"
          :key="user.id"
          :label="userLabel(user)"
          :value="user.id"
          class="user-option"
        />
      </el-select>
      <NButton
        text
        type="primary"
        :loading="usersLoading"
        aria-label="重新加载用户目录"
        @click="loadUsers"
        >重新加载用户目录</NButton
      >
    </div>
    <NAlert
      v-if="usersError"
      type="warning"
      :show-icon="false"
      title="用户目录加载失败"
      class="read-alert"
      >{{
        usersLoaded
          ? '保留上次成功的活跃用户目录，尚未确认最新名单。'
          : '尚未确认活跃用户目录，所有用户汇总仍可独立读取。'
      }}<NButton text type="primary" aria-label="重试用户目录" @click="loadUsers"
        >重试加载</NButton
      ></NAlert
    >
    <p v-else-if="!usersLoaded" class="read-note">
      {{ usersLoading ? '正在读取活跃用户目录。' : '用户目录尚未加载成功。' }}
    </p>
    <p v-if="usersReturned >= 100" class="read-note">
      用户目录取自当前最多 100 条用户记录，仅列出其中的活跃用户，不代表全部用户。
    </p>
    <NAlert
      v-if="loadError"
      type="warning"
      :show-icon="false"
      title="持仓记录加载失败"
      class="read-alert"
      >{{
        hasLoaded
          ? `保留上次成功范围：${scopeLabel(loadedUserId)}。当前选择的最新结果尚未确认。`
          : '尚未确认当前范围的持仓，请重试。'
      }}<NButton text type="primary" aria-label="重试管理员持仓" @click="loadHoldings"
        >重试加载</NButton
      ></NAlert
    >
    <p v-if="scopeOutdated" class="read-note" role="status">
      当前选择：{{ scopeLabel(selectedUserId) }}；下方仍为上次成功范围：{{
        scopeLabel(loadedUserId)
      }}。{{ loading ? '正在读取当前选择。' : '当前选择尚未成功加载。' }}
    </p>
    <p v-else-if="loading && hasLoaded" class="read-note" role="status">
      正在重新加载，下方为上次成功的持仓。
    </p>
    <NAlert
      v-if="ratesFailed"
      type="warning"
      :show-icon="false"
      title="汇率加载失败"
      class="read-alert"
      >外币折算暂使用已有汇率；缺汇率记录不计入人民币汇总，尚未确认最新汇率。<NButton
        text
        type="primary"
        aria-label="重试汇率读取"
        @click="loadExchangeRates"
        >重试加载汇率</NButton
      ></NAlert
    >
    <section class="holdings-summary" aria-label="管理员持仓汇总">
      <div class="section-heading">
        <h2>持仓概览</h2>
        <span>{{ scopeLabel(loadedUserId) }}</span>
      </div>
      <div class="summary-stats">
        <FinancialStatistic
          title="持仓记录数"
          :value="hasLoaded ? holdings.length : null"
          :precision="0"
        />
        <FinancialStatistic
          title="总成本（CNY）"
          :value="knownCost ? summary.totalCostCNY : null"
          :formatter="(value) => formatCurrency(value, 'CNY')"
        />
        <FinancialStatistic
          title="总市值（CNY）"
          :value="knownValue ? summary.totalValueCNY : null"
          :formatter="(value) => formatCurrency(value, 'CNY')"
        />
        <FinancialStatistic
          title="浮动盈亏（CNY）"
          :value="knownValue ? summary.profitCNY : null"
          :formatter="(value) => formatCurrency(value, 'CNY')"
          :value-style="{ color: profitColor(knownValue ? summary.profitCNY : null) }"
        />
      </div>
      <p class="read-note">
        持仓按账户记录计数。成本含有汇率的记录；市值与盈亏仅含有现价及汇率的同一批记录。
      </p>
      <NAlert
        v-for="note in summaryNotes"
        :key="note"
        type="warning"
        :show-icon="false"
        class="read-alert"
        >{{ note }}</NAlert
      >
    </section>
    <section class="holdings-detail" aria-label="管理员持仓明细">
      <div class="section-heading">
        <h2>持仓明细</h2>
        <span>{{ scopeLabel(loadedUserId) }}</span>
      </div>
      <AdminHoldingsTable
        :rows="holdings"
        :loading="loading"
        :all-users="loadedUserId === ALL_USERS"
        :empty-description="emptyDescription"
      />
    </section>
  </div>
</template>
<style scoped>
.all-holdings-page {
  width: 100%;
  min-width: 0;
}
.page-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 28px;
}
.user-selector {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 20px;
}
.user-selector label {
  font-size: 13px;
  color: var(--app-text-muted);
}
.user-select {
  width: 360px;
  max-width: 100%;
  --el-text-color-placeholder: var(--app-text-soft);
}
.user-option {
  height: auto;
  min-height: 34px;
  padding-block: 8px;
  white-space: normal;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.section-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 18px;
}
h2 {
  font-size: 20px;
  font-weight: 600;
  margin: 0;
}
.section-heading > span {
  color: var(--app-text-muted);
  font-size: 13px;
  overflow-wrap: anywhere;
}
.holdings-summary {
  margin: 24px 0;
}
.summary-stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 24px;
  border-block: 1px solid var(--app-border);
  padding-block: 20px;
}
.read-note {
  color: var(--app-text-muted);
  font-size: 13px;
  line-height: 1.7;
  margin: 12px 0;
}
.read-alert {
  margin: 12px 0;
}
.holdings-detail {
  min-width: 0;
}
@media (max-width: 1100px) {
  .summary-stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 640px) {
  .page-heading {
    align-items: flex-start;
    flex-wrap: wrap;
    gap: 16px;
    margin-bottom: 24px;
  }
  .page-heading :deep(.n-button),
  .user-selector :deep(.n-button),
  .read-alert :deep(.n-button),
  .user-select :deep(.el-select__wrapper) {
    min-height: 44px;
  }
  .user-select {
    width: 100%;
  }
  .summary-stats {
    gap: 20px;
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (min-width: 1025px) {
  .page-heading {
    margin-bottom: 16px;
  }
  .user-selector {
    margin-bottom: 12px;
  }
  .section-heading {
    margin-bottom: 12px;
  }
  h2 {
    font-size: 18px;
  }
  .holdings-summary {
    margin: 16px 0;
  }
  .summary-stats {
    gap: 20px;
  }
}
</style>
