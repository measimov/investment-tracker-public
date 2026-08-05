<template>
  <div class="security-detail-page">
    <el-card>
      <template #header>
        <div class="page-header">
          <div class="title-with-tag">
            <el-button text :icon="ArrowLeft" @click="router.back()">返回</el-button>
            <span class="security-title">
              {{ symbol }}
              <span v-if="analysis?.name" class="security-name">{{ analysis.name }}</span>
            </span>
            <el-tag size="small" effect="plain">{{ market }}</el-tag>
            <el-tag
              v-if="analysis"
              :type="riskTagType(analysis.risk_level)"
              size="small"
              data-testid="risk-level-tag"
            >
              风险 {{ riskLabel(analysis.risk_level) }}
            </el-tag>
          </div>
          <div class="header-actions">
            <el-button
              type="primary"
              :loading="generating"
              :disabled="!supported"
              data-testid="generate-analysis-button"
              @click="generateAnalysis"
            >
              {{ analysis ? '重新生成分析' : '生成 AI 分析' }}
            </el-button>
          </div>
        </div>
      </template>

      <el-alert
        v-if="!supported"
        title="该市场暂不支持基本面数据与 AI 分析（支持：A股/美股/港股）；其他市场信息以券商对账单与行情为准。"
        type="info"
        :closable="false"
        show-icon
      />

      <template v-else>
        <!-- 生成进度（必须在 v-if="analysis" 之外：首次生成时还没有分析正文） -->
        <div v-if="analysisJob" class="job-progress" data-testid="analysis-progress">
          <div class="job-progress-header">
            <span>{{ analysisStatusText }}</span>
            <span v-if="analysisJob.total">
              {{ analysisJob.completed || 0 }}/{{ analysisJob.total }}
            </span>
          </div>
          <el-progress
            :percentage="analysisPercent"
            :status="analysisProgressStatus"
            :stroke-width="10"
          />
          <div v-if="analysisJob.error" class="job-progress-error">
            {{ analysisJob.error }}
          </div>
        </div>

        <!-- AI 分析区 -->
        <section v-if="analysis" class="analysis-section">
          <div class="analysis-meta">
            <el-tag
              v-for="tag in analysis.tags"
              :key="tag"
              type="warning"
              effect="plain"
              class="analysis-tag"
            >
              {{ tag }}
            </el-tag>
            <span class="analysis-summary">{{ analysis.summary }}</span>
          </div>
          <div class="markdown-body" v-html="renderMarkdown(analysis.content)" />
          <div class="analysis-footnote">
            {{ analysis.model }} · {{ analysis.total_tokens || '—' }} tokens · 生成于
            {{ formatDateTime(analysis.created_at) }}
            <template v-if="analysis.data_fetched_at">
              · 数据获取于 {{ analysis.data_fetched_at }}
            </template>
          </div>
        </section>
        <el-empty
          v-else
          description="暂无 AI 分析；点击右上角生成（将同步基本面数据并调用 LLM）"
          :image-size="88"
        />

        <el-divider />

        <!-- 商业画像 -->
        <section class="data-section" data-testid="business-profile-section">
          <h3>商业画像</h3>
          <template v-if="businessProfile">
            <el-descriptions :column="1" border size="small">
              <el-descriptions-item label="商业模式">
                {{ businessProfile['商业模式'] || '—' }}
              </el-descriptions-item>
              <el-descriptions-item label="行业与竞争">
                {{ businessProfile['行业与竞争'] || '—' }}
              </el-descriptions-item>
              <el-descriptions-item label="供应商集中度">
                {{ businessProfile['供应商集中度'] || '—' }}
              </el-descriptions-item>
              <el-descriptions-item label="客户集中度">
                {{ businessProfile['客户集中度'] || '—' }}
              </el-descriptions-item>
            </el-descriptions>

            <div class="profile-grid">
              <div>
                <h4>业务分部</h4>
                <el-table :data="businessProfile['业务分部'] || []" size="small" stripe>
                  <el-table-column label="名称" prop="名称" />
                  <el-table-column label="收入占比" prop="收入占比" width="90" />
                  <el-table-column label="毛利率" prop="毛利率" width="90" />
                  <el-table-column label="趋势" prop="趋势" width="70" />
                </el-table>
              </div>
              <div>
                <h4>估值观察因子</h4>
                <el-table :data="businessProfile['估值观察因子'] || []" size="small" stripe>
                  <el-table-column label="因子" prop="因子" />
                  <el-table-column label="方向" prop="方向" width="90" />
                  <el-table-column label="传导" prop="传导" />
                </el-table>
              </div>
            </div>

            <div class="profile-grid">
              <div>
                <h4>上游依赖</h4>
                <el-table :data="businessProfile['上游依赖'] || []" size="small" stripe>
                  <el-table-column label="要素" prop="要素" width="140" />
                  <el-table-column label="影响" prop="影响" />
                </el-table>
              </div>
              <div>
                <h4>下游需求</h4>
                <el-table :data="businessProfile['下游需求'] || []" size="small" stripe>
                  <el-table-column label="客群或场景" width="140">
                    <template #default="{ row }">
                      {{ row['客群或场景'] || row['客群/场景'] || '—' }}
                    </template>
                  </el-table-column>
                  <el-table-column label="需求驱动" prop="需求驱动" />
                </el-table>
              </div>
            </div>
          </template>
          <el-empty
            v-else
            description="暂无商业画像；生成分析时自动合成（需先有财报摘要或业务章节）"
            :image-size="60"
          />

          <div v-if="peers.length" class="peer-list" data-testid="peer-list">
            <span class="peer-label">同业（{{ peerIndustry || '同行业' }}）：</span>
            <template v-for="peer in peers" :key="peer.symbol || peer.name">
              <el-link v-if="peer.symbol" type="primary" class="peer-item" @click="goToPeer(peer)">
                {{ peer.name || peer.symbol }}
              </el-link>
              <span v-else class="peer-item peer-plain">{{ peer.name }}</span>
            </template>
          </div>
        </section>

        <!-- 财报摘要 -->
        <section
          v-if="capabilities.report_digest"
          class="data-section"
          data-testid="report-digest-section"
        >
          <div class="section-header">
            <h3>
              财报摘要（已摘要 {{ digestProgress.digested }} 份<template
                v-if="digestProgress.failed_capped"
                >，{{ digestProgress.failed_capped }} 份获取失败</template
              >）
            </h3>
            <el-button
              size="small"
              :loading="backfilling"
              data-testid="backfill-digests-button"
              @click="backfillDigests"
            >
              补齐历史摘要
            </el-button>
          </div>
          <el-alert
            v-if="backfillSummary"
            :title="backfillSummary"
            type="info"
            :closable="false"
            class="backfill-summary"
          />
          <el-collapse v-if="reportDigests.length">
            <el-collapse-item
              v-for="digest in reportDigests"
              :key="digest.period_key"
              :name="digest.period_key"
            >
              <template #title>
                {{ formatPeriod(digest.end_date) }}
                <el-tag size="small" effect="plain" class="digest-type-tag">
                  {{ digestTypeLabel(digest.report_type) }}
                </el-tag>
              </template>
              <div class="digest-body">
                <template v-for="field in DIGEST_FIELD_ORDER" :key="field">
                  <p v-if="digest.digest?.[field]">
                    <strong>{{ field }}：</strong>{{ digest.digest[field] }}
                  </p>
                </template>
                <div v-if="digest.digest?.['关键数字']?.length" class="digest-numbers">
                  <strong>关键数字：</strong>
                  <ul>
                    <li v-for="(num, index) in digest.digest['关键数字']" :key="index">
                      {{ num }}
                    </li>
                  </ul>
                </div>
                <el-link
                  v-if="typeof digest.source_url === 'string'"
                  :href="digest.source_url"
                  target="_blank"
                  type="info"
                >
                  查看原文
                </el-link>
              </div>
            </el-collapse-item>
          </el-collapse>
          <el-empty
            v-else
            description="暂无财报摘要；点击「补齐历史摘要」抓取年报并生成（每次最多 4 份，可重复点击续跑）"
            :image-size="60"
          />
        </section>

        <!-- 利润质量指标 -->
        <section class="data-section" data-testid="earnings-quality-section">
          <h3>利润质量指标（红色 = 触及红旗阈值）</h3>
          <el-table :data="qualityRows" size="small" stripe>
            <template #empty>
              <el-empty description="暂无数据；生成分析时会自动同步" :image-size="60" />
            </template>
            <el-table-column label="年度" prop="year" width="70" />
            <el-table-column label="CFO/净利润" align="right">
              <template #default="{ row }">
                <span :class="{ 'red-flag': isNum(row.cfo_ni_ratio) && row.cfo_ni_ratio < 0.8 }">
                  {{ formatMaybe(row.cfo_ni_ratio) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="应计率" align="right">
              <template #default="{ row }">
                <span
                  :class="{ 'red-flag': isNum(row.accruals_ratio) && row.accruals_ratio > 0.1 }"
                >
                  {{ formatMaybe(row.accruals_ratio) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="应收-营收增速差pp" align="right">
              <template #default="{ row }">
                <span
                  :class="{
                    'red-flag':
                      isNum(row.receivable_vs_revenue_gap_pp) &&
                      row.receivable_vs_revenue_gap_pp > 20
                  }"
                >
                  {{ formatMaybe(row.receivable_vs_revenue_gap_pp) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="存货-营收增速差pp" align="right">
              <template #default="{ row }">
                <span
                  :class="{
                    'red-flag':
                      isNum(row.inventory_vs_revenue_gap_pp) && row.inventory_vs_revenue_gap_pp > 20
                  }"
                >
                  {{ formatMaybe(row.inventory_vs_revenue_gap_pp) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="毛利率%" align="right">
              <template #default="{ row }">{{ formatMaybe(row.gross_margin) }}</template>
            </el-table-column>
            <el-table-column label="扣非占比" align="right">
              <template #default="{ row }">
                <span
                  :class="{
                    'red-flag':
                      isNum(row.recurring_profit_share) && row.recurring_profit_share < 0.7
                  }"
                >
                  {{ formatMaybe(row.recurring_profit_share) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="M-score" align="right">
              <template #default="{ row }">
                <span :class="{ 'red-flag': row.m_flag }">{{ formatMaybe(row.m_score) }}</span>
              </template>
            </el-table-column>
          </el-table>
          <div v-if="isNum(cfoNi5y)" class="quality-footnote">
            近 5 年累计 CFO/净利润：
            <span :class="{ 'red-flag': cfoNi5y! < 0.8 }">{{ formatMaybe(cfoNi5y) }}</span>
            （指标口径与红旗阈值见 AI 分析「利润质量与会计风险」章节）
          </div>
        </section>

        <!-- 未来事件（A股：分红公告同步时抓取） -->
        <section v-if="market === 'A股'" class="data-section">
          <h3>标的事件</h3>
          <el-table :data="events" size="small" stripe>
            <template #empty>
              <el-empty
                description="暂无事件；公司行动页同步分红公告时会一并抓取"
                :image-size="60"
              />
            </template>
            <el-table-column label="日期" width="110" prop="event_date" />
            <el-table-column label="类型" width="120">
              <template #default="{ row }">{{ eventTypeLabel(row.event_type) }}</template>
            </el-table-column>
            <el-table-column label="详情">
              <template #default="{ row }">{{ eventDetail(row) }}</template>
            </el-table-column>
          </el-table>
        </section>

        <!-- 年度报表（美股/港股：EDGAR/Yahoo 透视行） -->
        <section
          v-if="market !== 'A股'"
          class="data-section"
          data-testid="pivot-statements-section"
        >
          <h3>
            年度核心科目（{{ market === '美股' ? 'SEC XBRL' : '雅虎财经' }}，单位
            {{ pivotCurrency || '原币' }} 亿<template v-if="market === '港股'"
              >；非官方接口，<strong>仅近 3-5 年</strong>——更长周期请看下方财报摘要</template
            >）
          </h3>
          <el-table :data="pivotRows" size="small" stripe>
            <template #empty>
              <el-empty description="暂无数据；生成分析时会自动同步" :image-size="60" />
            </template>
            <el-table-column label="财年止" width="110">
              <template #default="{ row }">{{ formatPeriod(row.end_date) }}</template>
            </el-table-column>
            <el-table-column label="营业收入" align="right">
              <template #default="{ row }">{{ formatYi(row.total_revenue) }}</template>
            </el-table-column>
            <el-table-column label="净利润" align="right">
              <template #default="{ row }">{{ formatYi(row.n_income_attr_p) }}</template>
            </el-table-column>
            <el-table-column label="经营现金流" align="right">
              <template #default="{ row }">{{ formatYi(row.n_cashflow_act) }}</template>
            </el-table-column>
            <el-table-column label="总资产" align="right">
              <template #default="{ row }">{{ formatYi(row.total_assets) }}</template>
            </el-table-column>
            <el-table-column label="股东权益" align="right">
              <template #default="{ row }">{{ formatYi(row.total_hldr_eqy_exc_min_int) }}</template>
            </el-table-column>
            <el-table-column label="EPS" align="right">
              <template #default="{ row }">{{ formatMaybe(row.basic_eps) }}</template>
            </el-table-column>
          </el-table>
        </section>

        <!-- 财务指标（A股 Tushare） -->
        <section v-if="market === 'A股'" class="data-section">
          <h3>
            财务指标（近 {{ finaRows.length }} 期<template v-if="latestPeriods.fina_indicator"
              >，最新报告期 {{ formatPeriod(latestPeriods.fina_indicator) }}</template
            >）
          </h3>
          <el-table :data="finaRows" size="small" stripe>
            <template #empty>
              <el-empty description="暂无数据；生成分析时会自动同步" :image-size="60" />
            </template>
            <el-table-column label="报告期" width="110">
              <template #default="{ row }">{{ formatPeriod(row.end_date) }}</template>
            </el-table-column>
            <el-table-column label="EPS" align="right">
              <template #default="{ row }">{{ formatMaybe(row.eps) }}</template>
            </el-table-column>
            <el-table-column label="ROE%" align="right">
              <template #default="{ row }">{{ formatMaybe(row.roe) }}</template>
            </el-table-column>
            <el-table-column label="毛利率%" align="right">
              <template #default="{ row }">{{ formatMaybe(row.grossprofit_margin) }}</template>
            </el-table-column>
            <el-table-column label="净利率%" align="right">
              <template #default="{ row }">{{ formatMaybe(row.netprofit_margin) }}</template>
            </el-table-column>
            <el-table-column label="资产负债率%" align="right">
              <template #default="{ row }">{{ formatMaybe(row.debt_to_assets) }}</template>
            </el-table-column>
            <el-table-column label="营收同比%" align="right">
              <template #default="{ row }">{{ formatMaybe(row.or_yoy ?? row.tr_yoy) }}</template>
            </el-table-column>
            <el-table-column label="净利同比%" align="right">
              <template #default="{ row }">{{ formatMaybe(row.netprofit_yoy) }}</template>
            </el-table-column>
          </el-table>
        </section>

        <!-- 三大报表摘要（A股） -->
        <section v-if="market === 'A股'" class="data-section">
          <h3>
            利润与现金流摘要（合并报表，亿元<template v-if="latestPeriods.income"
              >，最新报告期 {{ formatPeriod(latestPeriods.income) }}</template
            >）
          </h3>
          <el-table :data="statementRows" size="small" stripe>
            <template #empty>
              <el-empty description="暂无数据；生成分析时会自动同步" :image-size="60" />
            </template>
            <el-table-column label="报告期" width="110">
              <template #default="{ row }">{{ formatPeriod(row.end_date) }}</template>
            </el-table-column>
            <el-table-column label="营业总收入" align="right">
              <template #default="{ row }">{{ formatYi(row.total_revenue) }}</template>
            </el-table-column>
            <el-table-column label="营业利润" align="right">
              <template #default="{ row }">{{ formatYi(row.operate_profit) }}</template>
            </el-table-column>
            <el-table-column label="归母净利润" align="right">
              <template #default="{ row }">{{
                formatYi(row.n_income_attr_p ?? row.n_income)
              }}</template>
            </el-table-column>
            <el-table-column label="经营现金流净额" align="right">
              <template #default="{ row }">{{ formatYi(row.n_cashflow_act) }}</template>
            </el-table-column>
            <el-table-column label="投资现金流净额" align="right">
              <template #default="{ row }">{{ formatYi(row.n_cashflow_inv_act) }}</template>
            </el-table-column>
            <el-table-column label="总资产" align="right">
              <template #default="{ row }">{{ formatYi(row.total_assets) }}</template>
            </el-table-column>
            <el-table-column label="净资产" align="right">
              <template #default="{ row }">{{ formatYi(row.total_hldr_eqy_exc_min_int) }}</template>
            </el-table-column>
          </el-table>
        </section>

        <!-- 分红历史（A股） -->
        <section v-if="market === 'A股'" class="data-section">
          <h3>
            分红历史（已实施<template v-if="latestPeriods.dividend_history"
              >，最新公告
              {{ formatPeriod(latestPeriods.dividend_history.split('|').pop()) }}</template
            >）
          </h3>
          <el-table :data="dividendRows" size="small" stripe>
            <template #empty>
              <el-empty description="暂无数据" :image-size="60" />
            </template>
            <el-table-column label="报告期" width="110">
              <template #default="{ row }">{{ formatPeriod(row.end_date) }}</template>
            </el-table-column>
            <el-table-column label="每股税前" align="right">
              <template #default="{ row }">{{ formatMaybe(row.cash_div_tax) }}</template>
            </el-table-column>
            <el-table-column label="每股送转" align="right">
              <template #default="{ row }">{{ formatMaybe(row.stk_div) }}</template>
            </el-table-column>
            <el-table-column label="除权日" width="110">
              <template #default="{ row }">{{ formatPeriod(row.ex_date) }}</template>
            </el-table-column>
          </el-table>
        </section>

        <!-- 风险信号（A股数据源；美/港股见 AI 分析中的说明） -->
        <section v-if="capabilities.risk_signals === true" class="data-section">
          <h3>风险信号</h3>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="审计意见（最近）">
              {{ latestAudit || '数据不足' }}
            </el-descriptions-item>
            <el-descriptions-item label="股权质押（最近）">
              {{ latestPledge || '数据不足' }}
            </el-descriptions-item>
            <el-descriptions-item label="股东增减持（近 5 条）">
              <div v-if="holderTrades.length">
                <div v-for="(trade, index) in holderTrades" :key="index">{{ trade }}</div>
              </div>
              <template v-else>数据不足</template>
            </el-descriptions-item>
          </el-descriptions>
        </section>
      </template>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ArrowLeft } from '@element-plus/icons-vue'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import api from '../api'
import { getApiErrorMessage } from '../utils/apiErrors'
import { renderMarkdown } from '@/utils/markdown'
import { formatDateTime, formatNumber, toNumber } from '../utils/helpers'
import { pollJobUntilDone } from '../utils/polling'

interface AnalysisDetail {
  id: number
  name?: string | null
  tags: string[]
  risk_level: string
  summary: string
  content: string
  model?: string
  total_tokens?: number | null
  created_at?: string | null
  data_fetched_at?: string | null
}

interface AnalysisJob {
  id?: string
  status?: string
  stage?: string | null
  stage_label?: string | null
  completed?: number
  total?: number
  progress_percent?: number | string | null
  error?: string | null
  [key: string]: unknown
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type ProfileRow = Record<string, any>

const route = useRoute()
const router = useRouter()
const market = computed(() => String(route.params.market || ''))
const symbol = computed(() => String(route.params.symbol || ''))

const analysis = ref<AnalysisDetail | null>(null)
const profileDatasets = ref<Record<string, ProfileRow[]>>({})
const latestPeriods = ref<Record<string, string | null>>({})
const events = ref<ProfileRow[]>([])
const supported = ref(true)
const generating = ref(false)
const analysisJob = ref<AnalysisJob | null>(null)
const capabilities = ref<ProfileRow>({})
const business = ref<ProfileRow>({ profile: null, peers: [], industry: null })
const reportDigests = ref<ProfileRow[]>([])
const digestProgress = ref<{ digested: number; failed_capped: number }>({
  digested: 0,
  failed_capped: 0
})
const earningsQuality = ref<ProfileRow>({})
const backfilling = ref(false)
const backfillResult = ref<ProfileRow | null>(null)
let isUnmounted = false

// 与后端 report_digest_prompts.DIGEST_FIELDS 同序（关键数字单列）
const DIGEST_FIELD_ORDER = [
  '经营回顾',
  '业务分部占比',
  '上下游与产业链',
  '主营收入结构',
  '成本与费用',
  '一次性项目',
  '会计信号',
  '风险要点',
  '展望'
]

const businessProfile = computed(() => business.value.profile || null)
const peers = computed<ProfileRow[]>(() => business.value.peers || [])
const peerIndustry = computed(() => business.value.industry || '')

// 美股/港股年度透视行（美股行含季度，表格只展示 FY）
const pivotRows = computed<ProfileRow[]>(() => {
  if (market.value === '美股') {
    return (profileDatasets.value.edgar_companyfacts || []).filter((row) => row.fp === 'FY')
  }
  if (market.value === '港股') return profileDatasets.value.yahoo_fundamentals || []
  return []
})
const pivotCurrency = computed(() => pivotRows.value[0]?.currency || '')

const qualityRows = computed(() => {
  const perYear = earningsQuality.value.per_year || {}
  const mScores = earningsQuality.value.beneish_m_score || {}
  return Object.keys(perYear)
    .sort()
    .reverse()
    .map((year) => ({
      year,
      ...perYear[year],
      m_score: mScores[year]?.score ?? null,
      m_flag: Boolean(mScores[year]?.flag)
    }))
})
const cfoNi5y = computed<number | null>(() => {
  const value = earningsQuality.value.cfo_ni_ratio_5y
  return typeof value === 'number' ? value : null
})

const analysisPercent = computed(() =>
  Math.max(0, Math.min(100, Math.round(Number(analysisJob.value?.progress_percent || 0))))
)

const analysisProgressStatus = computed(() => {
  const status = analysisJob.value?.status
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
  const job = analysisJob.value
  if (!job) return ''
  // 运行中优先显示后端回写的阶段名（同步基本面档案 / 生成分析（LLM）…）
  if (job.status === 'running' && job.stage_label) return job.stage_label
  return ANALYSIS_STATUS_LABELS[String(job.status || '')] || 'AI 分析生成中'
})

const backfillSummary = computed(() => {
  const result = backfillResult.value
  if (!result) return ''
  const parts = [
    `本次生成 ${result.generated ?? 0} 份，累计 ${result.completed ?? 0}/${result.total ?? 0} 份`
  ]
  if (result.remaining) parts.push(`剩余 ${result.remaining} 份可继续点击补齐`)
  if (result.gaps?.length) parts.push(`缺口：${result.gaps.join('；')}`)
  return parts.join('；')
})

function isNum(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

const DIGEST_TYPE_LABELS: Record<string, string> = {
  annual: '年报',
  semi: '半年报',
  '10-K': '10-K'
}

function digestTypeLabel(type: unknown): string {
  return DIGEST_TYPE_LABELS[String(type || '')] || String(type || '—')
}

function goToPeer(peer: ProfileRow) {
  if (!peer.symbol) return
  router.push({
    name: 'SecurityDetail',
    params: { market: market.value, symbol: String(peer.symbol) }
  })
}

const finaRows = computed(() => profileDatasets.value.fina_indicator || [])
// 三大报表按报告期合并为一行（利润表/现金流/资产负债各取核心科目）
const statementRows = computed(() => {
  const merged = new Map<string, ProfileRow>()
  for (const dataset of ['income', 'cashflow', 'balancesheet']) {
    for (const row of profileDatasets.value[dataset] || []) {
      const key = String(row.end_date || '')
      if (!key) continue
      merged.set(key, { ...(merged.get(key) || {}), ...row })
    }
  }
  return [...merged.entries()]
    .sort((a, b) => (a[0] < b[0] ? 1 : -1))
    .map(([, row]) => row)
    .slice(0, 8)
})

function formatYi(value: unknown): string {
  if (value == null || value === '') return '—'
  if (typeof value !== 'number' && typeof value !== 'string') return String(value)
  return formatNumber(toNumber(value) / 1e8, 2)
}

const dividendRows = computed(() =>
  (profileDatasets.value.dividend_history || []).filter((row) => row.div_proc === '实施')
)

const latestAudit = computed(() => {
  const row = (profileDatasets.value.fina_audit || [])[0]
  if (!row) return ''
  return `${formatPeriod(row.end_date)}：${row.audit_result || '—'}（${row.audit_agency || '—'}）`
})

const latestPledge = computed(() => {
  const row = (profileDatasets.value.pledge_stat || [])[0]
  if (!row) return ''
  return `${formatPeriod(row.end_date)}：质押比例 ${formatMaybe(row.pledge_ratio)}%（${row.pledge_count ?? '—'} 笔）`
})

const holderTrades = computed(() =>
  (profileDatasets.value.stk_holdertrade || []).slice(0, 5).map((row) => {
    const direction = row.in_de === 'IN' ? '增持' : '减持'
    return `${formatPeriod(row.ann_date)} ${row.holder_name || '—'} ${direction} ${formatMaybe(row.change_vol)} 股（占比 ${formatMaybe(row.change_ratio)}%）`
  })
)

function riskTagType(level: string) {
  if (level === 'high') return 'danger'
  if (level === 'medium') return 'warning'
  return 'success'
}

const RISK_LABELS: Record<string, string> = { low: '低', medium: '中', high: '高' }

function riskLabel(level: string) {
  return RISK_LABELS[level] || level
}

const EVENT_TYPE_LABELS: Record<string, string> = {
  EARNINGS_DISCLOSURE: '财报披露',
  DIVIDEND_PLAN: '分红预案',
  SHARE_UNLOCK: '限售解禁'
}

function eventTypeLabel(type: string) {
  return EVENT_TYPE_LABELS[type] || type
}

function eventDetail(row: ProfileRow): string {
  const payload = row.payload || {}
  if (row.event_type === 'DIVIDEND_PLAN') {
    return `每股税前 ${formatMaybe(payload.cash_div_tax)}（${payload.div_proc || '预案'}）`
  }
  if (row.event_type === 'SHARE_UNLOCK') {
    return `解禁 ${formatMaybe(payload.float_share)} 万股（占比 ${formatMaybe(payload.float_ratio_pct)}%）`
  }
  if (row.event_type === 'EARNINGS_DISCLOSURE') {
    return `报告期 ${formatPeriod(payload.period)}`
  }
  return ''
}

function formatPeriod(value: unknown): string {
  const text = String(value || '')
  if (/^\d{8}$/.test(text)) return `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)}`
  return text || '—'
}

function formatMaybe(value: unknown): string {
  if (value == null || value === '') return '—'
  if (typeof value !== 'number' && typeof value !== 'string') return String(value)
  return formatNumber(toNumber(value), 4)
}

/**
 * 请求身份守卫：同业跳转复用同一组件实例，A 的请求慢于 B 时，A 的响应会
 * 在 B 的标题下写入 B 的数据区。每次发起时领取一个**单调递增的代次**，
 * 返回时不是最新代次就整条丢弃（成功与失败都丢——过期的错误提示同样是
 * 误导）。
 *
 * 用代次而不是 `market/symbol`：后者无法区分同一标的的两代请求。A₁（慢）
 * → 切到 B → 再切回 A 发起 A₂（快）时，A₂ 先渲染、A₁ 后到，业务 key 又
 * 相等，旧的 A₁ 仍会覆盖更新的 A₂（ABA）。
 */
let requestGeneration = 0

function nextGeneration(): number {
  requestGeneration += 1
  return requestGeneration
}

function isStale(generation: number): boolean {
  return isUnmounted || generation !== requestGeneration
}

async function loadAnalysis(generation: number) {
  try {
    const response = await api.getSecurityAnalysis(market.value, symbol.value)
    if (isStale(generation)) return
    analysis.value = response.data
  } catch {
    if (isStale(generation)) return
    analysis.value = null // 404 = 尚未生成，空态引导
  }
}

async function loadProfile(generation: number) {
  try {
    const response = await api.getSecurityProfile(market.value, symbol.value)
    if (isStale(generation)) return
    profileDatasets.value = response.data.datasets || {}
    latestPeriods.value = response.data.latest_periods || {}
    events.value = response.data.events || []
    supported.value = response.data.supported !== false
    capabilities.value = response.data.capabilities || {}
    business.value = response.data.business || { profile: null, peers: [], industry: null }
    reportDigests.value = response.data.report_digests || []
    digestProgress.value = response.data.digest_progress || { digested: 0, failed_capped: 0 }
    earningsQuality.value = response.data.earnings_quality || {}
  } catch (error) {
    if (isStale(generation)) return
    ElMessage.error(getApiErrorMessage(error, '加载标的档案失败'))
  }
}

/** 一次导航 = 一个代次，analysis 与 profile 共用，互不作废 */
function reloadAll() {
  const generation = nextGeneration()
  loadAnalysis(generation)
  loadProfile(generation)
}

async function backfillDigests() {
  // 用户操作沿用当前代次（不新开）：后续导航会作废它，包括切走再切回
  const generation = requestGeneration
  backfilling.value = true
  try {
    const startResponse = await api.startReportBackfillJob(market.value, symbol.value)
    const job = await pollJobUntilDone(() => api.getReportBackfillJob(startResponse.data.id), {
      intervalMs: 3000,
      maxAttempts: 200,
      isCancelled: () => isStale(generation),
      failureMessage: '财报摘要回填失败'
    })
    if (!job || isStale(generation)) return
    backfillResult.value = (job.result as ProfileRow) || null
    ElMessage.success('财报摘要回填完成')
    await loadProfile(generation)
  } catch (error) {
    if (isStale(generation)) return
    ElMessage.error(getApiErrorMessage(error, '财报摘要回填失败'))
  } finally {
    if (!isStale(generation)) backfilling.value = false
  }
}

async function generateAnalysis() {
  const generation = requestGeneration
  generating.value = true
  analysisJob.value = null
  try {
    const startResponse = await api.startSecurityAnalysisJob(market.value, symbol.value)
    if (isStale(generation)) return
    analysisJob.value = startResponse.data as AnalysisJob // 立刻显示「排队中」
    const job = await pollJobUntilDone(() => api.getSecurityAnalysisJob(startResponse.data.id), {
      intervalMs: 3000,
      maxAttempts: 400, // 3s × 400 ≈ 20 分钟：冷启动含财报摘要时 6 分钟不够
      isCancelled: () => isStale(generation),
      onUpdate: (job) => {
        // onUpdate 是每轮无条件调用的（取消检查在 fetch 之前），必须自己判过期，
        // 否则同业跳转后旧标的的进度会画进新标的的页面
        if (isStale(generation)) return
        analysisJob.value = job as AnalysisJob
      },
      timeoutMessage: '分析仍在后台运行，请稍后刷新本页查看',
      failureMessage: '标的分析生成失败'
    })
    if (!job || isStale(generation)) return
    analysisJob.value = null // 成功即收起：正文本身就是完成证据
    ElMessage.success('分析已生成')
    await Promise.all([loadAnalysis(generation), loadProfile(generation)])
  } catch (error) {
    if (isStale(generation)) return
    // 失败保留进度块：卡在哪个阶段 + 错误原文是唯一有用的残留（消息会消失）
    const message = getApiErrorMessage(error, '标的分析生成失败')
    analysisJob.value = { ...(analysisJob.value || {}), status: 'failed', error: message }
    ElMessage.error(message)
  } finally {
    if (!isStale(generation)) generating.value = false
  }
}

onMounted(() => {
  isUnmounted = false
  reloadAll()
})

// 同业跳转复用同一路由组件：参数变化时清空并重载（旧标的的在途请求由
// 代次守卫拦下，不会写进新标的的页面）
watch(
  () => [route.params.market, route.params.symbol],
  () => {
    if (route.name !== 'SecurityDetail') return
    analysis.value = null
    backfillResult.value = null
    profileDatasets.value = {}
    latestPeriods.value = {}
    events.value = []
    capabilities.value = {}
    business.value = { profile: null, peers: [], industry: null }
    reportDigests.value = []
    digestProgress.value = { digested: 0, failed_capped: 0 }
    earningsQuality.value = {}
    generating.value = false
    backfilling.value = false
    analysisJob.value = null
    reloadAll()
  }
)

onUnmounted(() => {
  isUnmounted = true
})
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

.title-with-tag {
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

.analysis-section {
  margin-bottom: 8px;
}

.analysis-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.analysis-tag {
  flex-shrink: 0;
}

.analysis-summary {
  color: var(--app-text-soft);
  font-size: 13px;
}

.analysis-footnote {
  margin-top: 12px;
  font-size: 12px;
  color: var(--app-text-soft);
}

.data-section {
  margin-top: 18px;
}

.data-section h3 {
  margin: 0 0 10px;
  font-size: 14px;
}

.data-section h4 {
  margin: 12px 0 8px;
  font-size: 13px;
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}

.profile-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 0 16px;
}

.peer-list {
  margin-top: 12px;
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.peer-label {
  color: var(--app-text-soft);
}

.peer-item {
  font-size: 13px;
}

.peer-plain {
  color: var(--app-text-soft);
}

.digest-type-tag {
  margin-left: 8px;
}

.digest-body p {
  margin: 4px 0;
  font-size: 13px;
  line-height: 1.7;
}

.digest-numbers ul {
  margin: 4px 0;
  padding-left: 20px;
  font-size: 13px;
}

.backfill-summary {
  margin-bottom: 10px;
}

.red-flag {
  color: var(--el-color-danger);
  font-weight: 600;
}

.quality-footnote {
  margin-top: 8px;
  font-size: 12px;
  color: var(--app-text-soft);
}
</style>
