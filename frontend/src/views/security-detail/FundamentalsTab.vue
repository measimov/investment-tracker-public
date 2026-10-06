<script setup lang="ts">
/**
 * 「基本面」tab：估值快照与业绩预告（A股）、财务指标（A股）、利润质量指标、
 * 分红历史（A股）、风险信号、标的事件（A股）。
 */
import { NDataTable, NEmpty, NSwitch, type DataTableColumns } from 'naive-ui'
import ResearchNote from './ResearchNote.vue'
import { computed, ref, h, type Directive } from 'vue'
import { EMPTY } from '@/utils/helpers'
import { securityEventTypeLabel as eventTypeLabel } from '@/utils/labels'
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

// Naive 的公开 contentClass 定位内容层，父层为实际滚动区；保留原生左右键滚动。
const vTableScrollFocus: Directive<HTMLElement, string> = {
  mounted(element, { value }) {
    const container = element.querySelector<HTMLElement>('.sd-financial-content')?.parentElement
    if (!container) return
    container.classList.add('sd-financial-scroll')
    container.tabIndex = 0
    container.setAttribute('role', 'region')
    container.setAttribute('aria-label', value)
  }
}

const isA = computed(() => props.market === 'A股')
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
// 报告币种中途切换（后端只在切换时给出）：切换年的跨年指标不计；近 5 年累计只含连续且同币种的年份
const qualityCurrencyNotes = computed<string[]>(() => {
  const quality = props.state.earningsQuality
  const changes = (quality.currency_changes || []) as Array<{ year: string; change: string }>
  const notes = changes.map(
    (item) => `${item.year} 年起报告币种 ${item.change}：该年相对上一年的增速差与 M-score 不计`
  )
  if (quality.cfo_ni_ratio_5y_note) notes.push(String(quality.cfo_ni_ratio_5y_note))
  if (quality.cfo_ni_ratio_5y_unavailable === 'ni_non_positive')
    notes.push('近 5 年累计净利润 ≤ 0：累计 CFO/净利润不计')
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
  const ratio = formatPct(row.pledge_ratio)
  return `${formatPeriod(row.end_date)}：质押比例 ${ratio === EMPTY ? ratio : ratio + '%'}（${formatCount(row.pledge_count)} 笔）`
})
const holderTrades = computed(() =>
  (datasets.value.stk_holdertrade || []).slice(0, 5).map((row) => {
    const direction = row.in_de === 'IN' ? '增持' : '减持'
    const ratio = formatFixed(row.change_ratio, 2, 4)
    return `${formatPeriod(row.ann_date)} ${row.holder_name || EMPTY} ${direction} ${formatCount(row.change_vol)} 股（占比 ${ratio === EMPTY ? ratio : ratio + '%'}）`
  })
)

// ---- 标的事件 ----
function eventDetail(row: ProfileRow): string {
  const payload = row.payload || {}
  if (row.event_type === 'DIVIDEND_PLAN') {
    // 港股（披露易）事件带派发币种与股息类型；A/B 股（Tushare）为人民币元
    if (payload.currency) {
      const kinds = Array.isArray(payload.dividend_types) ? payload.dividend_types.join('+') : ''
      return `每股 ${formatPerShare(payload.cash_div_tax)} ${payload.currency}（${kinds || payload.div_proc || '公告'}）`
    }
    const dateNote = payload.date_basis === 'announcement' ? '；日期为公告日，除权除息日未公布' : ''
    return `每股税前 ${formatPerShare(payload.cash_div_tax)} 元（${payload.div_proc || '预案'}）${dateNote}`
  }
  if (row.event_type === 'SHARE_UNLOCK') {
    // Tushare share_float.float_share 单位是「股」，换算成万股展示（此前直接写「万股」差一万倍）
    const ratio = formatPct(payload.float_ratio_pct)
    return `解禁 ${formatWanShares(payload.float_share)} 万股（占总股本 ${ratio === EMPTY ? ratio : ratio + '%'}）`
  }
  if (row.event_type === 'EARNINGS_DISCLOSURE') {
    return `报告期 ${formatPeriod(payload.period)}`
  }
  return ''
}

const finaColumns: DataTableColumns<ProfileRow> = [
  {
    title: '报告期',
    key: 'end_date',
    width: 120,
    fixed: 'left',
    render: (row) => formatPeriod(row.end_date)
  },
  { title: '期别', key: 'period', width: 70, render: (row) => periodKindLabel(row.end_date) },
  {
    title: 'EPS',
    key: 'eps',
    align: 'right',
    className: 'sd-numeric',
    render: (row) => formatEps(row.eps)
  },
  {
    title: 'ROE%',
    key: 'roe',
    align: 'right',
    className: 'sd-numeric',
    render: (row) => formatPct(row.roe)
  },
  {
    title: '毛利率%',
    key: 'grossprofit_margin',
    align: 'right',
    className: 'sd-numeric',
    render: (row) => formatPct(row.grossprofit_margin)
  },
  {
    title: '净利率%',
    key: 'netprofit_margin',
    align: 'right',
    className: 'sd-numeric',
    render: (row) => formatPct(row.netprofit_margin)
  },
  {
    title: '资产负债率%',
    key: 'debt_to_assets',
    align: 'right',
    className: 'sd-numeric',
    render: (row) => formatPct(row.debt_to_assets)
  },
  {
    title: '营收同比%',
    key: 'or_yoy',
    align: 'right',
    className: 'sd-numeric',
    render: (row) => formatPct(row.or_yoy ?? row.tr_yoy)
  },
  {
    title: '净利同比%',
    key: 'netprofit_yoy',
    align: 'right',
    className: 'sd-numeric',
    render: (row) => formatPct(row.netprofit_yoy)
  }
]
const qualityColumns = computed<DataTableColumns<ProfileRow>>(() => [
  { title: '年度', key: 'year', width: 64, fixed: 'left' },
  {
    title: 'CFO/净利润',
    key: 'cfo_ni_ratio',
    align: 'right',
    className: 'sd-numeric',
    render: (row) =>
      h(
        'span',
        { class: isNum(row.cfo_ni_ratio) && row.cfo_ni_ratio < 0.8 ? 'sd-red-flag' : '' },
        formatRatio(row.cfo_ni_ratio)
      )
  },
  {
    title: '应计率',
    key: 'accruals_ratio',
    align: 'right',
    className: 'sd-numeric',
    render: (row) =>
      h(
        'span',
        { class: isNum(row.accruals_ratio) && row.accruals_ratio > 0.1 ? 'sd-red-flag' : '' },
        formatRatio(row.accruals_ratio)
      )
  },
  {
    title: '应收-营收增速差pp',
    key: 'receivable_vs_revenue_gap_pp',
    align: 'right',
    className: 'sd-numeric',
    render: (row) =>
      h(
        'span',
        {
          class:
            isNum(row.receivable_vs_revenue_gap_pp) && row.receivable_vs_revenue_gap_pp > 20
              ? 'sd-red-flag'
              : ''
        },
        formatPp(row.receivable_vs_revenue_gap_pp)
      )
  },
  {
    title: '存货-营收增速差pp',
    key: 'inventory_vs_revenue_gap_pp',
    align: 'right',
    className: 'sd-numeric',
    render: (row) =>
      h(
        'span',
        {
          class:
            isNum(row.inventory_vs_revenue_gap_pp) && row.inventory_vs_revenue_gap_pp > 20
              ? 'sd-red-flag'
              : ''
        },
        formatPp(row.inventory_vs_revenue_gap_pp)
      )
  },
  {
    title: '毛利率%',
    key: 'gross_margin',
    align: 'right',
    className: 'sd-numeric',
    render: (row) => formatPct(row.gross_margin)
  },
  ...(isA.value
    ? [
        {
          title: '扣非占比',
          key: 'recurring_profit_share',
          align: 'right' as const,
          className: 'sd-numeric',
          render: (row: ProfileRow) =>
            h(
              'span',
              {
                class:
                  isNum(row.recurring_profit_share) && row.recurring_profit_share < 0.7
                    ? 'sd-red-flag'
                    : ''
              },
              formatRatio(row.recurring_profit_share)
            )
        }
      ]
    : []),
  {
    title: 'M-score',
    key: 'm_score',
    align: 'right',
    className: 'sd-numeric',
    render: (row) => h('span', { class: row.m_flag ? 'sd-red-flag' : '' }, formatRatio(row.m_score))
  }
])
const dividendColumns: DataTableColumns<ProfileRow> = [
  {
    title: '报告期',
    key: 'end_date',
    width: 120,
    fixed: 'left',
    render: (row) => formatPeriod(row.end_date)
  },
  {
    title: '每股税前(元)',
    key: 'cash_div_tax',
    align: 'right',
    className: 'sd-numeric',
    render: (row) => formatPerShare(row.cash_div_tax)
  },
  {
    title: '每股送转',
    key: 'stk_div',
    align: 'right',
    className: 'sd-numeric',
    render: (row) => formatPerShare(row.stk_div)
  },
  { title: '除权除息日', key: 'ex_date', width: 104, render: (row) => formatPeriod(row.ex_date) }
]
const eventColumns: DataTableColumns<ProfileRow> = [
  { title: '日期', key: 'event_date', width: 110, render: (row) => formatPeriod(row.event_date) },
  { title: '类型', key: 'event_type', width: 100, render: (row) => eventTypeLabel(row.event_type) },
  { title: '详情', key: 'detail', render: (row) => eventDetail(row) }
]
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
    <dl v-if="valuation" class="valuation-grid">
      <div>
        <dt>收盘价</dt>
        <dd>{{ formatEps(valuation.close) }}</dd>
      </div>
      <div>
        <dt>PE（TTM）</dt>
        <dd>{{ formatRatio(valuation.pe_ttm) }}</dd>
      </div>
      <div>
        <dt>PB</dt>
        <dd>{{ formatRatio(valuation.pb) }}</dd>
      </div>
      <div>
        <dt>PS（TTM）</dt>
        <dd>{{ formatRatio(valuation.ps_ttm) }}</dd>
      </div>
      <div>
        <dt>股息率（TTM）%</dt>
        <dd>{{ formatPct(valuation.dv_ttm) }}</dd>
      </div>
      <div>
        <dt>总市值(亿元)</dt>
        <dd>{{ formatFixed(isNum(valuation.total_mv) ? valuation.total_mv / 1e4 : null, 2) }}</dd>
      </div>
    </dl>
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
        <ResearchNote
          popover
          label="查看财务指标期别口径"
          text="一季报/中报/三季报为年初至今累计口径，与年报混排时注意期别"
        />
      </h3>
      <label class="fina-switch"
        ><NSwitch v-model:value="finaAnnualOnly" aria-label="财务指标只看年报" />只看年报</label
      >
    </div>
    <p class="sd-financial-scroll-hint">
      左右滑动查看更多列；首列固定便于核对，也可用左右方向键滚动。
    </p>
    <div v-table-scroll-focus="'财务指标表格，可横向滚动'" class="sd-fixed-table">
      <NDataTable
        class="sd-data-table"
        :columns="finaColumns"
        :data="finaRows"
        :bordered="false"
        :scroll-x="856"
        :scrollbar-props="{ contentClass: 'sd-financial-content' }"
        ><template #empty><NEmpty description="暂无数据；生成分析时会自动同步" /></template
      ></NDataTable>
    </div>
  </section>

  <!-- 利润质量指标 -->
  <section class="sd-block" data-testid="earnings-quality-section">
    <div class="sd-block-header">
      <h3 class="sd-block-title">
        利润质量指标
        <ResearchNote
          popover
          label="查看利润质量阈值与缺值口径"
          text="红色 = 触及红旗阈值（CFO/净利润 &lt; 0.8、应计率 &gt; 0.1、增速差 &gt; 20pp、扣非占比 &lt; 0.7、M-score 超阈值）；净利润 ≤ 0 的年份 CFO/净利润与扣非占比不计（显示 —）；口径见 AI 分析「利润质量与会计风险」章节"
        />
      </h3>
    </div>
    <p class="sd-financial-scroll-hint">
      左右滑动查看更多列；首列固定便于核对，也可用左右方向键滚动。
    </p>
    <div v-table-scroll-focus="'利润质量指标表格，可横向滚动'" class="sd-fixed-table">
      <NDataTable
        class="sd-data-table"
        :columns="qualityColumns"
        :data="qualityRows"
        :bordered="false"
        :scroll-x="730"
        :scrollbar-props="{ contentClass: 'sd-financial-content' }"
        ><template #empty><NEmpty description="暂无数据；生成分析时会自动同步" /></template
      ></NDataTable>
    </div>
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
    <p class="sd-financial-scroll-hint">
      左右滑动查看更多列；首列固定便于核对，也可用左右方向键滚动。
    </p>
    <div v-table-scroll-focus="'分红历史表格，可横向滚动'" class="sd-fixed-table">
      <NDataTable
        class="sd-data-table"
        :columns="dividendColumns"
        :data="dividendRows"
        :bordered="false"
        :scroll-x="476"
        :scrollbar-props="{ contentClass: 'sd-financial-content' }"
        ><template #empty><NEmpty description="暂无已实施的分红记录" /></template
      ></NDataTable>
    </div>
  </section>

  <!-- 风险信号（A股数据源；美/港股见 AI 分析中的说明） -->
  <section v-if="state.capabilities.risk_signals === true" class="sd-block">
    <div class="sd-block-header">
      <h3 class="sd-block-title">风险信号</h3>
    </div>
    <dl class="sd-field-list">
      <dt>审计意见（最近）</dt>
      <dd>{{ latestAudit || '数据不足' }}</dd>
      <dt>股权质押（最近）</dt>
      <dd>{{ latestPledge || '数据不足' }}</dd>
      <dt>股东增减持（近5条）</dt>
      <dd>
        <div v-if="holderTrades.length">
          <div v-for="(trade, index) in holderTrades" :key="index">{{ trade }}</div>
        </div>
        <template v-else>数据不足</template>
      </dd>
    </dl>
  </section>

  <!-- 标的事件（A股：分红公告同步时抓取） -->
  <section v-if="isA" class="sd-block">
    <div class="sd-block-header">
      <h3 class="sd-block-title">标的事件</h3>
    </div>
    <div class="sd-table-scroll" tabindex="0" role="region" aria-label="标的事件表格，可横向滚动">
      <NDataTable
        class="sd-data-table"
        :columns="eventColumns"
        :data="state.events"
        :bordered="false"
        :style="{ minWidth: '460px' }"
        ><template #empty
          ><NEmpty description="暂无事件；公司行动页同步分红公告时会一并抓取" /></template
      ></NDataTable>
    </div>
  </section>
</template>

<style scoped>
.fina-switch {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.valuation-grid {
  margin: 0;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 18px 24px;
}
.valuation-grid dt {
  font-size: 12px;
  color: var(--app-text-muted);
}
.valuation-grid dd {
  font-size: 20px;
  margin: 6px 0 0;
  font-variant-numeric: tabular-nums;
}
@media (max-width: 640px) {
  .valuation-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 18px 14px;
  }
}
</style>
