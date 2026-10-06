<template>
  <div class="security-detail-page">
    <header class="page-header">
      <div class="title-with-tag">
        <h1 class="security-title">{{ state.analysis?.name || symbol }}</h1>
        <p class="security-meta">{{ symbol }} · {{ market }} · 标的档案</p>
      </div>
      <div class="header-actions">
        <NTag v-if="state.watchState === 'watching'" type="success" :bordered="false"
          >已在观察清单</NTag
        >
        <NButton
          v-else-if="state.watchState === 'not-watching'"
          data-testid="add-to-watchlist-button"
          @click="addToWatchlist"
          ><template #icon><View /></template>加入观察</NButton
        >
        <NButton
          :type="activeTab === 'analysis' ? 'primary' : 'default'"
          :loading="state.generating"
          :disabled="!state.supported || state.analysisLoading || state.profileLoading"
          data-testid="generate-analysis-button"
          @click="generateAnalysis"
          >{{ state.analysis ? '重新生成分析' : '生成 AI 分析' }}</NButton
        >
      </div>
    </header>
    <NAlert v-if="!state.supported" type="info" :closable="false"
      >该市场暂不支持基本面数据与 AI
      分析（支持：A股/美股/港股）；其他市场信息以券商对账单与行情为准。</NAlert
    >
    <template v-else>
      <div v-if="state.analysisJob" class="job-progress" data-testid="analysis-progress">
        <div class="job-progress-header">
          <span>{{ analysisStatusText }}</span
          ><span v-if="state.analysisJob.total"
            >{{ state.analysisJob.completed || 0 }}/{{ state.analysisJob.total }}</span
          >
        </div>
        <NProgress
          type="line"
          :percentage="analysisPercent"
          :status="analysisProgressStatus"
          :height="8"
        />
        <div v-if="state.analysisJob.error" class="job-progress-error">
          {{ state.analysisJob.error }}
        </div>
      </div>
      <NAlert
        v-if="state.profileError"
        type="error"
        class="profile-error"
        data-testid="profile-load-error"
      >
        {{
          state.profileHasLoaded
            ? '档案刷新失败，下方保留此标的上次成功数据。'
            : '标的档案加载失败，当前内容未知。'
        }}
        {{ state.profileError }}
        <NButton
          :loading="state.profileLoading"
          :disabled="state.generating || state.backfilling"
          @click="retryProfile"
          >重试档案</NButton
        >
      </NAlert>
      <!-- 成熟tab保留首次lazy和之后常驻；公开键盘能力已实测。 -->
      <el-tabs v-model="activeTab" class="detail-tabs">
        <el-tab-pane label="分析" name="analysis" lazy
          ><AnalysisTab :state="state" :market="market" @retry="retryAnalysis"
        /></el-tab-pane>
        <el-tab-pane label="基本面" name="fundamentals" lazy>
          <div
            v-if="state.profileLoading && !state.profileHasLoaded"
            role="status"
            aria-label="正在加载基本面"
          >
            <NSkeleton text :repeat="6" />
          </div>
          <NEmpty
            v-else-if="state.profileError && !state.profileHasLoaded"
            description="基本面暂不可用，请重试档案"
          />
          <NSpin v-else :show="state.profileLoading"
            ><FundamentalsTab :state="state" :market="market"
          /></NSpin>
        </el-tab-pane>
        <el-tab-pane label="报表" name="statements" lazy>
          <div
            v-if="state.profileLoading && !state.profileHasLoaded"
            role="status"
            aria-label="正在加载报表"
          >
            <NSkeleton text :repeat="6" />
          </div>
          <NEmpty
            v-else-if="state.profileError && !state.profileHasLoaded"
            description="报表暂不可用，请重试档案"
          />
          <NSpin v-else :show="state.profileLoading"
            ><StatementsTab :state="state" :market="market" @backfill="backfillDigests"
          /></NSpin>
        </el-tab-pane>
        <el-tab-pane label="公告" name="announcements" lazy
          ><AnnouncementsTab :symbol="symbol" :market="market"
        /></el-tab-pane>
        <el-tab-pane v-if="showOpinionTab" label="观点" name="opinions" lazy
          ><OpinionSection :symbol="symbol" :market="market"
        /></el-tab-pane>
      </el-tabs>
    </template>
  </div>
</template>

<script setup lang="ts">
/**
 * 标的详情页（父 view 只做布局与编排，issue #140）：数据层在 security-detail/useSecurityProfile，
 * 五个 tab 各自一个子组件，区块样式统一在 security-detail/detail.css。
 */
import { NAlert, NButton, NEmpty, NProgress, NSkeleton, NSpin, NTag } from 'naive-ui'
import { Eye as View } from '@lucide/vue'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useAliveGuard } from '../composables/useAliveGuard'
import { useXueqiuCapabilitiesStore } from '../stores/xueqiuCapabilities'
import AnalysisTab from './security-detail/AnalysisTab.vue'
import AnnouncementsTab from './security-detail/AnnouncementsTab.vue'
import FundamentalsTab from './security-detail/FundamentalsTab.vue'
import OpinionSection from './security-detail/OpinionSection.vue'
import StatementsTab from './security-detail/StatementsTab.vue'
import { useSecurityProfile } from './security-detail/useSecurityProfile'
import './security-detail/detail.css'

const route = useRoute()
const market = computed(() => String(route.params.market || ''))
const symbol = computed(() => String(route.params.symbol || ''))
const activeTab = ref('analysis')
const capabilities = useXueqiuCapabilitiesStore()
const showOpinionTab = computed(() => capabilities.showOpinions || capabilities.showSymbolFeed)
watch(showOpinionTab, (visible) => {
  if (!visible && activeTab.value === 'opinions') activeTab.value = 'analysis'
})

const { isUnmounted } = useAliveGuard()
const {
  state,
  init,
  resetAndReload,
  retryAnalysis,
  retryProfile,
  addToWatchlist,
  backfillDigests,
  generateAnalysis
} = useSecurityProfile({
  market: () => market.value,
  symbol: () => symbol.value,
  isUnmounted
})

const analysisPercent = computed(() =>
  Math.max(0, Math.min(100, Math.round(Number(state.analysisJob?.progress_percent || 0))))
)

const analysisProgressStatus = computed(() => {
  const status = state.analysisJob?.status
  if (status === 'failed' || status === 'interrupted') return 'error'
  if (status === 'succeeded') return 'success'
  return undefined
})

const ANALYSIS_STATUS_LABELS: Record<string, string> = {
  queued: '分析任务排队中',
  running: 'AI 分析生成中',
  succeeded: '分析生成完成',
  failed: '分析生成失败',
  interrupted: '分析任务已中断'
}

const analysisStatusText = computed(() => {
  const job = state.analysisJob
  if (!job) return ''
  // 运行中优先显示后端回写的阶段名（同步基本面档案 / 生成分析（LLM）…）
  if (job.status === 'running' && job.stage_label) return job.stage_label
  return ANALYSIS_STATUS_LABELS[String(job.status || '')] || 'AI 分析生成中'
})

onMounted(init)

// 同业跳转复用同一路由组件：参数变化时清空并重载（旧标的的在途请求由
// 代次守卫拦下，不会写进新标的的页面）；当前 tab 保持不变
watch(
  () => [route.params.market, route.params.symbol],
  () => {
    if (route.name !== 'SecurityDetail') return
    resetAndReload()
  }
)
</script>

<style scoped>
.security-detail-page {
  width: 100%;
  max-width: 1120px;
  min-width: 0;
  margin-inline: auto;
}
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  padding: 0 0 16px;
  margin-bottom: 16px;
  border-bottom: 1px solid var(--app-border);
}
.title-with-tag {
  min-width: 0;
  flex: 1;
}
.security-title {
  font-family: var(--app-font-serif);
  font-weight: 400;
  font-size: 28px;
  line-height: 1.35;
  margin: 0;
  overflow-wrap: anywhere;
  text-wrap: balance;
}
.security-meta {
  color: var(--app-text-muted);
  font-size: 13px;
  line-height: 1.8;
  margin: 4px 0 0;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.profile-error {
  margin: 16px 0;
}
.profile-error .n-button {
  margin-left: 12px;
}
.detail-tabs :deep(.el-tabs__header) {
  margin-bottom: 24px;
}
.detail-tabs :deep(.el-tabs__item:not(.is-active)) {
  font-weight: 400;
  color: var(--app-text-muted);
}
.detail-tabs :deep(.el-tabs__item:not(.is-active):hover) {
  color: var(--el-color-primary);
}
@media (max-width: 900px) {
  .page-header {
    align-items: stretch;
  }
  .title-with-tag {
    flex: none;
    width: 100%;
  }
}
@media (max-width: 640px) {
  .page-header {
    gap: 12px;
  }
  .header-actions .n-button {
    min-height: 44px;
  }
  .header-actions {
    width: 100%;
  }
  .detail-tabs :deep(.el-tabs__item) {
    height: 44px;
    padding: 0 14px;
  }
}

@media (min-width: 1025px) {
  .detail-tabs :deep(.el-tabs__header) {
    margin-bottom: 16px;
  }
}
</style>
