<script setup lang="ts">
/**
 * 「分析」tab：AI 分析（元信息条 + 正文）、格雷厄姆准则、商业画像与同业。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { InfoFilled } from '@element-plus/icons-vue'
import { renderMarkdown } from '@/utils/markdown'
import { EMPTY, formatDateTime } from '@/utils/helpers'
import { analysisTagType, riskAdjustmentText, riskLabel, riskTagType } from './analysisTags'
import { daysAgoText, isAnalysisOutdated } from './format'
import {
  GRAHAM_VERDICT_LABELS,
  grahamCriterionLabel,
  humanizeAnalysisMarkdown
} from './analysisGlossary'
import { grahamBasisText, grahamSupplementText, grahamSupplementTitle } from './grahamFormat'
import type { GrahamCriterion, ProfileRow, SecurityProfileState } from './types'

const props = defineProps<{ state: SecurityProfileState; market: string }>()

const router = useRouter()

const analysis = computed(() => props.state.analysis)
const outdated = computed(() =>
  isAnalysisOutdated(analysis.value?.created_at, props.state.latestDataAt)
)
// 风险等级按市场下限上调（港股 low→medium）时在风险标签旁提示，不静默改写模型判断
const riskAdjustment = computed(() => riskAdjustmentText(analysis.value?.risk_level_adjusted))

const businessProfile = computed<ProfileRow | null>(() => props.state.business?.profile || null)
const peers = computed<ProfileRow[]>(() => props.state.business?.peers || [])
const peerIndustry = computed(() => props.state.business?.industry || '')

// 商业画像四段文字（其余为数组，单独渲染）
const PROFILE_TEXT_FIELDS = ['商业模式', '行业与竞争', '供应商集中度', '客户集中度']

function profileList(key: string): ProfileRow[] {
  const value = businessProfile.value?.[key]
  return Array.isArray(value) ? value : []
}

function goToPeer(peer: ProfileRow) {
  if (!peer.symbol) return
  router.push({
    name: 'SecurityDetail',
    params: { market: props.market, symbol: String(peer.symbol) }
  })
}

// 格雷厄姆防御型准则 × 塔勒布脆弱性信号（graham_screen 预计算）；准则名/判定词与 AI 正文共用
function grahamVerdictTag(verdict: string) {
  if (verdict === 'pass') return 'success'
  if (verdict === 'fail') return 'danger'
  return 'info'
}
const grahamScreen = computed(() => props.state.grahamScreen || {})
const grahamCriteria = computed(() => (grahamScreen.value.criteria || []) as GrahamCriterion[])
// 全部达标才绿；有不达标 warning；其余 info（不可判定是数据边界，不是负面信号）
const grahamSummaryType = computed(() => {
  const failed = Number(grahamScreen.value.failed || 0)
  const passed = Number(grahamScreen.value.passed || 0)
  if (failed > 0) return 'warning'
  if (passed > 0 && passed === grahamCriteria.value.length) return 'success'
  return 'info'
})
// 脆弱性信号拼展示串：只列有值的量化项，note 类字段原样透传
const fragilityParts = computed(() => {
  const fragility = (grahamScreen.value.fragility || {}) as Record<string, unknown>
  const parts: string[] = []
  if (typeof fragility.debt_to_assets === 'number')
    parts.push(`总负债率 ${(fragility.debt_to_assets * 100).toFixed(1)}%`)
  if (typeof fragility.net_debt_to_assets === 'number')
    parts.push(
      `净债务/总资产 ${(fragility.net_debt_to_assets * 100).toFixed(1)}%` +
        (fragility.net_debt_to_assets < 0 ? '（净现金）' : '')
    )
  if (typeof fragility.interest_coverage === 'number')
    parts.push(`利息覆盖 ${fragility.interest_coverage.toFixed(1)} 倍`)
  if (typeof fragility.net_cash_to_market_cap === 'number')
    parts.push(`净现金/市值 ${(fragility.net_cash_to_market_cap * 100).toFixed(1)}%`)
  if (typeof fragility.interest_coverage_note === 'string')
    parts.push(fragility.interest_coverage_note)
  return parts
})
</script>

<template>
  <!-- AI 分析 -->
  <section class="sd-block" data-testid="analysis-section">
    <div class="sd-block-header">
      <h3 class="sd-block-title">AI 分析</h3>
    </div>
    <template v-if="analysis">
      <div class="sd-meta-row">
        <el-tag
          :type="riskTagType(analysis.risk_level)"
          size="small"
          effect="dark"
          data-testid="risk-level-tag"
        >
          风险 {{ riskLabel(analysis.risk_level) }}
        </el-tag>
        <el-tooltip v-if="riskAdjustment" :content="riskAdjustment" placement="top">
          <el-tag type="info" size="small" effect="plain" data-testid="risk-level-adjusted">
            已上调
          </el-tag>
        </el-tooltip>
        <el-tag
          v-for="tag in analysis.tags"
          :key="tag"
          :type="analysisTagType(tag)"
          size="small"
          effect="light"
        >
          {{ tag }}
        </el-tag>
        <span class="sd-meta-time" data-testid="analysis-freshness">
          生成于 {{ formatDateTime(analysis.created_at) }}
          <template v-if="daysAgoText(analysis.created_at)"
            >（{{ daysAgoText(analysis.created_at) }}）</template
          >
        </span>
        <el-tooltip
          v-if="outdated"
          :content="`分析生成之后又有新的财报摘要或报表抽取（最新 ${formatDateTime(state.latestDataAt)}），建议重新生成`"
          placement="top"
        >
          <el-tag type="warning" size="small" effect="plain" data-testid="analysis-outdated">
            可能过期
          </el-tag>
        </el-tooltip>
        <p class="sd-meta-summary">{{ analysis.summary }}</p>
      </div>
      <!-- LLM Markdown 必须过 renderMarkdown（marked + DOMPurify 消毒）后才可 v-html；
           humanizeAnalysisMarkdown 先把存量报告里的字段名/英文判定词换成中文（只改展示） -->
      <div
        class="markdown-body"
        v-html="renderMarkdown(humanizeAnalysisMarkdown(analysis.content))"
      />
      <div class="sd-footnote">
        {{ analysis.model || EMPTY }} · {{ analysis.total_tokens || EMPTY }} tokens
        <template v-if="analysis.data_fetched_at">
          · 数据获取于 {{ analysis.data_fetched_at }}</template
        >
      </div>
    </template>
    <el-empty
      v-else
      description="暂无 AI 分析；点击右上角生成（将同步基本面数据并调用 LLM）"
      :image-size="72"
    />
  </section>

  <!-- 格雷厄姆防御型准则 × 塔勒布脆弱性信号 -->
  <section v-if="grahamScreen.status === 'ok'" class="sd-block" data-testid="graham-screen-section">
    <div class="sd-block-header">
      <h3 class="sd-block-title">
        格雷厄姆防御型准则
        <el-tag size="small" effect="plain" :type="grahamSummaryType">
          达标 {{ grahamScreen.passed }} / {{ grahamCriteria.length }}
        </el-tag>
        <span class="sd-title-note">数据年度 {{ grahamScreen.as_of_year }}</span>
      </h3>
    </div>
    <el-table :data="grahamCriteria" size="small" stripe>
      <el-table-column label="准则" min-width="150">
        <template #default="{ row }">
          {{ grahamCriterionLabel(row.criterion) }}
        </template>
      </el-table-column>
      <el-table-column label="判定" width="90">
        <template #default="{ row }">
          <el-tag :type="grahamVerdictTag(row.verdict)" size="small">
            {{ GRAHAM_VERDICT_LABELS[row.verdict] || row.verdict }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="依据" min-width="300">
        <template #default="{ row }">
          <span :class="{ 'sd-red-flag': row.verdict === 'fail' }">{{ row.reason }}</span>
          <div v-if="grahamBasisText(row.basis)" class="graham-basis" data-testid="graham-basis">
            {{ grahamBasisText(row.basis) }}
          </div>
          <div
            v-if="row.supplement"
            class="graham-basis"
            :title="grahamSupplementTitle(row.supplement)"
            data-testid="graham-supplement"
          >
            {{ grahamSupplementText(row.supplement) }}
          </div>
        </template>
      </el-table-column>
    </el-table>
    <div class="sd-footnote">
      估值两项按 TTM 判定：A股 为 Tushare 快照；港股/美股 为行情库最新收盘价 ÷
      报表推算的滚动每股盈利（年报 + 更新的中报/季报 − 上年同期），每股净资产取最近一期报表、
      股数由净利润 ÷ 每股盈利估算，报表币种按价格日汇率折算。年报口径与三年均值 PE 仅供参考。
    </div>
    <div v-if="fragilityParts.length" class="sd-footnote">
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
    <el-empty
      description="暂无年度报表数据，准则无法判定；生成 AI 分析或补齐摘要后自动计算"
      :image-size="56"
    />
  </section>

  <!-- 商业画像 -->
  <section class="sd-block" data-testid="business-profile-section">
    <div class="sd-block-header">
      <h3 class="sd-block-title">
        商业画像
        <el-tooltip
          content="由财报摘要与年报业务章节合成，随新报告期刷新；同业仅列名单，不做对比分析"
          placement="top"
        >
          <el-icon class="sd-help"><InfoFilled /></el-icon>
        </el-tooltip>
      </h3>
    </div>
    <dl v-if="businessProfile" class="sd-field-list">
      <template v-for="field in PROFILE_TEXT_FIELDS" :key="field">
        <dt>{{ field }}</dt>
        <dd>{{ businessProfile[field] || EMPTY }}</dd>
      </template>

      <dt>业务分部</dt>
      <dd>
        <el-table
          v-if="profileList('业务分部').length"
          :data="profileList('业务分部')"
          size="small"
          class="segment-table"
        >
          <el-table-column label="名称" prop="名称" min-width="110" />
          <el-table-column label="收入占比" prop="收入占比" width="90" align="right" />
          <el-table-column label="毛利率" prop="毛利率" width="80" align="right" />
          <el-table-column label="趋势" prop="趋势" width="64" />
        </el-table>
        <span v-else class="sd-muted">{{ EMPTY }}</span>
      </dd>

      <dt>估值观察因子</dt>
      <dd>
        <ul v-if="profileList('估值观察因子').length" class="sd-inline-list">
          <li v-for="(item, index) in profileList('估值观察因子')" :key="index">
            <strong>{{ item['因子'] }}</strong>
            <el-tag v-if="item['方向']" size="small" type="info" effect="plain">{{
              item['方向']
            }}</el-tag>
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
    <el-empty
      v-else
      description="暂无商业画像；生成分析时自动合成（需先有财报摘要或业务章节）"
      :image-size="56"
    />

    <div v-if="peers.length" class="peer-list" data-testid="peer-list">
      <span class="sd-muted">同业（{{ peerIndustry || '同行业' }}）：</span>
      <template v-for="peer in peers" :key="peer.symbol || peer.name">
        <el-link v-if="peer.symbol" type="primary" class="peer-item" @click="goToPeer(peer)">
          {{ peer.name || peer.symbol }}
        </el-link>
        <span v-else class="peer-item sd-muted">{{ peer.name }}</span>
      </template>
    </div>
  </section>
</template>

<style scoped>
.peer-list {
  margin-top: 12px;
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.segment-table {
  max-width: 640px;
}

.peer-item {
  font-size: 13px;
}

.sd-inline-list .el-tag {
  margin: 0 4px;
}

/* 准则依据下的口径说明（TTM 构成/价格日期）与参考值：小一号灰字，与判定正文区分 */
.graham-basis {
  margin-top: 2px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--app-text-soft);
  font-variant-numeric: tabular-nums;
}
</style>
