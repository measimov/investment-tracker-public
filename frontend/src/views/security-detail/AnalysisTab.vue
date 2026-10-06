<script setup lang="ts">
/**
 * 「分析」tab：AI 分析（元信息条 + 正文）、格雷厄姆准则、商业画像与同业。
 */
import { computed, h } from 'vue'
import {
  NAlert,
  NButton,
  NDataTable,
  NEmpty,
  NSkeleton,
  NTag,
  type DataTableColumns
} from 'naive-ui'
import { RouterLink } from 'vue-router'
import ResearchNote from './ResearchNote.vue'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { renderMarkdown } from '@/utils/markdown'
import { EMPTY, formatDateTime, formatPlainPercent } from '@/utils/helpers'
import {
  analysisTagType,
  outputAdjustmentText,
  riskAdjustmentText,
  riskLabel,
  riskTagType
} from './analysisTags'
import { daysAgoText, isAnalysisOutdated } from './format'
import {
  GRAHAM_VERDICT_LABELS,
  grahamCriterionLabel,
  humanizeAnalysisMarkdown
} from './analysisGlossary'
import {
  grahamBasisText,
  grahamSupplementText,
  grahamSupplementTitle,
  grahamSummaryTagType
} from './grahamFormat'
import type { GrahamCriterion, ProfileRow, SecurityProfileState } from './types'

const props = defineProps<{ state: SecurityProfileState; market: string }>()

const emit = defineEmits<{ retry: [] }>()

const analysis = computed(() => props.state.analysis)
const outdated = computed(() =>
  isAnalysisOutdated(analysis.value?.created_at, props.state.latestDataAt)
)
// 风险等级按市场下限上调（港股 low→medium）时在风险标签旁提示，不静默改写模型判断
const riskAdjustment = computed(() => riskAdjustmentText(analysis.value?.risk_level_adjusted))
// 解析层丢弃/归一标签、补免责声明时提示（#287），不静默改写模型输出
const outputAdjustment = computed(() => outputAdjustmentText(analysis.value?.output_adjustments))

const businessProfile = computed<ProfileRow | null>(() => props.state.business?.profile || null)
const peers = computed<ProfileRow[]>(() => props.state.business?.peers || [])
const peerIndustry = computed(() => props.state.business?.industry || '')

// 商业画像四段文字（其余为数组，单独渲染）
const PROFILE_TEXT_FIELDS = ['商业模式', '行业与竞争', '供应商集中度', '客户集中度']

function profileList(key: string): ProfileRow[] {
  const value = businessProfile.value?.[key]
  return Array.isArray(value) ? value : []
}

// 格雷厄姆防御型准则 × 塔勒布脆弱性信号（graham_screen 预计算）；准则名/判定词与 AI 正文共用
function grahamVerdictTag(verdict: string) {
  if (verdict === 'pass') return 'success'
  if (verdict === 'fail') return 'danger'
  return 'info'
}
const grahamScreen = computed(() => props.state.grahamScreen || {})
const grahamCriteria = computed(() => (grahamScreen.value.criteria || []) as GrahamCriterion[])
const grahamSummaryType = computed(() => grahamSummaryTagType(grahamScreen.value))
const isMobileView = useMediaQuery('(max-width: 640px)')
// 脆弱性信号拼展示串：只列有值的量化项，note 类字段原样透传
const fragilityParts = computed(() => {
  const fragility = (grahamScreen.value.fragility || {}) as Record<string, unknown>
  const parts: string[] = []
  if (typeof fragility.debt_to_assets === 'number')
    parts.push(`总负债率 ${formatPlainPercent(fragility.debt_to_assets * 100, 1)}`)
  if (typeof fragility.net_debt_to_assets === 'number')
    parts.push(
      `净债务/总资产 ${formatPlainPercent(fragility.net_debt_to_assets * 100, 1)}` +
        (fragility.net_debt_to_assets < 0 ? '（净现金）' : '')
    )
  if (typeof fragility.interest_coverage === 'number')
    parts.push(`利息覆盖 ${fragility.interest_coverage.toFixed(1)} 倍`)
  if (typeof fragility.net_cash_to_market_cap === 'number')
    parts.push(`净现金/市值 ${formatPlainPercent(fragility.net_cash_to_market_cap * 100, 1)}`)
  if (typeof fragility.interest_coverage_note === 'string')
    parts.push(fragility.interest_coverage_note)
  return parts
})

function naiveTagType(value: string): 'default' | 'error' | 'success' | 'warning' {
  if (value === 'danger') return 'error'
  if (value === 'success' || value === 'warning') return value
  return 'default'
}
const grahamColumns: DataTableColumns<GrahamCriterion> = [
  {
    title: '准则',
    key: 'criterion',
    width: 150,
    render: (row) => grahamCriterionLabel(row.criterion)
  },
  {
    title: '判定',
    key: 'verdict',
    width: 90,
    render: (row) =>
      h(
        NTag,
        { type: naiveTagType(grahamVerdictTag(row.verdict)), size: 'small', bordered: false },
        () => GRAHAM_VERDICT_LABELS[row.verdict] || row.verdict
      )
  },
  {
    title: '依据',
    key: 'reason',
    minWidth: 360,
    render: (row) =>
      h('div', [
        h('span', { class: row.verdict === 'fail' ? 'sd-red-flag' : '' }, row.reason),
        grahamBasisText(row.basis)
          ? h(
              'div',
              { class: 'graham-basis', 'data-testid': 'graham-basis' },
              grahamBasisText(row.basis)
            )
          : null,
        row.supplement
          ? h('div', { class: 'graham-basis', 'data-testid': 'graham-supplement' }, [
              grahamSupplementText(row.supplement),
              h(ResearchNote, {
                popover: true,
                label: '查看补充估值口径',
                text: grahamSupplementTitle(row.supplement)
              })
            ])
          : null
      ])
  }
]
</script>

<template>
  <!-- AI 分析 -->
  <section class="sd-block analysis-section" data-testid="analysis-section" aria-label="AI 分析">
    <div v-if="state.analysisLoading" role="status" aria-label="正在加载AI分析">
      <NSkeleton text :repeat="5" />
    </div>
    <NAlert
      v-if="state.analysisError"
      type="error"
      class="analysis-error"
      data-testid="analysis-load-error"
    >
      {{
        state.analysisHasLoaded && analysis
          ? '分析刷新失败，保留此标的上次成功正文。'
          : 'AI分析加载失败，当前是否有分析未知。'
      }}
      {{ state.analysisError }}
      <NButton :loading="state.analysisLoading" :disabled="state.generating" @click="emit('retry')"
        >重试分析</NButton
      >
    </NAlert>
    <template v-if="!state.analysisLoading && analysis">
      <div class="sd-meta-row">
        <div class="analysis-tags">
          <NTag
            :type="naiveTagType(riskTagType(analysis.risk_level))"
            size="small"
            :bordered="false"
            data-testid="risk-level-tag"
          >
            风险 {{ riskLabel(analysis.risk_level) }}
          </NTag>
          <ResearchNote
            v-if="riskAdjustment"
            popover
            label="查看风险等级调整依据"
            caption="已上调"
            :text="riskAdjustment"
            data-testid="risk-level-adjusted"
          />
          <ResearchNote
            v-if="outputAdjustment"
            popover
            label="查看分析输出调整依据"
            caption="已调整"
            :text="outputAdjustment"
            data-testid="analysis-output-adjusted"
          />
          <NTag
            v-for="tag in analysis.tags"
            :key="tag"
            :type="naiveTagType(analysisTagType(tag))"
            size="small"
            :bordered="false"
          >
            {{ tag }}
          </NTag>
        </div>
        <div class="analysis-timing">
          <span class="sd-meta-time" data-testid="analysis-freshness">
            AI 生成于 {{ formatDateTime(analysis.created_at) }}
            <template v-if="daysAgoText(analysis.created_at)"
              >（{{ daysAgoText(analysis.created_at) }}）</template
            >
          </span>
          <ResearchNote
            v-if="outdated"
            popover
            label="查看分析时效依据"
            caption="可能过期"
            style="color: var(--app-warning-text)"
            :text="`分析生成之后又有新的财报摘要或报表抽取（最新 ${formatDateTime(state.latestDataAt)}），建议重新生成`"
            data-testid="analysis-outdated"
          />
        </div>
      </div>
      <div class="analysis-reading">
        <p class="analysis-summary">{{ analysis.summary }}</p>
        <!-- LLM Markdown 必须过 renderMarkdown（marked + DOMPurify 消毒）后才可 v-html；
           humanizeAnalysisMarkdown 先把存量报告里的字段名/英文判定词换成中文（只改展示） -->
        <div
          class="markdown-body"
          v-html="renderMarkdown(humanizeAnalysisMarkdown(analysis.content))"
        />
      </div>
      <div class="sd-footnote">
        {{ analysis.model || EMPTY }} · {{ analysis.total_tokens ?? EMPTY }} tokens
        <template v-if="analysis.data_fetched_at">
          · 数据获取于 {{ formatDateTime(analysis.data_fetched_at) }}</template
        >
      </div>
    </template>
    <NEmpty
      v-if="!state.analysisLoading && !analysis && !state.analysisError && state.analysisHasLoaded"
      description="暂无 AI 分析；点击右上角生成（将同步基本面数据并调用 LLM）"
    />
  </section>

  <!-- 格雷厄姆防御型准则 × 塔勒布脆弱性信号 -->
  <div v-if="state.profileLoading" role="status" aria-label="正在加载格雷厄姆准则">
    <NSkeleton text :repeat="3" />
  </div>
  <section
    v-else-if="grahamScreen.status === 'ok'"
    class="sd-block"
    data-testid="graham-screen-section"
  >
    <div class="sd-block-header">
      <h3 class="sd-block-title">
        格雷厄姆防御型准则
        <NTag size="small" :bordered="false" :type="naiveTagType(grahamSummaryType)">
          达标 {{ grahamScreen.passed }} / {{ grahamCriteria.length }}
        </NTag>
        <span class="sd-title-note">数据年度 {{ grahamScreen.as_of_year }}</span>
      </h3>
    </div>
    <ul v-if="isMobileView" class="graham-list" role="list" aria-label="格雷厄姆准则列表">
      <li v-for="criterion in grahamCriteria" :key="criterion.criterion" class="graham-item">
        <div class="graham-item-header">
          <h4>{{ grahamCriterionLabel(criterion.criterion) }}</h4>
          <NTag
            :type="naiveTagType(grahamVerdictTag(criterion.verdict))"
            size="small"
            :bordered="false"
            >{{ GRAHAM_VERDICT_LABELS[criterion.verdict] || criterion.verdict }}</NTag
          >
        </div>
        <p class="graham-reason" :class="{ 'sd-red-flag': criterion.verdict === 'fail' }">
          {{ criterion.reason }}
        </p>
        <p v-if="grahamBasisText(criterion.basis)" class="graham-basis" data-testid="graham-basis">
          {{ grahamBasisText(criterion.basis) }}
        </p>
        <div v-if="criterion.supplement" class="graham-basis" data-testid="graham-supplement">
          {{ grahamSupplementText(criterion.supplement) }}
          <ResearchNote
            popover
            label="查看补充估值口径"
            :text="grahamSupplementTitle(criterion.supplement)"
          />
        </div>
      </li>
    </ul>
    <div
      v-else
      class="sd-table-scroll"
      tabindex="0"
      role="region"
      aria-label="格雷厄姆准则表格，可横向滚动"
    >
      <NDataTable
        class="sd-data-table graham-table"
        :columns="grahamColumns"
        :data="grahamCriteria"
        :bordered="false"
        :style="{ minWidth: '680px' }"
      />
    </div>
    <div class="sd-footnote graham-footnote">
      估值两项按 TTM 判定：A股 为 Tushare 快照；港股/美股 为行情库最新收盘价 ÷
      报表推算的滚动每股盈利（年报 + 更新的中报/季报 − 上年同期），每股净资产取最近一期报表、
      股数由净利润 ÷ 每股盈利估算，报表币种按价格日汇率折算。年报口径与三年均值 PE 仅供参考。
    </div>
    <div v-if="fragilityParts.length" class="sd-footnote graham-footnote">
      脆弱性信号：{{ fragilityParts.join('；') }}
      （口径与含义见 AI 分析「非对称性与脆弱性」章节；不可判定 = 数据源边界，非达标）
    </div>
  </section>
  <section
    v-else-if="grahamScreen.status === 'no_data'"
    class="sd-block"
    data-testid="graham-screen-empty"
  >
    <div class="sd-block-header">
      <h3 class="sd-block-title">格雷厄姆防御型准则</h3>
    </div>
    <NEmpty description="暂无年度报表数据，准则无法判定；生成 AI 分析或补齐摘要后自动计算" />
  </section>

  <!-- 商业画像 -->
  <section class="sd-block business-profile-section" data-testid="business-profile-section">
    <div class="sd-block-header business-profile-header">
      <h3 class="sd-block-title">商业画像</h3>
      <ResearchNote
        label="查看商业画像与同业口径"
        text="由财报摘要与年报业务章节合成，随新报告期刷新；同业仅列名单，不做对比分析"
      />
    </div>
    <div v-if="state.profileLoading" role="status" aria-label="正在加载商业画像">
      <NSkeleton text :repeat="5" />
    </div>
    <dl v-else-if="businessProfile" class="sd-field-list business-field-list">
      <template v-for="field in PROFILE_TEXT_FIELDS" :key="field">
        <dt>{{ field }}</dt>
        <dd class="business-narrative">{{ businessProfile[field] || EMPTY }}</dd>
      </template>

      <dt>业务分部</dt>
      <dd>
        <div
          v-if="profileList('业务分部').length"
          class="segment-list"
          data-testid="business-segments"
        >
          <article
            v-for="(segment, index) in profileList('业务分部')"
            :key="index"
            class="segment-item"
          >
            <div class="segment-summary">
              <h4 class="segment-name">{{ segment['名称'] ?? EMPTY }}</h4>
              <dl class="segment-metrics">
                <div v-for="metric in ['收入占比', '毛利率']" :key="metric">
                  <dt>{{ metric }}</dt>
                  <dd>{{ segment[metric] ?? EMPTY }}</dd>
                </div>
              </dl>
            </div>
            <div class="segment-trend">
              <span class="segment-label">趋势与依据</span>
              <p>{{ segment['趋势'] ?? EMPTY }}</p>
            </div>
          </article>
        </div>
        <span v-else class="sd-muted">{{ EMPTY }}</span>
      </dd>

      <dt>估值观察因子</dt>
      <dd>
        <ul v-if="profileList('估值观察因子').length" class="sd-inline-list">
          <li v-for="(item, index) in profileList('估值观察因子')" :key="index">
            <strong>{{ item['因子'] }}</strong>
            <NTag v-if="item['方向']" size="small" type="default" :bordered="false">{{
              item['方向']
            }}</NTag>
            <span v-if="item['传导']" class="sd-muted"> {{ item['传导'] }}</span>
          </li>
        </ul>
        <span v-else class="sd-muted">{{ EMPTY }}</span>
      </dd>

      <dt>上游依赖</dt>
      <dd>
        <ul v-if="profileList('上游依赖').length" class="sd-inline-list">
          <li v-for="(item, index) in profileList('上游依赖')" :key="index">
            <strong>{{ item['要素'] }}</strong
            >：{{ item['影响'] || EMPTY }}
          </li>
        </ul>
        <span v-else class="sd-muted">{{ EMPTY }}</span>
      </dd>

      <dt>下游需求</dt>
      <dd>
        <ul v-if="profileList('下游需求').length" class="sd-inline-list">
          <li v-for="(item, index) in profileList('下游需求')" :key="index">
            <strong>{{ item['客群或场景'] || item['客群/场景'] || EMPTY }}</strong
            >：{{ item['需求驱动'] || EMPTY }}
          </li>
        </ul>
        <span v-else class="sd-muted">{{ EMPTY }}</span>
      </dd>
    </dl>
    <NEmpty
      v-else
      :description="
        state.profileError && !state.profileHasLoaded
          ? '商业画像暂不可用，请重试档案'
          : '暂无商业画像；生成分析时自动合成（需先有财报摘要或业务章节）'
      "
    />

    <div v-if="peers.length" class="peer-list" data-testid="peer-list">
      <span class="sd-muted">同业（{{ peerIndustry || '同行业' }}）：</span>
      <template v-for="peer in peers" :key="peer.symbol || peer.name">
        <RouterLink
          v-if="peer.symbol"
          :to="{ name: 'SecurityDetail', params: { market, symbol: String(peer.symbol) } }"
          class="peer-item peer-link"
          >{{ peer.name || peer.symbol }}</RouterLink
        >
        <span v-else class="peer-item sd-muted">{{ peer.name }}</span>
      </template>
    </div>
  </section>
</template>

<style scoped>
.business-profile-section {
  max-width: 38em;
  margin-inline: auto;
  font-size: 17px;
  background: transparent;
  border: 0;
  border-radius: 0;
  padding: 0;
}
.business-field-list {
  grid-template-columns: minmax(0, 1fr);
  gap: 4px;
  font-family: var(--app-font-sans);
  font-size: 14px;
}
.business-field-list > dd + dt {
  margin-top: 20px;
}
.business-field-list > .business-narrative {
  font-family: var(--app-font-serif);
  font-size: 17px;
  font-weight: 400;
  line-height: 1.8;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.business-profile-section :deep(.sd-note) {
  font-size: 13px;
}
.business-profile-header {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  align-items: start;
  gap: 6px;
}
.business-profile-header .sd-block-title {
  min-height: 24px;
}
@media (max-width: 640px) {
  .business-profile-header .sd-block-title {
    min-height: 44px;
  }
}
.analysis-section {
  max-width: 38em;
  margin-inline: auto;
  font-size: 18px;
  background: transparent;
  border: 0;
  border-radius: 0;
  padding: 0;
}
.analysis-section .sd-meta-row {
  font-size: 13px;
  margin-bottom: 16px;
}
.analysis-tags,
.analysis-timing {
  display: flex;
  align-items: center;
  gap: 6px 8px;
  flex-wrap: wrap;
  min-width: 0;
}
.analysis-section .sd-meta-time,
.analysis-section > .sd-footnote,
.analysis-section :deep(.n-tag),
.analysis-section :deep(.note-trigger) {
  font-size: 13px;
}
.analysis-reading {
  text-align: left;
}
.analysis-reading .markdown-body {
  font-size: inherit;
  line-height: 1.8;
}
.analysis-reading :deep(h1),
.analysis-reading :deep(h2),
.analysis-reading :deep(h3) {
  font-weight: 600;
  text-align: left;
}
.analysis-reading :deep(h1) {
  font-size: 24px;
}
.analysis-reading :deep(h2) {
  font-size: 22px;
}
.analysis-reading :deep(h3) {
  font-size: 19px;
}
.analysis-summary {
  margin: 0 0 24px;
  font-family: var(--app-font-serif);
  font-size: 19px;
  font-weight: 500;
  line-height: 1.65;
  color: var(--app-text);
  overflow-wrap: anywhere;
}
@media (max-width: 640px) {
  .analysis-section {
    font-size: 17px;
  }
  .analysis-summary {
    font-size: 18px;
  }
  .analysis-reading :deep(h1) {
    font-size: 22px;
  }
  .analysis-reading :deep(h2) {
    font-size: 20px;
  }
  .analysis-reading :deep(h3) {
    font-size: 18px;
  }
}
.analysis-error {
  margin-bottom: 16px;
}
.analysis-error .n-button {
  margin-left: 12px;
}
.peer-link {
  color: var(--app-primary-strong);
  text-decoration: none;
}
.peer-link:hover {
  text-decoration: underline;
}
.graham-table :deep(td) {
  font-size: 14px;
  line-height: 1.8;
  vertical-align: top;
  overflow-wrap: anywhere;
}
.graham-list {
  margin: 0;
  padding: 0;
  list-style: none;
}
.graham-item {
  padding-block: 12px;
  font-size: 14px;
  line-height: 1.8;
  overflow-wrap: anywhere;
}
.graham-item + .graham-item {
  border-top: 1px solid var(--app-border-soft);
}
.graham-item-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}
.graham-item-header h4 {
  margin: 0;
  min-width: 0;
  font-size: 14px;
  font-weight: 600;
}
.graham-item-header :deep(.n-tag) {
  flex-shrink: 0;
}
.graham-reason {
  margin: 6px 0 0;
}
.graham-basis,
:deep(.graham-basis) {
  margin-bottom: 0;
  margin-top: 6px;
  font-size: 13px;
  color: var(--app-text-muted);
  line-height: 1.8;
  font-variant-numeric: tabular-nums;
}
.graham-table :deep(.note-trigger),
.graham-item :deep(.note-trigger) {
  font-size: 13px;
}
.graham-footnote {
  font-size: 13px;
  line-height: 1.8;
}

.peer-list {
  margin-top: 12px;
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.segment-list {
  container-type: inline-size;
  display: grid;
  gap: 10px;
}

.segment-item {
  min-width: 0;
}
.segment-item + .segment-item {
  padding-top: 16px;
  border-top: 1px solid var(--app-border-soft);
}

.segment-summary {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 2fr);
  gap: 10px 20px;
  align-items: start;
}

.segment-name {
  margin: 0;
  font-family: var(--app-font-sans);
  font-size: 14px;
  font-weight: 600;
}

.segment-metrics {
  margin: 0;
  font-family: var(--app-font-sans);
  font-size: 14px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 20px;
}

.segment-metrics dt,
.segment-label {
  font-size: 13px;
  color: var(--app-text-muted);
}

.segment-metrics dd {
  margin: 2px 0 0;
  font-variant-numeric: tabular-nums;
}

.segment-trend {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--app-border-soft);
}

.segment-trend p {
  margin: 2px 0 0;
  font-family: var(--app-font-serif);
  font-size: 17px;
  font-weight: 400;
  line-height: 1.8;
}

.segment-name,
.segment-metrics dd,
.segment-trend p {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

@container (max-width: 560px) {
  .segment-summary {
    grid-template-columns: minmax(0, 1fr);
  }
}

@container (max-width: 320px) {
  .segment-metrics {
    grid-template-columns: minmax(0, 1fr);
  }
}

.peer-item {
  font-size: 13px;
}

.sd-inline-list .el-tag {
  margin: 0 4px;
}
</style>
