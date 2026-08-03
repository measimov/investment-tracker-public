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
        title="该市场暂不支持基本面数据与 AI 分析（仅 A 股）；港/美股信息以券商对账单与行情为准。"
        type="info"
        :closable="false"
        show-icon
      />

      <template v-else>
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

        <!-- 未来事件 -->
        <section class="data-section">
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

        <!-- 财务指标 -->
        <section class="data-section">
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

        <!-- 分红历史 -->
        <section class="data-section">
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

        <!-- 风险信号 -->
        <section class="data-section">
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
import { computed, onMounted, onUnmounted, ref } from 'vue'
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
let isUnmounted = false

const finaRows = computed(() => profileDatasets.value.fina_indicator || [])
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

async function loadAnalysis() {
  try {
    const response = await api.getSecurityAnalysis(market.value, symbol.value)
    analysis.value = response.data
  } catch {
    analysis.value = null // 404 = 尚未生成，空态引导
  }
}

async function loadProfile() {
  try {
    const response = await api.getSecurityProfile(market.value, symbol.value)
    profileDatasets.value = response.data.datasets || {}
    latestPeriods.value = response.data.latest_periods || {}
    events.value = response.data.events || []
    supported.value = response.data.supported !== false
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '加载标的档案失败'))
  }
}

async function generateAnalysis() {
  generating.value = true
  try {
    const startResponse = await api.startSecurityAnalysisJob(market.value, symbol.value)
    const job = await pollJobUntilDone(() => api.getSecurityAnalysisJob(startResponse.data.id), {
      intervalMs: 3000,
      maxAttempts: 120,
      isCancelled: () => isUnmounted,
      failureMessage: '标的分析生成失败'
    })
    if (!job) return
    ElMessage.success('分析已生成')
    await Promise.all([loadAnalysis(), loadProfile()])
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '标的分析生成失败'))
  } finally {
    generating.value = false
  }
}

onMounted(() => {
  isUnmounted = false
  loadAnalysis()
  loadProfile()
})

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
</style>
