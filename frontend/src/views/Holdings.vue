<template>
  <NConfigProvider :locale="zhCN" :date-locale="dateZhCN">
    <div class="holdings-page">
      <header class="holdings-heading">
        <div>
          <h1 class="page-title">当前持仓</h1>
        </div>
        <div class="heading-actions">
          <NPopover
            v-model:show="researchVisible"
            trigger="click"
            placement="bottom-end"
            :width="248"
            :to="false"
          >
            <template #trigger>
              <NButton
                ref="researchTrigger"
                quaternary
                :loading="batch.starting || digest.starting"
                aria-label="打开持仓研究任务"
                :aria-expanded="researchVisible"
                aria-controls="research-actions"
                @keydown.down.prevent="focusResearchAction"
                @keydown.esc.stop.prevent="closeResearch"
              >
                研究任务 <ArrowDown class="menu-arrow" />
              </NButton>
            </template>
            <div
              id="research-actions"
              role="group"
              aria-label="持仓研究任务"
              class="research-actions"
              @keydown.esc.stop.prevent="closeResearch"
            >
              <NButton
                ref="analysisAction"
                block
                tabindex="0"
                data-testid="analyze-all-button"
                :disabled="researchDisabled"
                :aria-disabled="researchDisabled"
                @click="runResearch('analysis')"
                >批量分析持仓</NButton
              >
              <NButton
                block
                tabindex="0"
                data-testid="digest-backfill-button"
                :disabled="researchDisabled"
                :aria-disabled="researchDisabled"
                @click="runResearch('digest')"
                >财报摘要回填</NButton
              >
              <p v-if="batch.analyzableCount === 0" class="research-hint">
                当前持仓没有 A股/美股/港股 标的
              </p>
            </div>
          </NPopover>
          <NButton type="primary" :loading="table.state.refreshing" @click="table.refreshPrices">
            <template #icon><Refresh /></template>刷新价格
          </NButton>
        </div>
      </header>
      <JobProgressCard
        v-if="batch.job"
        :job="batch.job"
        :status-text="batch.statusText"
        :percent="batch.percent"
        :progress-status="batch.progressStatus"
        :eta-text="batch.etaText"
        :active="batch.isActive"
        hint="「停止查看进度」只停止本页轮询，后台任务会继续运行并继续消耗 token；重新进入持仓页可继续查看。"
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
        hint="「停止查看进度」只停止本页轮询，后台任务会继续运行；重新进入持仓页可继续查看。"
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

      <section class="holdings-detail" aria-label="持仓明细">
        <div class="holdings-filters">
          <NInput
            v-model:value="table.state.keyword"
            placeholder="搜索代码 / 名称"
            clearable
            class="filter-keyword"
            :input-props="searchInputProps"
          >
            <template #prefix><Search class="filter-icon" /></template>
          </NInput>
          <NSelect
            :value="table.state.selectedMarket"
            :options="marketOptions"
            class="filter-market"
            data-testid="holdings-market-filter"
            :input-props="{ 'aria-label': '筛选市场' }"
            @update:value="selectMarket"
          />
          <NSelect
            :value="table.state.selectedAccount || null"
            :options="accountOptions"
            placeholder="全部账户"
            filterable
            clearable
            class="filter-account"
            data-testid="holdings-account-filter"
            :input-props="{ 'aria-label': '筛选账户' }"
            @update:value="(value) => (table.state.selectedAccount = value ?? '')"
          />
          <NSelect
            v-model:value="table.state.selectedTags"
            multiple
            max-tag-count="responsive"
            filterable
            clearable
            :options="tagOptions"
            :placeholder="table.tagOptions.length ? '按标签筛选' : '暂无可筛选的标签'"
            :disabled="!table.tagOptions.length && !table.state.selectedTags.length"
            class="filter-tags"
            data-testid="holdings-tag-filter"
            :input-props="{ 'aria-label': '筛选持仓标签' }"
          />
          <div class="holdings-view-tools">
            <HelpTip label="查看持仓筛选范围"
              >搜索与标签只筛选明细；上方汇总按账户与市场范围计算。</HelpTip
            >
            <div
              class="holdings-view-mode"
              data-testid="holdings-view-mode"
              role="group"
              aria-label="持仓视图"
            >
              <button
                v-for="mode in [
                  { value: 'merged', label: '按标的' },
                  { value: 'account', label: '按账户' }
                ]"
                :key="mode.value"
                type="button"
                :aria-pressed="table.state.viewMode === mode.value"
                @click="table.setViewMode(mode.value as HoldingsViewMode)"
              >
                {{ mode.label }}
              </button>
            </div>
          </div>
          <span v-if="table.isFiltered" class="filter-count" data-testid="holdings-filter-count">
            筛选出 {{ table.rows.length }} / {{ table.totalRowCount }}
            <NButton text size="small" type="primary" @click="table.clearFilters">清除筛选</NButton>
          </span>
        </div>
        <NAlert
          v-if="notHeldFocus"
          type="warning"
          closable
          class="focus-alert"
          data-testid="holdings-not-held"
          @close="clearDeepLink"
        >
          <template #header>
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
        </NAlert>

        <HoldingsTable :table="table" :badges="badges" @transfer="transfer.openDialog" />
      </section>

      <TransferDialog :transfer="transfer" />
    </div>
  </NConfigProvider>
</template>

<script setup lang="ts">
import HelpTip from '@/components/HelpTip.vue'
import {
  NAlert,
  NButton,
  NConfigProvider,
  NPopover,
  NInput,
  NSelect,
  zhCN,
  dateZhCN
} from 'naive-ui'
import type { AnalysisBatchJob, DigestBatchJob } from '@/types'
import { accountOptionLabel, UNASSIGNED_ACCOUNT, UNASSIGNED_ACCOUNT_LABEL } from '@/utils/labels'
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter, type LocationQuery } from 'vue-router'
import { ChevronDown as ArrowDown, RefreshCw as Refresh, Search } from '@lucide/vue'
import api from '../api'
import { useAliveGuard } from '../composables/useAliveGuard'
import { useAutoReload } from '../composables/useAutoReload'
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
import type { HoldingFocus, HoldingsViewMode } from './holdings/useHoldingsTable'

// 壳层职责（issue #140）：页头（刷新/批量按钮 + 账户/市场过滤）、两个批量
// job 的进度块、以及五个 feature 的编排。持仓数据/角标/转仓/批量分析/财报
// 回填各自成 composable，子组件只做展示与交互绑定。
const { isUnmounted } = useAliveGuard()
const { loadExchangeRates } = useExchangeRates()

const badges = useSecurityBadges()
const table = useHoldingsTable({ isUnmounted, tagSourceOf: badges.tagSourceOf })
const searchInputProps = { 'aria-label': '搜索代码或名称', 'data-testid': 'holdings-search' }
const researchVisible = ref(false)
const researchTrigger = ref<InstanceType<typeof NButton> | null>(null)
const analysisAction = ref<InstanceType<typeof NButton> | null>(null)
const researchDisabled = computed(
  () => batch.analyzableCount === 0 || batch.isActive || digest.isActive
)
function closeResearch() {
  researchVisible.value = false
  researchTrigger.value?.$el.focus()
}
async function focusResearchAction() {
  researchVisible.value = true
  await nextTick()
  if (!researchDisabled.value) analysisAction.value?.$el.focus()
}
function runResearch(action: 'analysis' | 'digest') {
  if (researchDisabled.value) return
  closeResearch()
  return action === 'analysis' ? batch.analyzeAll() : digest.backfillAll()
}
const accountOptions = computed(() => [
  { label: UNASSIGNED_ACCOUNT_LABEL, value: UNASSIGNED_ACCOUNT },
  ...table.state.brokerAccounts.map((account) => ({
    label: accountOptionLabel(account),
    value: account.id
  }))
])
const marketOptions = [
  { label: '全部市场', value: '' },
  ...MARKETS.map((market) => ({ label: market, value: market }))
]
const tagOptions = computed(() =>
  table.tagOptions.map((group) => ({
    type: 'group' as const,
    label: group.label,
    key: group.group,
    children: group.options.map((option) => ({
      label: `${option.label} · ${option.count}`,
      value: option.value
    }))
  }))
)
function selectMarket(market: string) {
  if (table.state.selectedMarket === market) return
  table.state.selectedMarket = market
  table.loadHoldings()
}
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
    const jobs = response.data || []
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
  if (!focus || table.state.loading || table.state.loadError || table.focusHeld) return null
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

// 报价由后端交易时段每 15 分钟刷新；页面可见时每 5 分钟静默重读持仓（只读库）。
// 正在改价时跳过，免得重读覆盖输入框
useAutoReload(() => table.loadHoldings({ force: true, silent: true }), {
  paused: () => table.state.editingRowKey !== null || table.state.loading
})

onMounted(async () => {
  await Promise.all([loadExchangeRates(), table.loadHoldings(), table.loadBrokerAccounts()])
  deepLinkReady = true
  if (!isUnmounted()) await applyDeepLink()
  badges.loadEvents()
  badges.loadAnnouncements()
  badges.loadAnalyses()
  badges.loadOpinions()
  badges.loadIndustries()
  batch.loadTargetCount()
  attachToActiveBatchJob()
})
</script>

<style scoped>
.holdings-page {
  width: 100%;
  padding: 12px 0 24px;
}
.holdings-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
  margin-bottom: 18px;
}
.heading-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 16px;
  margin-bottom: 22px;
}
.menu-arrow {
  width: 12px;
  height: 12px;
  margin-left: 6px;
}
.research-actions {
  display: grid;
  gap: 8px;
}
.research-hint {
  margin: 0;
  color: var(--app-text-muted);
  font-size: 12px;
}
.holdings-view-tools {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
}
.holdings-view-mode {
  display: flex;
  background: var(--app-surface-secondary);
  border-radius: 4px;
  padding: 3px;
  flex-shrink: 0;
}
.holdings-view-mode button {
  border: 0;
  background: transparent;
  color: var(--app-text-muted);
  padding: 4px 12px;
  cursor: pointer;
  font: inherit;
  font-size: 13px;
  line-height: 20px;
  min-height: 30px;
  border-radius: 3px;
}
.holdings-view-mode button[aria-pressed='true'] {
  background: var(--app-surface);
  color: var(--app-text);
}
.holdings-filters {
  display: grid;
  grid-template-columns: minmax(140px, 1.2fr) minmax(100px, 0.6fr) repeat(2, minmax(0, 1fr)) auto;
  gap: 10px;
  align-items: center;
  margin-bottom: 12px;
}
.filter-keyword,
.filter-market,
.filter-account,
.filter-tags {
  width: 100%;
  min-width: 0;
}
.filter-icon {
  width: 17px;
  height: 17px;
}
.filter-count {
  grid-column: 1 / -1;
  font-size: 13px;
  color: var(--app-text-muted);
  display: flex;
  gap: 12px;
  align-items: center;
}
.focus-alert {
  margin-bottom: 12px;
}
.focus-alert-link {
  margin-left: 8px;
  color: var(--app-primary);
}
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
@media (min-width: 1025px) {
  .holdings-page {
    padding: 4px 0 var(--app-space-md);
  }
  .holdings-heading {
    margin-bottom: var(--app-space-md);
  }
  .heading-actions {
    margin-bottom: var(--app-space-sm);
  }
}
@media (max-width: 900px) {
  .holdings-filters {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .holdings-view-tools {
    grid-column: 1 / -1;
  }
}
@media (max-width: 640px) {
  .holdings-page {
    padding: 8px 0 20px;
  }
  .holdings-heading {
    gap: 12px;
    align-items: flex-start;
    margin-bottom: 14px;
  }
  .heading-actions {
    flex-direction: column-reverse;
    align-items: flex-end;
    gap: 4px 12px;
    margin: 0;
  }
  .holdings-view-mode button {
    min-height: 44px;
  }
}
</style>
