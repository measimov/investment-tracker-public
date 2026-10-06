<template>
  <div class="corporate-actions-page">
    <header class="actions-heading">
      <h1 class="page-title">公司行动</h1>
      <p class="page-description">查看已记录权益变动，核对公告预计与实际股息到账。</p>
    </header>
    <el-tabs v-model="activeTab" class="page-tabs">
      <el-tab-pane name="records" label="公司行动记录" />
      <el-tab-pane name="suggestions"
        ><template #label>
          <span>
            预计与待收股息
            <NBadge
              v-if="suggestionCountLoaded && suggestionPendingCount > 0"
              :value="suggestionPendingCount"
              class="tab-badge"
            />
            <span
              v-if="suggestionCountLoading || suggestionCountError || !suggestionCountLoaded"
              class="count-status"
              data-testid="suggestion-count-status"
              role="status"
              >{{
                suggestionCountLoading
                  ? suggestionCountLoaded
                    ? '数量更新中'
                    : '数量加载中'
                  : suggestionCountLoaded
                    ? '数量更新失败'
                    : '数量未确认'
              }}</span
            >
          </span>
        </template></el-tab-pane
      >
    </el-tabs>
    <NAlert
      v-if="suggestionCountError"
      type="warning"
      :show-icon="false"
      class="count-error"
      data-testid="suggestion-count-error"
    >
      {{
        suggestionCountLoaded
          ? `待处理数量更新失败，保留上次成功计数 ${suggestionPendingCount}；最新数量尚未确认。`
          : '待处理数量尚未确认，读取失败不表示没有待处理股息。'
      }}
      <NButton
        text
        type="primary"
        class="count-retry"
        :loading="suggestionCountLoading"
        @click="refreshSuggestionCount"
        >重试待处理数量</NButton
      >
    </NAlert>

    <RecordsTab
      ref="recordsTab"
      v-show="activeTab === 'records'"
      :broker-accounts="brokerAccounts"
      :broker-accounts-status="accountsState.status"
    />

    <SuggestionsTab
      v-show="activeTab === 'suggestions'"
      :active="activeTab === 'suggestions'"
      :broker-accounts="brokerAccounts"
      :broker-accounts-status="accountsState.status"
      @counts-changed="refreshSuggestionCount"
      @accepted="recordsTab?.reload()"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted } from 'vue'
import { NAlert, NBadge, NButton } from 'naive-ui'
import { useBrokerAccounts } from '@/composables/useBrokerAccounts'
import { useAliveGuard } from '@/composables/useAliveGuard'
import { useLatestRequest } from '@/composables/useLatestRequest'
import api from '../api'
import RecordsTab from './corporate-actions/RecordsTab.vue'
import SuggestionsTab from './corporate-actions/SuggestionsTab.vue'

// 壳层职责（issue #140）：tab 骨架与徽标、两个 tab 共用的券商账户目录、
// 跨 tab 编排（接受建议入账后刷新记录列表）。两个 tab 各自成组件
// （v-show 常驻，行为与拆分前一致：建议 tab 首次激活才加载、同步轮询
// 切走不中断）。
const activeTab = ref<'records' | 'suggestions'>('records')
const { state: accountsState, load: loadBrokerAccounts } = useBrokerAccounts()
const brokerAccounts = computed(() => accountsState.accounts)
const suggestionPendingCount = ref(0)
const suggestionCountLoading = ref(true)
const suggestionCountLoaded = ref(false)
const suggestionCountError = ref(false)
const suggestionCountRequest = useLatestRequest()
const { isUnmounted } = useAliveGuard()
const recordsTab = ref<InstanceType<typeof RecordsTab> | null>(null)

async function refreshSuggestionCount() {
  if (isUnmounted()) return
  const request = suggestionCountRequest.begin()
  suggestionCountLoading.value = true
  try {
    const response = await api.countDividendSuggestions()
    if (!suggestionCountRequest.isCurrent(request)) return
    suggestionPendingCount.value = response.data.total || 0
    suggestionCountLoaded.value = true
    suggestionCountError.value = false
  } catch {
    if (suggestionCountRequest.isCurrent(request)) suggestionCountError.value = true
  } finally {
    if (suggestionCountRequest.isCurrent(request)) suggestionCountLoading.value = false
  }
}

onMounted(async () => {
  await loadBrokerAccounts()
  if (isUnmounted()) return
  recordsTab.value?.reload()
  refreshSuggestionCount()
})
</script>

<style scoped>
.corporate-actions-page {
  width: 100%;
}

.actions-heading {
  padding: 24px 0 28px;
  border-bottom: 1px solid var(--app-border);
  margin-bottom: 14px;
}
.page-tabs {
  margin-bottom: 4px;
}

.tab-badge {
  margin-left: 6px;
  vertical-align: 2px;
}
.count-status {
  margin-left: 6px;
  color: var(--app-text-muted);
  font-size: 13px;
}
.count-error {
  margin: 12px 0;
}
@media (max-width: 640px) {
  .count-retry {
    min-height: 44px;
  }
}
@media (min-width: 1025px) {
  .actions-heading {
    padding: 4px 0 var(--app-space-md);
  }
}
</style>
