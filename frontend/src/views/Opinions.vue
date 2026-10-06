<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NAlert, NButton } from 'naive-ui'
import { useAliveGuard } from '@/composables/useAliveGuard'
import JobProgressCard from '@/components/JobProgressCard.vue'
import SymbolsTab from './opinions/SymbolsTab.vue'
import AuthorsTab from './opinions/AuthorsTab.vue'
import CollectorCard from './opinions/CollectorCard.vue'
import { useOpinions } from './opinions/useOpinions'
import { useOpinionBatch } from './opinions/useOpinionBatch'
import { formatDateTime } from '@/utils/helpers'

const { isUnmounted } = useAliveGuard()
const { state, feedLoaded, staleText, loadSummaries, loadFeed } = useOpinions()
const batch = useOpinionBatch({ isUnmounted, refreshSummaries: loadSummaries })
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
    <header class="page-header">
      <div>
        <h1 class="page-title">雪球观点</h1>
        <p class="page-description">
          关注作者，查看标的观点与发言。
          <span v-if="state.freshness?.available">
            · 数据更新于 {{ formatDateTime(state.freshness.latest_scan_at) }} · 最新发言
            {{ formatDateTime(state.freshness.latest_utterance_at) }}
          </span>
        </p>
      </div>
      <NButton
        type="primary"
        data-testid="opinion-batch-button"
        :disabled="state.sourceAvailable !== true || batch.isActive"
        :loading="batch.starting"
        @click="batch.generateAll()"
        >批量生成观点摘要</NButton
      >
    </header>

    <NAlert
      v-if="state.sourceAvailable === false"
      type="error"
      data-testid="opinion-source-missing"
      title="雪球观点数据源当前不可用"
    >
      本次不能确认最新发言；已有历史摘要仍保留，详见下方采集器状态。
    </NAlert>
    <NAlert
      v-else-if="state.freshness?.stale"
      type="warning"
      data-testid="opinion-source-stale"
      :title="`雪球观点数据${staleText}`"
    >
      雪球采集器可能已停摆（Cookie 过期、WAF
      或进程离线，见下方「采集器」）；摘要仍可生成，但不含最新发言。
    </NAlert>
    <CollectorCard />
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
      <template #detail
        ><span v-if="batch.job?.current_stage">{{ batch.job.current_stage }}</span></template
      >
    </JobProgressCard>
    <section class="opinions-content" aria-label="观点与作者发言">
      <el-tabs v-model="activeTab" @tab-change="onTabChange">
        <el-tab-pane label="标的观点" name="symbols">
          <div class="section-toolbar">
            <p class="read-note">
              近 {{ state.recentDays }} 天关注作者涉及的持仓与自选，按新发言数量排序。
            </p>
            <NButton :loading="state.loading" aria-label="重新加载观点概览" @click="loadSummaries"
              >重新加载</NButton
            >
          </div>
          <NAlert v-if="state.loadError" type="error" :title="state.loadError">
            {{
              state.hasLoaded
                ? '保留上次成功的概览，未确认最新结果。'
                : '尚未确认观点概览，不能据此判断没有发言。'
            }}
          </NAlert>
          <p v-else-if="state.loading && state.hasLoaded" class="read-note">
            正在重新加载，当前保留上次成功的概览。
          </p>
          <SymbolsTab
            :items="state.items"
            :loading="state.loading"
            :has-loaded="state.hasLoaded"
            :load-error="state.loadError"
            :source-available="state.sourceAvailable"
          />
        </el-tab-pane>
        <el-tab-pane label="作者动态" name="authors">
          <AuthorsTab
            :authors="state.feedAuthors"
            :loading="state.feedLoading"
            :has-loaded="feedLoaded"
            :load-error="state.feedError"
            :source-available="state.feedSourceAvailable"
            :freshness="state.feedFreshness"
            :days="state.recentDays"
            @retry="loadFeed(true)"
          />
        </el-tab-pane>
      </el-tabs>
    </section>
  </div>
</template>

<style scoped>
.opinions-page {
  display: grid;
  gap: 20px;
  min-width: 0;
}
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
  flex-wrap: wrap;
}
.read-note {
  color: var(--app-text-muted);
  font-size: 13px;
  line-height: 1.7;
  margin: 8px 0 0;
}
.opinions-content {
  min-width: 0;
  padding: 20px;
  border: 1px solid var(--app-border);
  border-radius: 12px;
  background: var(--app-surface);
}
.section-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
}
.section-toolbar .read-note {
  margin: 0;
}
.opinions-content :deep(.n-alert) {
  margin-bottom: 12px;
}
@media (max-width: 900px) {
  .page-header {
    align-items: flex-start;
  }
}
@media (max-width: 640px) {
  .opinions-page {
    gap: 16px;
  }
  .opinions-content {
    padding: 14px;
  }
  .page-header :deep(.n-button),
  .section-toolbar :deep(.n-button) {
    min-height: 44px;
  }
  .section-toolbar {
    align-items: flex-start;
  }
}

@media (min-width: 1025px) {
  .opinions-page {
    gap: 16px;
  }
  .opinions-content {
    padding: 16px;
  }
}
</style>
