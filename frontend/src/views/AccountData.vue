<template>
  <div class="account-data-page">
    <section class="page-intro">
      <div>
        <h1 class="page-title">账户数据</h1>
        <p class="page-description">
          把券商账户、现金活动和月末核对放在一起，先保证数据可信，再看收益。
        </p>
      </div>
      <NButton :loading="refreshing" aria-label="刷新账户数据" @click="refreshAll"
        >刷新数据</NButton
      >
    </section>

    <div class="summary-grid">
      <div class="summary-item">
        <span>券商账户</span>
        <strong>{{ accountsLoaded ? countLabel(accounts) : '—' }}</strong>
        <small
          >{{
            accountsLoaded
              ? `${activeAccountCount} 个启用${isAtListLimit(accounts) ? '（仅已加载账户）' : ''}`
              : '尚未确认账户'
          }}{{ accountsState.status !== 'ready' && accountsLoaded ? ' · 上次成功数据' : '' }}</small
        >
      </div>
      <div class="summary-item">
        <span>现金事件</span>
        <strong>{{ loaded.cash ? countLabel(cashEvents) : '—' }}</strong>
        <small
          >入金、出金及账户费用{{
            (loading.cash || errors.cash) && loaded.cash ? ' · 上次成功数据' : ''
          }}</small
        >
      </div>
      <div class="summary-item">
        <span>最近导入</span>
        <strong class="summary-date">{{ latestBatchDate }}</strong>
        <small
          >{{ loaded.batches ? countLabel(importBatches) : '—' }} 个可追溯批次{{
            (loading.batches || errors.batches) && loaded.batches ? ' · 上次成功数据' : ''
          }}</small
        >
      </div>
      <div class="summary-item">
        <span>月末核对</span>
        <strong>{{
          accountsLoaded && loaded.snapshots
            ? `${reconciliation.matched}/${reconciliation.total}`
            : '—'
        }}</strong>
        <small
          >个账户最近一次核对持仓一致；自动快照不核验现金{{
            (accountsState.status !== 'ready' || loading.snapshots || errors.snapshots) &&
            accountsLoaded &&
            loaded.snapshots
              ? ' · 上次成功数据'
              : ''
          }}</small
        >
      </div>
    </div>

    <section class="content-card" aria-label="账户数据明细">
      <el-tabs v-model="activeTab" class="data-tabs">
        <el-tab-pane name="accounts">
          <template #label>
            <span class="tab-label"><Wallet />账户</span>
          </template>
          <AccountsTab
            :accounts="accounts"
            :loading="accountsState.status === 'loading'"
            :has-loaded="accountsLoaded"
            :load-error="accountsState.status === 'error'"
            :reload="loadAccounts"
          />
        </el-tab-pane>

        <el-tab-pane name="cash">
          <template #label>
            <span class="tab-label"><Coin />现金事件</span>
          </template>
          <CashEventsTab
            :cash-events="cashEvents"
            :accounts="accounts"
            :accounts-status="accountsState.status"
            :loading="loading.cash"
            :has-loaded="loaded.cash"
            :load-error="errors.cash"
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
            :accounts-status="accountsState.status"
            :loading="loading.batches"
            :has-loaded="loaded.batches"
            :load-error="errors.batches"
            :reload="loadImportBatches"
          />
        </el-tab-pane>

        <el-tab-pane name="reconciliation">
          <template #label>
            <span class="tab-label"><CircleCheck />月末核对</span>
          </template>
          <ReconciliationTab
            :snapshots="snapshots"
            :accounts="accounts"
            :accounts-status="accountsState.status"
            :loading="loading.snapshots"
            :has-loaded="loaded.snapshots"
            :load-error="errors.snapshots"
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
    </section>
  </div>
</template>

<script setup lang="ts">
import { showApiError } from '@/utils/showApiError'
import { computed, onMounted, reactive, ref, type Ref } from 'vue'
import { CircleCheck, Coins as Coin, Files, CircleMinus as Remove, Wallet } from '@lucide/vue'
import api from '@/api'
import { NButton } from 'naive-ui'
import { useLatestRequest } from '@/composables/useLatestRequest'
import { useBrokerAccounts } from '@/composables/useBrokerAccounts'
import { formatDate } from '@/utils/helpers'
import AccountsTab from './account-data/AccountsTab.vue'
import CashEventsTab from './account-data/CashEventsTab.vue'
import ImportBatchesTab from './account-data/ImportBatchesTab.vue'
import ReconciliationTab from './account-data/ReconciliationTab.vue'
import SecurityRulesTab from './account-data/SecurityRulesTab.vue'
import {
  isAtListLimit,
  LIST_LIMIT,
  reconciledAccountSummary,
  type CashEventRow,
  type ImportBatchRow,
  type SnapshotRow
} from './account-data/shared'

// 满额时显示「1000+」：列表只取回最近 LIST_LIMIT 条，真实总数可能更多
const countLabel = (rows: readonly unknown[]) =>
  isAtListLimit(rows) ? `${LIST_LIMIT}+` : String(rows.length)

// 壳层职责（issue #140）：页头汇总 + tab 骨架 + 汇总卡消费的四类数据的
// 装载。各 tab 的表格/弹窗/CRUD 在 account-data/ 下的页面私有子组件里；
// 特例规则不进汇总卡，其数据完全归子组件所有（refreshAll 经 ref 触发）。
const activeTab = ref('accounts')
const { state: accountsState, load: loadBrokerAccounts } = useBrokerAccounts()
const accountsLoaded = ref(false)
const loadAccounts = async () => {
  await loadBrokerAccounts()
  if (accountsState.status === 'ready') accountsLoaded.value = true
}
const accounts = computed(() => accountsState.accounts)
const cashEvents = ref<CashEventRow[]>([])
const importBatches = ref<ImportBatchRow[]>([])
const snapshots = ref<SnapshotRow[]>([])
const refreshing = ref(false)
const loading = reactive({
  cash: false,
  batches: false,
  snapshots: false
})
const loaded = reactive({ cash: false, batches: false, snapshots: false })
const errors = reactive({ cash: false, batches: false, snapshots: false })
const rulesTab = ref<InstanceType<typeof SecurityRulesTab>>()

const activeAccountCount = computed(
  () => accounts.value.filter((item) => item.is_active !== false).length
)
const reconciliation = computed(() => reconciledAccountSummary(accounts.value, snapshots.value))
const latestBatchDate = computed(() => {
  if (!loaded.batches) return '—'
  const dates = importBatches.value
    .map((item) => item.created_at)
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
  const requests = useLatestRequest()
  return async () => {
    const token = requests.begin()
    loading[loadingKey] = true
    try {
      const response = await fetcher()
      if (!requests.isCurrent(token)) return
      target.value = response.data
      loaded[loadingKey] = true
      errors[loadingKey] = false
    } catch (error) {
      if (!requests.isCurrent(token)) return
      errors[loadingKey] = true
      showApiError(error, failureMessage)
    } finally {
      if (requests.isCurrent(token)) loading[loadingKey] = false
    }
  }
}

const loadCashEvents = makeLoader(
  'cash',
  cashEvents,
  () => api.getCashEvents({ limit: LIST_LIMIT }),
  '现金事件加载失败'
)
const loadImportBatches = makeLoader(
  'batches',
  importBatches,
  () => api.getImportBatches({ limit: LIST_LIMIT }),
  '导入批次加载失败'
)
const loadSnapshots = makeLoader(
  'snapshots',
  snapshots,
  () => api.getReconciliationSnapshots({ limit: LIST_LIMIT }),
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
  grid-template-columns: minmax(0, 1fr);
  gap: 20px;
}

.page-intro {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
}

.page-intro h1 {
  color: var(--app-text);
  letter-spacing: -0.02em;
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
  border-top: 1px solid var(--app-border);
  background: var(--app-surface-muted);
}

.summary-item span,
.summary-item small {
  color: var(--app-text-muted);
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

.content-card {
  min-width: 0;
  border-top: 1px solid var(--app-border);
  padding-top: 12px;
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

.account-data-page :deep(.toolbar-actions) {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  flex-shrink: 0;
}
.account-data-page :deep(.section-toolbar) {
  align-items: flex-start;
  gap: 20px;
}
.account-data-page :deep(.section-toolbar h2) {
  font-size: 20px;
  font-weight: 600;
}
.account-data-page :deep(.section-toolbar p) {
  max-width: 76ch;
  line-height: 1.7;
}
.account-data-page :deep(.n-data-table) {
  font-variant-numeric: tabular-nums;
}
.account-data-page :deep(.n-data-table-td) {
  overflow-wrap: anywhere;
}
.account-data-page :deep(.primary-cell) {
  display: grid;
  gap: 4px;
}
.account-data-page :deep(.primary-cell strong) {
  font-weight: 600;
}
.account-data-page :deep(.primary-cell span),
.account-data-page :deep(.read-note) {
  color: var(--app-text-muted);
}
.account-data-page :deep(.read-note) {
  font-size: 13px;
  line-height: 1.7;
  margin: 10px 0 14px;
}
.account-data-page :deep(.read-alert) {
  margin-bottom: 14px;
}
.account-data-page :deep(.row-actions) {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.account-data-page :deep(.row-actions .n-button) {
  min-height: 24px;
}
.account-data-page :deep(.filter-field),
.account-data-page :deep(.native-filters label) {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--app-text-muted);
  font-size: 13px;
}
.account-data-page :deep(.compact-filter),
.account-data-page :deep(.native-filters) {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  margin: 0 0 16px;
}
.account-data-page :deep(select) {
  font: inherit;
  color: var(--app-text);
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-inner);
  min-height: 36px;
  max-width: 100%;
  padding: 0 30px 0 10px;
}
.account-data-page :deep(.read-details summary) {
  cursor: pointer;
  min-height: 24px;
  line-height: 24px;
  color: var(--app-primary-strong);
}
.account-data-page :deep(.read-details p) {
  margin: 6px 0;
  color: var(--app-text-muted);
  line-height: 1.7;
  overflow-wrap: anywhere;
}
.account-data-page :deep(.diff-status-button) {
  border: 0;
  background: none;
  padding: 0;
  min-height: 24px;
  display: inline-flex;
  align-items: center;
  cursor: pointer;
}
.account-data-page :deep(.diff-status-button:focus-visible),
.account-data-page :deep(select:focus-visible),
.account-data-page :deep(.read-details summary:focus-visible) {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: 3px;
}
@media (min-width: 1025px) {
  .account-data-page {
    gap: var(--app-space-md);
  }
  .summary-item {
    padding: var(--app-space-sm) var(--app-space-md);
  }
  .summary-item strong:not(.summary-date) {
    font-size: var(--app-number-secondary);
  }
  .data-tabs :deep(.el-tabs__header) {
    margin-bottom: var(--app-space-sm);
  }
}
@media (max-width: 900px) {
  .summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 640px) {
  .account-data-page :deep(.mobile-card-head) {
    align-items: flex-start;
  }
  .account-data-page :deep(.mobile-card-symbol),
  .account-data-page :deep(.mobile-card-name) {
    white-space: normal;
    overflow: visible;
    text-overflow: clip;
    overflow-wrap: anywhere;
  }
  .account-data-page :deep(.mobile-card-meta) {
    display: grid;
    gap: 6px;
  }
  .account-data-page :deep(.mobile-card-meta > *) {
    border: 0;
    padding: 0;
    overflow-wrap: anywhere;
  }
  .account-data-page :deep(.mobile-card-actions .n-button) {
    min-width: 44px;
  }
  .account-data-page :deep(.mobile-card-actions .n-button),
  .account-data-page :deep(.diff-status-button),
  .account-data-page :deep(.toolbar-actions .n-button),
  .account-data-page :deep(.section-toolbar > .n-button) {
    min-height: 44px;
  }
  .account-data-page :deep(.read-details summary) {
    min-height: 44px;
    line-height: 44px;
  }
  .account-data-page :deep(select) {
    min-height: 44px;
    min-width: 0;
    flex: 1;
  }
  .account-data-page :deep(.filter-field),
  .account-data-page :deep(.native-filters label) {
    width: 100%;
  }
  .account-data-page :deep(.compact-filter) {
    gap: 10px;
  }
  .account-data-page {
    gap: 16px;
  }

  .page-intro {
    align-items: stretch;
    flex-direction: column;
    gap: 12px;
  }

  .page-intro :deep(.n-button) {
    align-self: flex-start;
    min-height: 44px;
  }

  .summary-grid {
    gap: 10px;
  }

  .summary-item {
    padding: 14px;
  }

  .summary-item small {
    white-space: normal;
  }

  .data-tabs :deep(.el-tabs__nav-wrap) {
    overflow-x: auto;
  }
}
</style>

<style>
/* 本页保留的成熟表单在 body 挂载，只限定这组实际对话框。 */
.account-form-dialog {
  --el-text-color-placeholder: var(--app-text-soft);
}
.account-form-dialog .el-form-item__content {
  min-width: 0;
}
.account-form-dialog .el-select,
.account-form-dialog .el-date-editor {
  max-width: 100%;
}
@media (max-width: 640px) {
  .account-form-dialog .el-input__wrapper,
  .account-form-dialog .el-select__wrapper {
    min-height: 44px;
    box-sizing: border-box;
  }
  .account-form-dialog .el-button {
    min-height: 44px;
  }
}
</style>
