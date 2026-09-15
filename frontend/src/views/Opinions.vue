<script setup lang="ts">
/**
 * 雪球观点页（父 view 只做布局与编排，逻辑在 views/opinions/ 下，issue #140）：
 * 数据源状态条 + 批量生成 + 标的观点 / 作者动态两个 tab。
 */
import { onMounted, ref } from 'vue'
import { useAliveGuard } from '@/composables/useAliveGuard'
import JobProgressCard from '@/components/JobProgressCard.vue'
import SymbolsTab from './opinions/SymbolsTab.vue'
import AuthorsTab from './opinions/AuthorsTab.vue'
import { useOpinions } from './opinions/useOpinions'
import { useOpinionBatch } from './opinions/useOpinionBatch'

const { isUnmounted } = useAliveGuard()
const { state, staleHoursText, loadSummaries, loadFeed } = useOpinions()
const batch = useOpinionBatch({
  isUnmounted,
  refreshSummaries: loadSummaries
})

const activeTab = ref('symbols')

function onTabChange(name: string | number) {
  if (name === 'authors') loadFeed()
}

onMounted(async () => {
  await loadSummaries()
  await batch.adoptActiveJob()
})
</script>

<template>
  <div class="opinions-page">
    <el-card>
      <template #header>
        <div class="page-header">
          <div>
            <span class="page-title">雪球观点</span>
            <span v-if="state.freshness?.available" class="freshness-line">
              数据更新于 {{ state.freshness.latest_scan_at?.slice(0, 16).replace('T', ' ') }} ·
              最新发言 {{ state.freshness.latest_utterance_at?.slice(0, 10) }}
            </span>
          </div>
          <el-button
            type="primary"
            data-testid="opinion-batch-button"
            :disabled="!state.sourceAvailable || batch.isActive"
            :loading="batch.starting"
            @click="batch.generateAll()"
          >
            批量生成观点标签
          </el-button>
        </div>
      </template>

      <el-alert
        v-if="!state.sourceAvailable"
        type="error"
        :closable="false"
        show-icon
        data-testid="opinion-source-missing"
        title="雪球观点数据源未接入"
        description="未找到 xueqiu_archiver_utterances 表；该数据由 xueqiu-timeline-archiver 项目的采集任务写入同一数据库。"
        class="status-alert"
      />
      <el-alert
        v-else-if="state.freshness?.stale"
        type="warning"
        :closable="false"
        show-icon
        data-testid="opinion-source-stale"
        :title="`雪球观点数据已 ${staleHoursText} 小时未更新`"
        description="archiver 采集 cron 可能已停摆（Cookie 过期或任务失败）；摘要仍可生成，但不含最新发言。"
        class="status-alert"
      />
      <el-alert
        v-if="state.loadError"
        type="error"
        :closable="false"
        :title="state.loadError"
        class="status-alert"
      />

      <JobProgressCard
        v-if="batch.job"
        :job="batch.job"
        :status-text="batch.statusText"
        :percent="batch.percent"
        :progress-status="batch.progressStatus"
        :eta-text="batch.etaText"
        :active="batch.isActive"
        hint="任务在后台运行，关闭页面不会中断；已生成的摘要即时可见。"
        testid="opinion-batch-progress"
        cancel-testid="cancel-opinion-batch-button"
        stop-testid="stop-watch-opinion-batch-button"
        @cancel="batch.requestCancel"
        @stop="batch.stopWatching"
      >
        <template #detail>
          <span v-if="batch.job?.current_stage">{{ batch.job.current_stage }}</span>
        </template>
      </JobProgressCard>

      <el-tabs v-model="activeTab" @tab-change="onTabChange">
        <el-tab-pane label="标的观点" name="symbols">
          <SymbolsTab
            :items="state.items"
            :loading="state.loading"
            :source-available="state.sourceAvailable"
          />
        </el-tab-pane>
        <el-tab-pane label="作者动态" name="authors">
          <AuthorsTab :authors="state.feedAuthors" :loading="state.feedLoading" />
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.page-title {
  font-weight: 600;
  font-size: 16px;
}
.freshness-line {
  margin-left: 12px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.status-alert {
  margin-bottom: 12px;
}
</style>
