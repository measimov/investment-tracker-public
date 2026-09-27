<template>
  <div class="holdings-page">
    <el-card class="holdings-card">
      <template #header>
        <div class="page-header">
          <span>当前持仓</span>
          <div class="header-actions">
            <el-radio-group
              :model-value="table.state.viewMode"
              size="default"
              data-testid="holdings-view-mode"
              @update:model-value="(mode) => table.setViewMode(mode as HoldingsViewMode)"
            >
              <el-radio-button value="merged">按标的</el-radio-button>
              <el-radio-button value="account">按账户</el-radio-button>
            </el-radio-group>
            <el-button
              type="success"
              :icon="Refresh"
              @click="table.refreshPrices"
              :loading="table.state.refreshing"
            >
              刷新股价
            </el-button>
            <el-tooltip
              :disabled="batch.analyzableCount > 0"
              content="当前持仓没有 A股/美股/港股 标的"
            >
              <span>
                <el-button
                  type="primary"
                  :icon="MagicStick"
                  :loading="batch.starting"
                  :disabled="batch.analyzableCount === 0 || batch.isActive || digest.isActive"
                  data-testid="analyze-all-button"
                  @click="batch.analyzeAll"
                >
                  AI 分析全部
                </el-button>
              </span>
            </el-tooltip>
            <el-tooltip
              :disabled="batch.analyzableCount > 0"
              content="当前持仓没有 A股/美股/港股 标的"
            >
              <span>
                <el-button
                  :icon="Notebook"
                  :loading="digest.starting"
                  :disabled="batch.analyzableCount === 0 || batch.isActive || digest.isActive"
                  data-testid="digest-backfill-button"
                  @click="digest.backfillAll"
                >
                  补财报摘要
                </el-button>
              </span>
            </el-tooltip>
            <el-select
              v-model="table.state.selectedAccount"
              placeholder="选择账户"
              clearable
              class="market-select"
            >
              <el-option label="全部账户" :value="''" />
              <el-option label="未指定账户" value="unassigned" />
              <el-option
                v-for="account in table.state.brokerAccounts"
                :key="account.id"
                :label="account.account_name"
                :value="account.id"
              />
            </el-select>
            <el-select
              v-model="table.state.selectedMarket"
              placeholder="选择市场"
              clearable
              @change="table.loadHoldings"
              class="market-select"
            >
              <el-option label="全部市场" value="" />
              <el-option v-for="m in MARKETS" :key="m" :label="m" :value="m" />
            </el-select>
          </div>
        </div>
      </template>

      <JobProgressCard
        v-if="batch.job"
        :job="batch.job"
        :status-text="batch.statusText"
        :percent="batch.percent"
        :progress-status="batch.progressStatus"
        :eta-text="batch.etaText"
        :active="batch.isActive"
        hint="「停止查看」只停止本页轮询，后台任务会继续运行并继续消耗 token；重新进入持仓页可继续查看。"
        testid="batch-analysis-progress"
        cancel-testid="cancel-batch-button"
        stop-testid="stop-watching-batch"
        @cancel="batch.requestCancel"
        @stop="batch.stopWatching"
      >
        <template #header-suffix>
          <template v-if="batch.job.current_stage">（{{ batch.job.current_stage }}）</template>
        </template>
        <template #detail>
          成功 {{ batch.job.success_count || 0 }} · 跳过 {{ batch.job.skipped_count || 0 }} · 失败
          {{ batch.job.failed_count || 0 }}
        </template>
        <template #extra>
          <div v-if="batch.recentResults.length" class="batch-recent">
            <span
              v-for="item in batch.recentResults"
              :key="`${item.symbol}:${item.market}`"
              :class="{ 'batch-recent-failed': item.status === 'failed' }"
            >
              {{ item.symbol }} {{ batch.resultLabel(item) }}
            </span>
          </div>
        </template>
      </JobProgressCard>

      <JobProgressCard
        v-if="digest.job"
        :job="digest.job"
        :status-text="digest.statusText"
        :percent="digest.percent"
        :progress-status="digest.progressStatus"
        :eta-text="digest.etaText"
        :active="digest.isActive"
        hint="「停止查看」只停止本页轮询，后台任务会继续运行；重新进入持仓页可继续查看。"
        testid="digest-backfill-progress"
        cancel-testid="cancel-digest-backfill"
        @cancel="digest.requestCancel"
        @stop="digest.stopWatching"
      >
        <template #detail>
          已生成摘要 {{ digest.job.digests_generated || 0 }} 份 · 标的成功
          {{ digest.job.success_count || 0 }} · 失败 {{ digest.job.failed_count || 0 }}
          <template v-if="digest.job.statements_generated || digest.job.statements_suspect">
            · 港股报表新抽 {{ digest.job.statements_generated || 0 }} 份<template
              v-if="digest.job.statements_suspect"
              >、{{ digest.job.statements_suspect }} 期存疑</template
            >
          </template>
        </template>
      </JobProgressCard>

      <!-- 汇总放在表格上方：持仓一多，表格下方的汇总要滚到底才看得到 -->
      <HoldingsSummary :table="table" />

      <HoldingsTable :table="table" :badges="badges" @transfer="transfer.openDialog" />
    </el-card>

    <TransferDialog :transfer="transfer" />
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { MagicStick, Notebook, Refresh } from '@element-plus/icons-vue'
import api from '../api'
import { useAliveGuard } from '../composables/useAliveGuard'
import { useExchangeRates } from '../composables/useExchangeRates'
import { MARKETS } from '../utils/securities'
import JobProgressCard from '../components/JobProgressCard.vue'
import HoldingsTable from './holdings/HoldingsTable.vue'
import HoldingsSummary from './holdings/HoldingsSummary.vue'
import TransferDialog from './holdings/TransferDialog.vue'
import { useHoldingsTable } from './holdings/useHoldingsTable'
import { useSecurityBadges } from './holdings/useSecurityBadges'
import { useTransfer } from './holdings/useTransfer'
import { useBatchAnalysis } from './holdings/useBatchAnalysis'
import { useDigestBackfill } from './holdings/useDigestBackfill'
import type { AnalysisBatchJob, DigestBatchJob } from './holdings/types'
import type { HoldingsViewMode } from './holdings/useHoldingsTable'

// 壳层职责（issue #140）：页头（刷新/批量按钮 + 账户/市场过滤）、两个批量
// job 的进度块、以及五个 feature 的编排。持仓数据/角标/转仓/批量分析/财报
// 回填各自成 composable，子组件只做展示与交互绑定。
const { isUnmounted } = useAliveGuard()
const { loadExchangeRates } = useExchangeRates()

const table = useHoldingsTable({ isUnmounted })
const badges = useSecurityBadges()
const transfer = useTransfer({
  accounts: () => table.state.brokerAccounts,
  accountLabel: table.accountLabel,
  reload: () => table.loadHoldings({ force: true })
})
const batch = useBatchAnalysis({
  isUnmounted,
  holdings: () => table.state.holdings,
  refreshAnalyses: badges.loadAnalyses
})
const digest = useDigestBackfill({ isUnmounted })

// 刷新页面/切走再回来时接上进行中的批量任务（分析与回填互斥，最多一个在飞）
async function attachToActiveBatchJob() {
  try {
    const response = await api.listActiveAnalysisJobs()
    const jobs = (response.data || []) as AnalysisBatchJob[]
    const analysis = jobs.find((job) => job.type === 'security_analysis_batch')
    if (analysis?.id && !isUnmounted()) {
      batch.adopt(analysis)
      await batch.watchJob(analysis.id)
      return
    }
    const digestJob = jobs.find((job) => job.type === 'report_digest_batch')
    if (digestJob?.id && !isUnmounted()) {
      digest.adopt(digestJob as DigestBatchJob)
      await digest.watchJob(digestJob.id)
    }
  } catch {
    // 恢复失败静默：不打断持仓主流程
  }
}

onMounted(async () => {
  await Promise.all([loadExchangeRates(), table.loadHoldings(), table.loadBrokerAccounts()])
  badges.loadEvents()
  badges.loadAnalyses()
  badges.loadOpinions()
  batch.loadTargetCount()
  attachToActiveBatchJob()
})
</script>

<style scoped>
/* 批量进度块的通用外观走 styles.css 的 .job-progress 套件；这里只留批量特有的 */
.batch-recent {
  margin-top: 6px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  font-size: 12px;
  color: var(--app-text-muted);
}

.batch-recent-failed {
  color: var(--app-danger);
}

.holdings-page {
  width: 100%;
}

.holdings-card {
  overflow: hidden;
}

.market-select {
  width: 150px;
}

@media (max-width: 900px) {
  .header-actions {
    align-items: stretch;
    width: 100%;
  }

  .market-select {
    width: 100%;
  }
}

@media (max-width: 640px) {
  .holdings-card :deep(.el-card__header) {
    text-align: center;
  }
}
</style>
