<script setup lang="ts">
/**
 * 「基本面」tab：估值快照与业绩预告（A股）、财务指标（A股）、利润质量指标、
 * 分红历史（A股）、风险信号、标的事件（A股）。
 */
import { computed, ref } from 'vue'
import { InfoFilled } from '@element-plus/icons-vue'
import { EMPTY } from '@/utils/helpers'
import { useMediaQuery } from '@/composables/useMediaQuery'
import {
  annualOnly,
  formatCount,
  formatEps,
  formatFixed,
  formatPct,
  formatPerShare,
  formatPeriod,
  formatPp,
  formatRatio,
  formatWanShares,
  latestImplementedDividendAnnDate,
  periodKindLabel
} from './format'
import type { ProfileRow, SecurityProfileState } from './types'

const props = defineProps<{ state: SecurityProfileState; market: string }>()

const isA = computed(() => props.market === 'A股')
const isMobile = useMediaQuery('(max-width: 640px)')
const datasets = computed(() => props.state.datasets || {})

function isNum(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

// ---- 估值快照（daily_basic 最新一行）与业绩预告（forecast 最新一行）----
const valuation = computed<ProfileRow | null>(() => (datasets.value.daily_basic || [])[0] || null)
const forecast = computed<ProfileRow | null>(() => (datasets.value.forecast || [])[0] || null)

function signedPct(value: unknown): string {
  if (!isNum(value)) return EMPTY
  return `${value > 0 ? '+' : ''}${formatFixed(value, 1)}%`
}

const forecastText = computed(() => {
  const row = forecast.value
  if (!row) return ''
  const range =
    isNum(row.p_change_min) || isNum(row.p_change_max)
      ? `净利润同比 ${signedPct(row.p_change_min)} ~ ${signedPct(row.p_change_max)}`
      : ''
  return [row.type, range].filter(Boolean).join('，')
})

// ---- 财务指标（A股 Tushare fina_indicator）----
const finaAnnualOnly = ref(false)
const finaRows = computed(() => {
  const rows = datasets.value.fina_indicator || []
  return finaAnnualOnly.value ? annualOnly(rows) : rows
})

// ---- 利润质量指标 ----
const qualityRows = computed(() => {
  const perYear = props.state.earningsQuality.per_year || {}
  const mScores = props.state.earningsQuality.beneish_m_score || {}
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
  const value = props.state.earningsQuality.cfo_ni_ratio_5y
  return isNum(value) ? value : null
})
// 报告币种中途切换（后端只在切换时给出）：切换年的跨年指标不计，近 5 年累计只含同币种年份
const qualityCurrencyNotes = computed<string[]>(() => {
  const quality = props.state.earningsQuality
  const changes = (quality.currency_changes || []) as Array<{ year: string; change: string }>
  const notes = changes.map(
    (item) => `${item.year} 年起报告币种 ${item.change}：该年相对上一年的增速差与 M-score 不计`
  )
  if (quality.cfo_ni_ratio_5y_note) notes.push(String(quality.cfo_ni_ratio_5y_note))
  return notes
})

// ---- 分红历史（A股，只列已实施）----
const dividendRows = computed(() =>
  (datasets.value.dividend_history || []).filter((row) => row.div_proc === '实施')
)
const latestDividendAnn = computed(() =>
  latestImplementedDividendAnnDate(datasets.value.dividend_history || [])
)

// ---- 风险信号 ----
const latestAudit = computed(() => {
  const row = (datasets.value.fina_audit || [])[0]
  if (!row) return ''
  return `${formatPeriod(row.end_date)}：${row.audit_result || EMPTY}（${row.audit_agency || EMPTY}）`
})
const latestPledge = computed(() => {
  const row = (datasets.value.pledge_stat || [])[0]
  if (!row) return ''
  return `${formatPeriod(row.end_date)}：质押比例 ${formatPct(row.pledge_ratio)}%（${formatCount(row.pledge_count)} 笔）`
})
const holderTrades = computed(() =>
  (datasets.value.stk_holdertrade || []).slice(0, 5).map((row) => {
    const direction = row.in_de === 'IN' ? '增持' : '减持'
    return `${formatPeriod(row.ann_date)} ${row.holder_name || EMPTY} ${direction} ${formatCount(row.change_vol)} 股（占比 ${formatFixed(row.change_ratio, 2, 4)}%）`
  })
)

// ---- 标的事件 ----
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
    return `每股税前 ${formatPerShare(payload.cash_div_tax)} 元（${payload.div_proc || '预案'}）`
  }
  if (row.event_type === 'SHARE_UNLOCK') {
    // Tushare share_float.float_share 单位是「股」，换算成万股展示（此前直接写「万股」差一万倍）
    return `解禁 ${formatWanShares(payload.float_share)} 万股（占总股本 ${formatPct(payload.float_ratio_pct)}%）`
  }
  if (row.event_type === 'EARNINGS_DISCLOSURE') {
    return `报告期 ${formatPeriod(payload.period)}`
  }
  return ''
}
</script>

<template>
  <!-- 估值快照与业绩预告（A股） -->
  <section v-if="isA && (valuation || forecast)" class="sd-block" data-testid="valuation-section">
    <div class="sd-block-header">
      <h3 class="sd-block-title">
        估值快照
        <span v-if="valuation" class="sd-title-note"
          >交易日 {{ formatPeriod(valuation.trade_date) }}</span
        >
      </h3>
    </div>
    <el-descriptions
      v-if="valuation"
      :column="isMobile ? 2 : 3"
      border
      size="small"
      class="valuation-grid"
    >
      <el-descriptions-item label="收盘价">{{ formatEps(valuation.close) }}</el-descriptions-item>
      <el-descriptions-item label="PE(TTM)">{{
        formatRatio(valuation.pe_ttm)
      }}</el-descriptions-item>
      <el-descriptions-item label="PB">{{ formatRatio(valuation.pb) }}</el-descriptions-item>
      <el-descriptions-item label="PS(TTM)">{{
        formatRatio(valuation.ps_ttm)
      }}</el-descriptions-item>
      <el-descriptions-item label="股息率(TTM)%">{{
        formatPct(valuation.dv_ttm)
      }}</el-descriptions-item>
      <el-descriptions-item label="总市值(亿元)">{{
        formatFixed(isNum(valuation.total_mv) ? valuation.total_mv / 1e4 : null, 2)
      }}</el-descriptions-item>
    </el-descriptions>
    <div v-if="forecast" class="sd-footnote">
      业绩预告（报告期 {{ formatPeriod(forecast.end_date) }}，公告
      {{ formatPeriod(forecast.ann_date) }}）：{{ forecastText || EMPTY }}
      <template v-if="forecast.summary">。{{ forecast.summary }}</template>
    </div>
  </section>

  <!-- 财务指标（A股 Tushare） -->
  <section v-if="isA" class="sd-block">
    <div class="sd-block-header">
      <h3 class="sd-block-title">
        财务指标
        <span class="sd-title-note"
          >近 {{ finaRows.length }} 期<template v-if="state.latestPeriods.fina_indicator"
            >，最新报告期 {{ formatPeriod(state.latestPeriods.fina_indicator) }}</template
          ></span
        >
        <el-tooltip
          content="一季报/中报/三季报为年初至今累计口径，与年报混排时注意期别"
          placement="top"
        >
          <el-icon class="sd-help"><InfoFilled /></el-icon>
        </el-tooltip>
      </h3>
      <el-switch v-model="finaAnnualOnly" size="small" active-text="只看年报" />
    </div>
    <el-table :data="finaRows" size="small" stripe>
      <template #empty>
        <el-empty description="暂无数据；生成分析时会自动同步" :image-size="56" />
      </template>
      <el-table-column label="报告期" width="104">
        <template #default="{ row }">{{ formatPeriod(row.end_date) }}</template>
      </el-table-column>
      <el-table-column label="期别" width="70">
        <template #default="{ row }">{{ periodKindLabel(row.end_date) }}</template>
      </el-table-column>
      <el-table-column label="EPS" align="right" min-width="70">
        <template #default="{ row }">{{ formatEps(row.eps) }}</template>
      </el-table-column>
      <el-table-column label="ROE%" align="right" min-width="70">
        <template #default="{ row }">{{ formatPct(row.roe) }}</template>
      </el-table-column>
      <el-table-column label="毛利率%" align="right" min-width="76">
        <template #default="{ row }">{{ formatPct(row.grossprofit_margin) }}</template>
      </el-table-column>
      <el-table-column label="净利率%" align="right" min-width="76">
        <template #default="{ row }">{{ formatPct(row.netprofit_margin) }}</template>
      </el-table-column>
      <el-table-column label="资产负债率%" align="right" min-width="96">
        <template #default="{ row }">{{ formatPct(row.debt_to_assets) }}</template>
      </el-table-column>
      <el-table-column label="营收同比%" align="right" min-width="86">
        <template #default="{ row }">{{ formatPct(row.or_yoy ?? row.tr_yoy) }}</template>
      </el-table-column>
      <el-table-column label="净利同比%" align="right" min-width="86">
        <template #default="{ row }">{{ formatPct(row.netprofit_yoy) }}</template>
      </el-table-column>
    </el-table>
  </section>

  <!-- 利润质量指标 -->
  <section class="sd-block" data-testid="earnings-quality-section">
    <div class="sd-block-header">
      <h3 class="sd-block-title">
        利润质量指标
        <el-tooltip
          content="红色 = 触及红旗阈值（CFO/净利润 < 0.8、应计率 > 0.1、增速差 > 20pp、扣非占比 < 0.7、M-score 超阈值）；口径见 AI 分析「利润质量与会计风险」章节"
          placement="top"
        >
          <el-icon class="sd-help"><InfoFilled /></el-icon>
        </el-tooltip>
      </h3>
    </div>
    <el-table :data="qualityRows" size="small" stripe>
      <template #empty>
        <el-empty description="暂无数据；生成分析时会自动同步" :image-size="56" />
      </template>
      <el-table-column label="年度" prop="year" width="64" />
      <el-table-column label="CFO/净利润" align="right" min-width="90">
        <template #default="{ row }">
          <span :class="{ 'sd-red-flag': isNum(row.cfo_ni_ratio) && row.cfo_ni_ratio < 0.8 }">
            {{ formatRatio(row.cfo_ni_ratio) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="应计率" align="right" min-width="70">
        <template #default="{ row }">
          <span :class="{ 'sd-red-flag': isNum(row.accruals_ratio) && row.accruals_ratio > 0.1 }">
            {{ formatRatio(row.accruals_ratio) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="应收-营收增速差pp" align="right" min-width="130">
        <template #default="{ row }">
          <span
            :class="{
              'sd-red-flag':
                isNum(row.receivable_vs_revenue_gap_pp) && row.receivable_vs_revenue_gap_pp > 20
            }"
          >
            {{ formatPp(row.receivable_vs_revenue_gap_pp) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="存货-营收增速差pp" align="right" min-width="130">
        <template #default="{ row }">
          <span
            :class="{
              'sd-red-flag':
                isNum(row.inventory_vs_revenue_gap_pp) && row.inventory_vs_revenue_gap_pp > 20
            }"
          >
            {{ formatPp(row.inventory_vs_revenue_gap_pp) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="毛利率%" align="right" min-width="76">
        <template #default="{ row }">{{ formatPct(row.gross_margin) }}</template>
      </el-table-column>
      <!-- 扣非占比只有 A 股有数据源（Tushare 扣非净利润），美/港股永远缺 → 不显示列 -->
      <el-table-column v-if="isA" label="扣非占比" align="right" min-width="76">
        <template #default="{ row }">
          <span
            :class="{
              'sd-red-flag': isNum(row.recurring_profit_share) && row.recurring_profit_share < 0.7
            }"
          >
            {{ formatRatio(row.recurring_profit_share) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="M-score" align="right" min-width="74">
        <template #default="{ row }">
          <span :class="{ 'sd-red-flag': row.m_flag }">{{ formatRatio(row.m_score) }}</span>
        </template>
      </el-table-column>
    </el-table>
    <div v-if="isNum(cfoNi5y)" class="sd-footnote">
      近 5 年累计 CFO/净利润：
      <span :class="{ 'sd-red-flag': cfoNi5y! < 0.8 }">{{ formatRatio(cfoNi5y) }}</span>
    </div>
    <div
      v-for="note in qualityCurrencyNotes"
      :key="note"
      class="sd-footnote"
      data-testid="quality-currency-note"
    >
      {{ note }}
    </div>
  </section>

  <!-- 分红历史（A股） -->
  <section v-if="isA" class="sd-block">
    <div class="sd-block-header">
      <h3 class="sd-block-title">
        分红历史
        <span class="sd-title-note"
          >已实施<template v-if="latestDividendAnn"
            >，最新实施公告 {{ formatPeriod(latestDividendAnn) }}</template
          ></span
        >
      </h3>
    </div>
    <el-table :data="dividendRows" size="small" stripe>
      <template #empty>
        <el-empty description="暂无已实施的分红记录" :image-size="56" />
      </template>
      <el-table-column label="报告期" width="104">
        <template #default="{ row }">{{ formatPeriod(row.end_date) }}</template>
      </el-table-column>
      <el-table-column label="每股税前(元)" align="right">
        <template #default="{ row }">{{ formatPerShare(row.cash_div_tax) }}</template>
      </el-table-column>
      <el-table-column label="每股送转" align="right">
        <template #default="{ row }">{{ formatPerShare(row.stk_div) }}</template>
      </el-table-column>
      <el-table-column label="除权日" width="104">
        <template #default="{ row }">{{ formatPeriod(row.ex_date) }}</template>
      </el-table-column>
    </el-table>
  </section>

  <!-- 风险信号（A股数据源；美/港股见 AI 分析中的说明） -->
  <section v-if="state.capabilities.risk_signals === true" class="sd-block">
    <div class="sd-block-header">
      <h3 class="sd-block-title">风险信号</h3>
    </div>
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

  <!-- 标的事件（A股：分红公告同步时抓取） -->
  <section v-if="isA" class="sd-block">
    <div class="sd-block-header">
      <h3 class="sd-block-title">标的事件</h3>
    </div>
    <el-table :data="state.events" size="small" stripe>
      <template #empty>
        <el-empty description="暂无事件；公司行动页同步分红公告时会一并抓取" :image-size="56" />
      </template>
      <el-table-column label="日期" width="110">
        <template #default="{ row }">{{ formatPeriod(row.event_date) }}</template>
      </el-table-column>
      <el-table-column label="类型" width="100">
        <template #default="{ row }">{{ eventTypeLabel(row.event_type) }}</template>
      </el-table-column>
      <el-table-column label="详情" min-width="200">
        <template #default="{ row }">{{ eventDetail(row) }}</template>
      </el-table-column>
    </el-table>
  </section>
</template>

<style scoped>
.valuation-grid :deep(.el-descriptions__content) {
  font-variant-numeric: tabular-nums;
}
</style>
