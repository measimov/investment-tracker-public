<template>
  <div class="security-detail-page">
    <el-card>
      <template #header>
        <div class="page-header">
          <div class="title-with-tag">
            <el-button text :icon="ArrowLeft" @click="router.back()">返回</el-button>
            <span class="security-title">
              {{ symbol }}
              <span v-if="state.analysis?.name" class="security-name">{{
                state.analysis.name
              }}</span>
            </span>
            <el-tag size="small" effect="plain">{{ market }}</el-tag>
          </div>
          <div class="header-actions">
            <el-tag
              v-if="state.watchState === 'watching'"
              type="success"
              effect="plain"
              size="small"
            >
              已在观察清单
            </el-tag>
            <el-button
              v-else-if="state.watchState === 'not-watching'"
              :icon="View"
              data-testid="add-to-watchlist-button"
              @click="addToWatchlist"
            >
              加入观察
            </el-button>
            <el-button
              type="primary"
              :loading="state.generating"
              :disabled="!state.supported"
              data-testid="generate-analysis-button"
              @click="generateAnalysis"
            >
              {{ state.analysis ? '重新生成分析' : '生成 AI 分析' }}
            </el-button>
          </div>
        </div>
      </template>

      <el-alert
        v-if="!state.supported"
        title="该市场暂不支持基本面数据与 AI 分析（支持：A股/美股/港股）；其他市场信息以券商对账单与行情为准。"
        type="info"
        :closable="false"
        show-icon
      />

      <template v-else>
        <!-- 生成进度（放在 tab 之外：在任何 tab 点「生成」都看得到；首次生成时还没有分析正文） -->
        <div v-if="state.analysisJob" class="job-progress" data-testid="analysis-progress">
          <div class="job-progress-header">
            <span>{{ analysisStatusText }}</span>
            <span v-if="state.analysisJob.total">
              {{ state.analysisJob.completed || 0 }}/{{ state.analysisJob.total }}
            </span>
          </div>
          <el-progress
            :percentage="analysisPercent"
            :status="analysisProgressStatus"
            :stroke-width="8"
          />
          <div v-if="state.analysisJob.error" class="job-progress-error">
            {{ state.analysisJob.error }}
          </div>
        </div>

        <!-- 按「分析 / 基本面 / 报表 / 观点」分 tab，懒渲染：未打开的 tab 不建表格、观点不取数 -->
        <el-tabs v-model="activeTab" class="detail-tabs">
          <el-tab-pane label="分析" name="analysis" lazy>
            <AnalysisTab :state="state" :market="market" />
          </el-tab-pane>
          <el-tab-pane label="基本面" name="fundamentals" lazy>
            <FundamentalsTab :state="state" :market="market" />
          </el-tab-pane>
          <el-tab-pane label="报表" name="statements" lazy>
            <StatementsTab :state="state" :market="market" @backfill="backfillDigests" />
          </el-tab-pane>
          <el-tab-pane label="观点" name="opinions" lazy>
            <OpinionSection :symbol="symbol" :market="market" />
          </el-tab-pane>
        </el-tabs>
      </template>
    </el-card>
  </div>
</template>

<script setup lang="ts">
/**
 * 标的详情页（父 view 只做布局与编排，issue #140）：数据层在 security-detail/useSecurityProfile，
 * 四个 tab 各自一个子组件，区块样式统一在 security-detail/detail.css。
 */
import { ArrowLeft, View } from '@element-plus/icons-vue'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAliveGuard } from '../composables/useAliveGuard'
import AnalysisTab from './security-detail/AnalysisTab.vue'
import FundamentalsTab from './security-detail/FundamentalsTab.vue'
import OpinionSection from './security-detail/OpinionSection.vue'
import StatementsTab from './security-detail/StatementsTab.vue'
import { useSecurityProfile } from './security-detail/useSecurityProfile'
import './security-detail/detail.css'

const route = useRoute()
const router = useRouter()
const market = computed(() => String(route.params.market || ''))
const symbol = computed(() => String(route.params.symbol || ''))
const activeTab = ref('analysis')

const { isUnmounted } = useAliveGuard()
const { state, init, resetAndReload, addToWatchlist, backfillDigests, generateAnalysis } =
  useSecurityProfile({
    market: () => market.value,
    symbol: () => symbol.value,
    isUnmounted
  })

const analysisPercent = computed(() =>
  Math.max(0, Math.min(100, Math.round(Number(state.analysisJob?.progress_percent || 0))))
)

const analysisProgressStatus = computed(() => {
  const status = state.analysisJob?.status
  if (status === 'failed' || status === 'interrupted') return 'exception'
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
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.title-with-tag,
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.security-title {
  font-weight: 600;
  font-size: 16px;
}

.security-name {
  color: var(--app-text-soft);
  font-weight: 400;
  margin-left: 4px;
}

.detail-tabs :deep(.el-tabs__header) {
  margin-bottom: 14px;
}
</style>
