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

      <!-- 搜索与标签筛选只收窄表格行，不改汇总口径；组内 OR、组间 AND（见 holdings/filters.ts） -->
      <div class="holdings-filters">
        <el-input
          v-model="table.state.keyword"
          :prefix-icon="Search"
          placeholder="搜索代码 / 名称"
          clearable
          class="filter-keyword"
          data-testid="holdings-search"
        />
        <el-select
          v-model="table.state.selectedTags"
          multiple
          collapse-tags
          collapse-tags-tooltip
          filterable
          clearable
          :placeholder="table.tagOptions.length ? '按标签筛选' : '暂无可筛选的标签'"
          :disabled="!table.tagOptions.length && !table.state.selectedTags.length"
          class="filter-tags"
          data-testid="holdings-tag-filter"
        >
          <el-option-group
            v-for="group in table.tagOptions"
            :key="group.group"
            :label="group.label"
          >
            <el-option
              v-for="option in group.options"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            >
              <span class="tag-option">
                <span :class="option.tone ? `tag-tone-${option.tone}` : ''">{{
                  option.label
                }}</span>
                <span class="tag-option-count">{{ option.count }}</span>
              </span>
            </el-option>
          </el-option-group>
        </el-select>
        <span v-if="table.isFiltered" class="filter-count" data-testid="holdings-filter-count">
          筛选出 {{ table.rows.length }} / {{ table.totalRowCount }}
          <el-button type="primary" text size="small" @click="table.clearFilters"
            >清除筛选</el-button
          >
        </span>
      </div>

      <el-alert
        v-if="notHeldFocus"
        type="warning"
        show-icon
        class="focus-alert"
        data-testid="holdings-not-held"
        @close="clearDeepLink"
      >
        <template #title>
          当前未持有 {{ notHeldFocus.symbol
          }}<template v-if="notHeldFocus.market">（{{ notHeldFocus.market }}）</template>
          <router-link
            v-if="notHeldFocus.market"
            :to="securityDetailPath(notHeldFocus)"
            class="focus-alert-link"
            data-testid="holdings-not-held-link"
          >
            查看标的档案
          </router-link>
        </template>
      </el-alert>

      <HoldingsTable :table="table" :badges="badges" @transfer="transfer.openDialog" />
    </el-card>

    <TransferDialog :transfer="transfer" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { useRoute, useRouter, type LocationQuery } from 'vue-router'
import { MagicStick, Notebook, Refresh, Search } from '@element-plus/icons-vue'
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
import type { HoldingFocus, HoldingsViewMode } from './holdings/useHoldingsTable'

// 壳层职责（issue #140）：页头（刷新/批量按钮 + 账户/市场过滤）、两个批量
// job 的进度块、以及五个 feature 的编排。持仓数据/角标/转仓/批量分析/财报
// 回填各自成 composable，子组件只做展示与交互绑定。
const { isUnmounted } = useAliveGuard()
const { loadExchangeRates } = useExchangeRates()

const badges = useSecurityBadges()
const table = useHoldingsTable({ isUnmounted, tagSourceOf: badges.tagSourceOf })
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

// ---- 深链定位：/holdings?symbol=&market=（仪表盘最近交易、交易记录的代码链接）----
const route = useRoute()
const router = useRouter()

function queryText(value: LocationQuery[string]): string {
  const first = Array.isArray(value) ? value[0] : value
  return (first ?? '').trim()
}

function securityDetailPath(focus: HoldingFocus) {
  return `/securities/${encodeURIComponent(focus.market ?? '')}/${encodeURIComponent(focus.symbol)}`
}

/** 深链目标没有持仓（已清仓/从未持有）：持仓已加载完才下结论，避免加载中闪一下 */
const notHeldFocus = computed(() => {
  const focus = table.state.focus
  if (!focus || table.state.loading || table.focusHeld) return null
  return focus
})

async function applyDeepLink() {
  const symbol = queryText(route.query.symbol)
  const market = queryText(route.query.market) || null
  if (!symbol) {
    table.state.focus = null
    return
  }
  table.state.focus = { symbol, market }
  // 关键词填代码而不是切市场：市场下拉会带 ?market= 重新拉持仓，这里尽量保持用户的过滤；
  // 标签筛选清掉，否则目标行可能被标签条件藏起来
  table.state.keyword = symbol
  table.state.selectedTags = []
  // 目标在别的市场（或未给市场且当前市场里没有）→ 市场重置为全部再拉一次
  const currentMarket = table.state.selectedMarket
  if (currentMarket && (market ? currentMarket !== market : !table.focusHeld)) {
    table.state.selectedMarket = ''
    await table.loadHoldings()
  }
  // 账户过滤是前端过滤：目标持仓在别的账户时放开到全部账户
  if (table.focusHeld && !table.focusVisible) table.state.selectedAccount = ''
}

/** 清除深链：去掉定位与关键词，并把 query 从地址栏拿掉（刷新后不会再定位） */
function clearDeepLink() {
  table.state.focus = null
  table.state.keyword = ''
  dropDeepLinkQuery()
}

function dropDeepLinkQuery() {
  if (!('symbol' in route.query) && !('market' in route.query)) return
  const query = { ...route.query }
  delete query.symbol
  delete query.market
  router.replace({ query })
}

// 关键词被改掉（清空或改搜别的）= 放弃定位：去高亮并清 query
watch(
  () => table.state.keyword,
  (keyword) => {
    const focus = table.state.focus
    if (focus && keyword.trim() !== focus.symbol) {
      table.state.focus = null
      dropDeepLinkQuery()
    }
  }
)

// 已在持仓页时再从别处深链进来（同一路由组件复用，onMounted 不会再跑）；
// 首次加载完成前的变化由 onMounted 末尾那次统一处理
let deepLinkReady = false
watch(
  () => [route.query.symbol, route.query.market],
  () => {
    if (deepLinkReady) applyDeepLink()
  }
)

onMounted(async () => {
  await Promise.all([loadExchangeRates(), table.loadHoldings(), table.loadBrokerAccounts()])
  deepLinkReady = true
  if (!isUnmounted()) await applyDeepLink()
  badges.loadEvents()
  badges.loadAnalyses()
  badges.loadOpinions()
  badges.loadIndustries()
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

.holdings-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  margin: 12px 0;
}

.filter-keyword {
  width: 220px;
}

.filter-tags {
  width: 280px;
}

.filter-count {
  font-size: 13px;
  color: var(--app-text-muted);
}

.tag-option {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.tag-option-count {
  color: var(--app-text-soft);
  font-variant-numeric: tabular-nums;
}

.tag-tone-positive {
  color: var(--app-success);
}

.tag-tone-negative {
  color: var(--app-danger);
}

.focus-alert {
  margin-bottom: 12px;
}

.focus-alert-link {
  margin-left: 8px;
  color: var(--app-primary);
}

@media (max-width: 900px) {
  .header-actions {
    align-items: stretch;
    width: 100%;
  }

  .market-select,
  .filter-keyword,
  .filter-tags {
    width: 100%;
  }
}

@media (max-width: 640px) {
  .holdings-card :deep(.el-card__header) {
    text-align: center;
  }
}
</style>
