<template>
  <div class="account-data-page">
    <section class="page-intro">
      <div>
        <h1>账户数据</h1>
        <p>把券商账户、现金活动和月末核对放在一起，先保证数据可信，再看收益。</p>
      </div>
      <el-button :icon="Refresh" :loading="refreshing" @click="refreshAll">刷新数据</el-button>
    </section>

    <div class="summary-grid">
      <div class="summary-item">
        <span>券商账户</span>
        <strong>{{ accounts.length }}</strong>
        <small>{{ activeAccountCount }} 个启用</small>
      </div>
      <div class="summary-item">
        <span>现金事件</span>
        <strong>{{ cashEvents.length }}</strong>
        <small>入金、出金及账户费用</small>
      </div>
      <div class="summary-item">
        <span>最近导入</span>
        <strong class="summary-date">{{ latestBatchDate }}</strong>
        <small>{{ importBatches.length }} 个可追溯批次</small>
      </div>
      <div class="summary-item">
        <span>月末核对</span>
        <strong>{{ reconciledCount }}/{{ snapshots.length }}</strong>
        <small>持仓数量一致；自动快照不核验现金</small>
      </div>
    </div>

    <el-card shadow="never" class="content-card">
      <el-tabs v-model="activeTab" class="data-tabs">
        <el-tab-pane name="accounts">
          <template #label>
            <span class="tab-label"><Wallet />账户</span>
          </template>
          <AccountsTab :accounts="accounts" :loading="loading.accounts" :reload="loadAccounts" />
        </el-tab-pane>

        <el-tab-pane name="cash">
          <template #label>
            <span class="tab-label"><Coin />现金事件</span>
          </template>
          <CashEventsTab
            :cash-events="cashEvents"
            :accounts="accounts"
            :loading="loading.cash"
            :reload="loadCashEvents"
          />
        </el-tab-pane>

        <el-tab-pane name="imports">
          <template #label>
            <span class="tab-label"><Files />导入批次</span>
          </template>
          <ImportBatchesTab
            :import-batches="importBatches"
            :accounts="accounts"
            :loading="loading.batches"
          />
        </el-tab-pane>

        <el-tab-pane name="reconciliation">
          <template #label>
            <span class="tab-label"><CircleCheck />月末核对</span>
          </template>
          <ReconciliationTab
            :snapshots="snapshots"
            :accounts="accounts"
            :loading="loading.snapshots"
            :reload="loadSnapshots"
          />
        </el-tab-pane>

        <el-tab-pane name="exclusions">
          <template #label>
            <span class="tab-label"><Remove />特例规则</span>
          </template>
          <SecurityRulesTab ref="rulesTab" />
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, type Ref } from 'vue'
import { ElMessage } from 'element-plus'
import { CircleCheck, Coin, Files, Refresh, Remove, Wallet } from '@element-plus/icons-vue'
import api from '@/api'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { formatDate } from '@/utils/helpers'
import AccountsTab from './account-data/AccountsTab.vue'
import CashEventsTab from './account-data/CashEventsTab.vue'
import ImportBatchesTab from './account-data/ImportBatchesTab.vue'
import ReconciliationTab from './account-data/ReconciliationTab.vue'
import SecurityRulesTab from './account-data/SecurityRulesTab.vue'
import type { AccountRow, CashEventRow, ImportBatchRow, SnapshotRow } from './account-data/shared'

// 壳层职责（issue #140）：页头汇总 + tab 骨架 + 汇总卡消费的四类数据的
// 装载。各 tab 的表格/弹窗/CRUD 在 account-data/ 下的页面私有子组件里；
// 特例规则不进汇总卡，其数据完全归子组件所有（refreshAll 经 ref 触发）。
const activeTab = ref('accounts')
const accounts = ref<AccountRow[]>([])
const cashEvents = ref<CashEventRow[]>([])
const importBatches = ref<ImportBatchRow[]>([])
const snapshots = ref<SnapshotRow[]>([])
const refreshing = ref(false)
const loading = reactive({
  accounts: false,
  cash: false,
  batches: false,
  snapshots: false
})
const rulesTab = ref<InstanceType<typeof SecurityRulesTab>>()

const activeAccountCount = computed(
  () => accounts.value.filter((item) => item.is_active !== false).length
)
const reconciledCount = computed(
  () => snapshots.value.filter((item) => String(item.status).toUpperCase() === 'MATCHED').length
)
const latestBatchDate = computed(() => {
  const dates = importBatches.value
    .map((item) => item.created_at || item.imported_at)
    .filter(Boolean)
    .sort()
  return dates.length ? formatDate(dates[dates.length - 1]) : '尚无'
})

// 列表加载工厂：loading 键 + 目标 ref + 拉取函数 + 失败文案，形状完全一致
// fetcher 保留 Axios 响应泛型（PR #172 复审）：此前降成 Promise<unknown> 再
// 用 rowsFrom<T> 断言回来，后端 schema 变化会从 typecheck 手里溜走
function makeLoader<T>(
  loadingKey: keyof typeof loading,
  target: Ref<T[]>,
  fetcher: () => Promise<{ data: T[] }>,
  failureMessage: string
) {
  return async () => {
    loading[loadingKey] = true
    try {
      target.value = (await fetcher()).data
    } catch (error) {
      ElMessage.error(getApiErrorMessage(error, failureMessage))
    } finally {
      loading[loadingKey] = false
    }
  }
}

const loadAccounts = makeLoader('accounts', accounts, () => api.getBrokerAccounts(), '账户加载失败')
const loadCashEvents = makeLoader(
  'cash',
  cashEvents,
  () => api.getCashEvents({ limit: 1000 }),
  '现金事件加载失败'
)
const loadImportBatches = makeLoader(
  'batches',
  importBatches,
  () => api.getImportBatches({ limit: 1000 }),
  '导入批次加载失败'
)
const loadSnapshots = makeLoader(
  'snapshots',
  snapshots,
  () => api.getReconciliationSnapshots({ limit: 1000 }),
  '月末核对加载失败'
)

async function refreshAll() {
  refreshing.value = true
  await Promise.all([
    loadAccounts(),
    loadCashEvents(),
    loadImportBatches(),
    loadSnapshots(),
    rulesTab.value?.reload() ?? Promise.resolve()
  ])
  refreshing.value = false
}

onMounted(refreshAll)
</script>

<style scoped>
.account-data-page {
  display: grid;
  gap: 20px;
}

.page-intro {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
}

.page-intro h1 {
  margin: 0;
  color: var(--app-text);
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -0.02em;
}

.page-intro p:last-child {
  margin: 7px 0 0;
  color: var(--app-text-soft);
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}

.summary-item {
  display: grid;
  gap: 5px;
  min-width: 0;
  padding: 18px 20px;
  border: 1px solid var(--app-border-soft);
  border-radius: var(--app-radius);
  background: var(--app-surface);
  box-shadow: var(--app-shadow-sm);
}

.summary-item span,
.summary-item small {
  color: var(--app-text-soft);
}

.summary-item strong {
  color: var(--app-text);
  font-size: 22px;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
}

.summary-item .summary-date {
  overflow: hidden;
  font-size: 16px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.content-card :deep(.el-card__body) {
  padding-top: 8px;
}

.data-tabs :deep(.el-tabs__header) {
  margin-bottom: 22px;
}

.tab-label {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}

.tab-label svg {
  width: 16px;
}

@media (max-width: 900px) {
  .summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 640px) {
  .account-data-page {
    gap: 16px;
  }

  .page-intro {
    align-items: stretch;
    flex-direction: column;
    gap: 12px;
  }

  .page-intro > .el-button {
    width: 100%;
  }

  .summary-grid {
    gap: 10px;
  }

  .summary-item {
    padding: 14px;
  }

  .summary-item strong {
    font-size: 21px;
  }

  .summary-item small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .content-card :deep(.el-card__body) {
    padding: 8px 14px 16px;
  }

  .data-tabs :deep(.el-tabs__nav-wrap) {
    overflow-x: auto;
  }
}
</style>
