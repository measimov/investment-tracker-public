<template>
  <div class="corporate-actions-page">
    <el-tabs v-model="activeTab" class="page-tabs">
      <el-tab-pane label="公司行动记录" name="records" />
      <el-tab-pane name="suggestions">
        <template #label>
          <span>
            分红建议
            <el-badge
              v-if="suggestionPendingCount > 0"
              :value="suggestionPendingCount"
              class="tab-badge"
            />
          </span>
        </template>
      </el-tab-pane>
    </el-tabs>

    <RecordsTab
      ref="recordsTab"
      v-show="activeTab === 'records'"
      :broker-accounts="brokerAccounts"
    />

    <SuggestionsTab
      v-show="activeTab === 'suggestions'"
      :active="activeTab === 'suggestions'"
      :broker-accounts="brokerAccounts"
      @counts-changed="refreshSuggestionCount"
      @accepted="recordsTab?.reload()"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'
import { getApiErrorMessage } from '../utils/apiErrors'
import type { BrokerAccount } from '../types'
import RecordsTab from './corporate-actions/RecordsTab.vue'
import SuggestionsTab from './corporate-actions/SuggestionsTab.vue'

// 壳层职责（issue #140）：tab 骨架与徽标、两个 tab 共用的券商账户目录、
// 跨 tab 编排（接受建议入账后刷新记录列表）。两个 tab 各自成组件
// （v-show 常驻，行为与拆分前一致：建议 tab 首次激活才加载、同步轮询
// 切走不中断）。
const activeTab = ref<'records' | 'suggestions'>('records')
const brokerAccounts = ref<BrokerAccount[]>([])
const suggestionPendingCount = ref(0)
const recordsTab = ref<InstanceType<typeof RecordsTab> | null>(null)

async function loadBrokerAccounts() {
  try {
    const response = await api.getBrokerAccounts({ limit: 1000 })
    brokerAccounts.value = response.data
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '加载券商账户失败'))
  }
}

async function refreshSuggestionCount() {
  try {
    const response = await api.countDividendSuggestions()
    suggestionPendingCount.value = response.data.total || 0
  } catch {
    // 徽标计数失败静默：不打断主流程
  }
}

onMounted(async () => {
  await loadBrokerAccounts()
  recordsTab.value?.reload()
  refreshSuggestionCount()
})
</script>

<style scoped>
.corporate-actions-page {
  width: 100%;
}

.page-tabs {
  margin-bottom: 4px;
}

.tab-badge {
  margin-left: 6px;
  vertical-align: 2px;
}
</style>
