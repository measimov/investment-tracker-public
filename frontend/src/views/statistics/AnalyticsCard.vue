<script setup lang="ts">
import HelpTip from '@/components/HelpTip.vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import {
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  GridComponent,
  DataZoomComponent
} from 'echarts/components'
import VChart from 'vue-echarts'
import { useChartFontOption } from '@/composables/useChartFontOption'
import { useElementSize } from '@vueuse/core'
import { computed, defineAsyncComponent, ref } from 'vue'
import { NAlert, NButton, NEmpty, NTag } from 'naive-ui'
import { EMPTY, formatCurrency, formatDate, profitColor as getProfitColor } from '@/utils/helpers'
import { RANGE_PRESETS, type AnalyticsFeature } from './useAnalytics'
import {
  formatNullableNumber,
  formatNullablePercent,
  formatPlainPercent,
  nullableFiniteNumber
} from './format'

use([
  CanvasRenderer,
  LineChart,
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  GridComponent,
  DataZoomComponent
])

const props = defineProps<{ analytics: AnalyticsFeature }>()
const chartFigure = ref<HTMLElement | null>(null)
const { width: chartWidth } = useElementSize(chartFigure)
const chartOption = useChartFontOption(() => {
  const option = props.analytics.chartOption
  // 侧栏变化也会改变图表宽度，刻度密度按绘图区而非浏览器断点决定。
  const tickCount = Math.max(2, Math.floor((chartWidth.value - 64) / 112))
  const interval = Math.max(0, Math.ceil(props.analytics.curve.length / tickCount) - 1)
  return {
    ...option,
    xAxis: option.xAxis.map((axis) => ({
      ...axis,
      axisLabel: { ...axis.axisLabel, interval }
    }))
  }
})
const DateRangeDialog = defineAsyncComponent(() => import('@/components/DateRangeDialog.vue'))
const dateDialogOpened = ref(false)
const dateDialogVisible = ref(false)
// 只读取当前成功响应的期末点；未知期末不能回退成较早点的收益。
const terminalPoint = computed(() => props.analytics.curve.at(-1))
const terminalRate = computed(() =>
  nullableFiniteNumber(terminalPoint.value?.cumulative_return_rate)
)
const cumulativeReturnText = computed(() => formatNullablePercent(terminalRate.value))
function selectPreset(value: string) {
  if (value === 'custom') {
    openDateRange()
    return
  }
  props.analytics.state.rangePreset = value
}
function openDateRange() {
  dateDialogOpened.value = true
  dateDialogVisible.value = true
}
function applyDateRange(range: [string, string] | null) {
  props.analytics.state.customRange = range
  props.analytics.state.rangePreset = range ? 'custom' : 'all'
  dateDialogVisible.value = false
}
const curveDescription = computed(() => {
  const curve = props.analytics.curve
  if (!curve.length) return ''
  const last = curve[curve.length - 1]
  return `证券组合收益率曲线，${props.analytics.effectiveRangeLabel}；期末累计 TTWR ${cumulativeReturnText.value}，回撤 ${formatPlainPercent(nullableFiniteNumber(last.drawdown_rate))}。${props.analytics.state.loaded && (props.analytics.state.error || props.analytics.state.loading) ? '保留上次成功结果，当前选择尚未确认。' : ''}`
})
</script>

<template>
  <section
    class="analytics-section"
    aria-labelledby="analytics-title"
    :aria-busy="analytics.state.loading"
  >
    <header class="analytics-heading">
      <div>
        <h2 id="analytics-title">区间收益</h2>
        <HelpTip label="查看收益图阅读说明">
          <p>
            上图为组合与基准累计收益，下图为回撤，两图共用日期轴。悬浮或点触曲线可查看当日数值。
          </p>
          <p>日期筛选决定下方指标的统计区间；拖动图底时间条仅缩放曲线。</p>
        </HelpTip>
        <NTag type="warning" size="small" :bordered="false">实验指标</NTag
        ><NTag v-if="analytics.state.whatIfPrices" type="warning" size="small" :bordered="false"
          >手工价试算中</NTag
        >
      </div>
      <NButton
        size="small"
        :loading="analytics.state.historyRefreshing"
        @click="analytics.refreshHistoryAndReload"
        >同步历史行情</NButton
      >
    </header>
    <div class="range-toolbar">
      <div class="range-presets" role="group" aria-label="收益曲线区间">
        <button
          v-for="preset in RANGE_PRESETS"
          :key="preset.value"
          type="button"
          :aria-pressed="analytics.state.rangePreset === preset.value"
          @click="selectPreset(preset.value)"
        >
          {{ preset.label }}
        </button>
      </div>
      <NButton
        v-if="analytics.state.rangePreset === 'custom'"
        @click="openDateRange"
        aria-haspopup="dialog"
        :aria-expanded="dateDialogVisible"
        >{{
          analytics.state.customRange
            ? `${formatDate(analytics.state.customRange[0])} 至 ${formatDate(analytics.state.customRange[1])}`
            : '选择收益区间'
        }}</NButton
      >
      <el-select
        v-model="analytics.state.selectedBenchmarks"
        multiple
        filterable
        collapse-tags
        :multiple-limit="3"
        default-first-option
        :loading="analytics.state.benchmarkCatalogLoading"
        aria-label="对比基准，最多三项"
        placeholder="对比基准（最多三项）"
        class="benchmark-select"
        data-testid="benchmark-select"
      >
        <el-option
          v-for="option in analytics.state.benchmarkOptions"
          :key="option.code"
          :label="option.name"
          :value="option.code"
        />
        <template #empty><p class="benchmark-empty">暂无匹配的基准</p></template>
      </el-select>
    </div>
    <NAlert v-if="analytics.state.benchmarkCatalogError" type="warning"
      >基准目录加载失败：{{ analytics.state.benchmarkCatalogError
      }}<span class="retry-action"
        ><NButton
          size="small"
          :loading="analytics.state.benchmarkCatalogLoading"
          @click="analytics.loadBenchmarkCatalog"
          >重试基准目录</NButton
        ></span
      ></NAlert
    >

    <NAlert
      v-if="analytics.state.error"
      type="warning"
      class="analytics-error"
      data-testid="analytics-error"
      >收益率曲线加载失败：{{ analytics.state.error }}；{{
        analytics.state.loaded
          ? '保留上次成功结果，当前区间与基准尚未确认'
          : '尚无可用数据，平仓样本与曲线未知'
      }}<span class="retry-action"
        ><NButton size="small" @click="analytics.load()">重试收益曲线</NButton></span
      ></NAlert
    >
    <p v-else-if="analytics.state.loading" class="range-label" role="status">正在加载收益曲线…</p>
    <div class="benchmark-status">
      <NTag
        v-for="block in analytics.unavailableBenchmarks"
        :key="block.code"
        size="small"
        :bordered="false"
        >{{ block.name }} 无基准数据</NTag
      >
      <p v-for="block in analytics.partialBenchmarks" :key="block.code">
        {{ block.name }} 起点晚于区间：计量区间不一致，不计算超额收益；可同步更早历史行情补齐。
      </p>
    </div>
    <div v-if="analytics.warnings.length" class="analytics-warnings">
      <NAlert v-for="warning in analytics.warnings" :key="warning" type="warning">{{
        warning
      }}</NAlert>
    </div>
    <div v-if="analytics.state.syncJob" class="job-progress">
      <div class="job-progress-header">
        <span>{{ analytics.syncStatusText }}</span>
        <span>
          {{ analytics.state.syncJob.completed || 0 }}/{{ analytics.state.syncJob.total || 0 }}
          <template v-if="analytics.state.syncJob.current_symbol">
            · {{ analytics.state.syncJob.current_symbol }}
            {{ analytics.state.syncJob.current_market }}
          </template>
        </span>
      </div>
      <el-progress
        :percentage="analytics.syncPercent"
        :status="analytics.syncProgressStatus"
        :stroke-width="10"
      />
      <div class="job-progress-detail">
        成功 {{ analytics.state.syncJob.success_count || 0 }} · 跳过
        {{ analytics.state.syncJob.skipped_count || 0 }} · 失败
        {{ analytics.state.syncJob.failed_count || 0 }}
      </div>
      <div v-if="analytics.state.syncJob.error" class="job-progress-error">
        {{ analytics.state.syncJob.error }}
      </div>
    </div>

    <NEmpty
      v-if="analytics.curve.length === 0"
      :description="
        analytics.state.error
          ? '收益曲线暂不可用'
          : analytics.state.loading || !analytics.state.loaded
            ? '收益曲线尚未加载'
            : '暂无收益率曲线数据'
      "
    />
    <div v-else ref="chartFigure" class="curve-figure" role="img" :aria-label="curveDescription">
      <VChart
        :update-options="{ notMerge: false, replaceMerge: ['series'] }"
        :option="chartOption"
        class="chart-performance"
        :autoresize="{ throttle: 32 }"
      />
    </div>
    <div class="analytics-summary-grid" aria-label="区间收益核心指标">
      <div class="analytics-metric primary-metric">
        <span class="metric-label">
          累计 TTWR
          <HelpTip label="查看区间收益范围与 TTWR 口径">
            <p>
              {{ analytics.effectiveRangeLabel || '收益区间尚未加载'
              }}<template
                v-if="analytics.state.loaded && (analytics.state.loading || analytics.state.error)"
                >；保留上次成功结果，当前选择尚未确认。</template
              >
            </p>
            TTWR
            基于证券交易现金流估算，未包含账户现金和真实外部入出金；股息按到账日计入，除息至到账期间可能暂时拉低收益。区间不足半年时年化仅供参考。
          </HelpTip>
        </span>
        <strong
          class="metric-value ttwr-value"
          :style="{ color: getProfitColor(terminalRate) }"
          data-testid="range-ttwr"
          >{{ cumulativeReturnText }}</strong
        >
        <span v-if="cumulativeReturnText === EMPTY" class="metric-hint" data-testid="ttwr-unknown"
          >期末未知</span
        >
      </div>
      <div class="analytics-metric">
        <span class="metric-label"
          >年化 TTWR
          <HelpTip label="查看年化 TTWR 口径"
            >将所选区间的累计 TTWR 换算为年化收益率。区间不足半年时年化仅供参考。</HelpTip
          >
        </span>
        <span
          class="metric-value"
          :style="{ color: getProfitColor(analytics.metrics.annualized_return_rate) }"
        >
          {{ formatNullablePercent(analytics.metrics.annualized_return_rate) }}
        </span>
        <span
          v-if="analytics.shortRange && analytics.metrics.annualized_return_rate != null"
          class="metric-hint"
        >
          短区间 · 仅供参考
        </span>
      </div>
      <div class="analytics-metric">
        <span class="metric-label"
          >最大回撤
          <HelpTip label="查看最大回撤口径"
            >所选区间内，组合收益曲线从此前高点回落的最大幅度。</HelpTip
          >
        </span>
        <span class="metric-value danger">
          {{ formatPlainPercent(analytics.metrics.max_drawdown_rate) }}
        </span>
      </div>
      <div class="analytics-metric">
        <span class="metric-label benchmark-label">
          <span>超额收益</span>
          <span v-if="analytics.primaryBenchmarkComparison" class="benchmark-name">{{
            analytics.primaryBenchmarkComparison.name
          }}</span>
          <HelpTip label="查看区间超额收益计算口径"
            ><p v-if="analytics.primaryBenchmarkComparison">
              对比基准：{{ analytics.primaryBenchmarkComparison.name }}。
            </p>
            组合 TTWR
            与基准累计收益的算术差（百分点）；基准为价格指数、不含股息、原币口径。</HelpTip
          >
        </span>
        <span
          class="metric-value"
          :style="{
            color: getProfitColor(analytics.primaryBenchmarkComparison?.excess_return_rate)
          }"
        >
          {{ formatNullablePercent(analytics.primaryBenchmarkComparison?.excess_return_rate) }}
        </span>
        <span v-if="!analytics.primaryBenchmarkComparison" class="metric-hint">无可比基准</span>
      </div>
      <div class="analytics-metric">
        <span class="metric-label">
          年化 XIRR
          <HelpTip label="查看年化 XIRR 口径"
            >所选区间的资金加权年化收益率（XIRR），考虑现金流发生的时间与金额。区间不足半年时年化仅供参考。</HelpTip
          >
        </span>
        <span
          class="metric-value"
          :style="{ color: getProfitColor(analytics.rangeSummary.xirr_annualized_rate) }"
        >
          {{ formatNullablePercent(analytics.rangeSummary.xirr_annualized_rate) }}
        </span>
        <span
          v-if="analytics.shortRange && analytics.rangeSummary.xirr_annualized_rate != null"
          class="metric-hint"
        >
          短区间 · 仅供参考
        </span>
      </div>
    </div>
    <div class="risk-details" aria-label="风险与平仓交易明细">
      <section class="metric-group" aria-labelledby="risk-metrics-title">
        <h3 id="risk-metrics-title">风险调整</h3>
        <div class="risk-metrics">
          <div class="analytics-metric">
            <span class="metric-label"
              >夏普率<HelpTip label="查看夏普率计算口径"
                >{{ analytics.riskFreeNote }}。</HelpTip
              ></span
            >
            <span class="metric-value">{{
              formatNullableNumber(analytics.metrics.sharpe_ratio)
            }}</span>
          </div>
          <div class="analytics-metric">
            <span class="metric-label"
              >索提诺率<HelpTip label="查看索提诺率计算口径"
                >{{ analytics.riskFreeNote }}。</HelpTip
              ></span
            >
            <span class="metric-value">{{
              formatNullableNumber(analytics.metrics.sortino_ratio)
            }}</span>
          </div>
          <div class="analytics-metric">
            <span class="metric-label"
              >卡玛率<HelpTip label="查看卡玛率计算口径"
                >年化收益率与最大回撤绝对值之比，用于衡量承受回撤所获得的收益。</HelpTip
              ></span
            >
            <span class="metric-value">{{
              formatNullableNumber(analytics.metrics.calmar_ratio)
            }}</span>
          </div>
        </div>
      </section>
      <section class="metric-group" aria-labelledby="trade-metrics-title">
        <h3 id="trade-metrics-title">
          平仓表现
          <HelpTip label="查看平仓交易样本">
            <template v-if="analytics.state.loaded"
              >样本 {{ analytics.tradeSkill.sample_count ?? 0 }} 笔平仓{{
                analytics.tradeSkill.sample_count ? '' : '，区间内无平仓'
              }}。</template
            >
            <template v-else>平仓样本尚未加载。</template>
            按平仓日落在所选区间内的交易统计，按笔，不是按标的。
          </HelpTip>
        </h3>
        <div class="risk-metrics">
          <div class="analytics-metric" data-testid="win-rate-metric">
            <span class="metric-label"
              >胜率（按笔·实验）<HelpTip label="查看胜率（按笔·实验）统计范围"
                >按平仓日落在所选区间内的每笔平仓交易统计，按笔，不是按标的。</HelpTip
              ></span
            >
            <span class="metric-value">{{
              analytics.tradeSkill.sample_count
                ? formatPlainPercent(analytics.tradeSkill.win_rate)
                : EMPTY
            }}</span>
            <span class="metric-hint sr-only">
              <template v-if="analytics.state.loaded"
                >样本 {{ analytics.tradeSkill.sample_count ?? 0 }} 笔平仓{{
                  analytics.tradeSkill.sample_count ? '' : '，区间内无平仓'
                }}</template
              ><template v-else>平仓样本尚未加载</template>
            </span>
          </div>
          <div class="analytics-metric">
            <span class="metric-label"
              >盈亏比<HelpTip label="查看盈亏比统计范围"
                >按平仓日落在所选区间内的每笔平仓交易统计，按笔，不是按标的。</HelpTip
              ></span
            >
            <span class="metric-value">{{
              formatNullableNumber(analytics.tradeSkill.payoff_ratio)
            }}</span>
          </div>
          <div class="analytics-metric">
            <span class="metric-label"
              >获利因子<HelpTip label="查看获利因子计算口径"
                >Profit Factor：区间内平仓交易的盈利总额与亏损总额绝对值之比。</HelpTip
              ></span
            >
            <span class="metric-value">{{
              formatNullableNumber(analytics.tradeSkill.profit_factor)
            }}</span>
          </div>
        </div>
      </section>
    </div>
    <div class="range-cash-summary">
      <div class="analytics-metric">
        <span class="metric-label">区间已实现盈亏</span>
        <span
          class="metric-value"
          :style="{ color: getProfitColor(analytics.rangeSummary.realized_pnl_cny) }"
        >
          {{ formatCurrency(analytics.rangeSummary.realized_pnl_cny) }}
        </span>
      </div>
      <div class="analytics-metric">
        <span class="metric-label">区间税后股息</span>
        <span class="metric-value">
          {{ formatCurrency(analytics.rangeSummary.dividend_net_cny) }}
        </span>
      </div>
    </div>
    <DateRangeDialog
      v-if="dateDialogOpened"
      v-model:show="dateDialogVisible"
      title="收益日期范围"
      :date-range="analytics.state.customRange"
      @apply="applyDateRange"
    />
  </section>
</template>

<style scoped>
.analytics-section {
  --analytics-number-primary: 30px;
  --analytics-number-secondary: 22px;
  --analytics-number-detail: 18px;
  --analytics-label-size: 13px;
  container: analytics / inline-size;
  padding: 16px;
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius);
  background: var(--app-surface);
  font-family: var(--app-font-sans);
}
.analytics-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin-bottom: 12px;
}
.analytics-heading > div {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
h2 {
  margin: 0;
  font-size: var(--analytics-number-detail);
  font-weight: 600;
  font-family: var(--app-font-sans);
}
.range-label {
  font-size: var(--analytics-label-size);
  line-height: 1.6;
  color: var(--app-text);
  margin: 12px 0;
}
.range-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 12px;
  margin-bottom: 12px;
}
.range-presets {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.range-presets button {
  font: inherit;
  font-size: var(--analytics-label-size);
  color: var(--app-text-muted);
  border: 0;
  border-radius: var(--app-radius-inner);
  background: transparent;
  padding: 6px 10px;
  min-height: 36px;
  cursor: pointer;
}
.range-presets button:hover {
  background: var(--app-hover);
}
.range-presets button[aria-pressed='true'] {
  background: var(--app-primary-soft);
  color: var(--app-primary-strong);
}
.range-presets button:focus-visible {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: -2px;
}
.benchmark-select {
  --el-text-color-placeholder: var(--app-text-soft);
  flex: 0 1 240px;
  max-width: 100%;
  margin-left: auto;
}
.benchmark-select :deep(.el-select__wrapper) {
  min-height: 36px;
}
.benchmark-empty {
  padding: 10px;
  margin: 0;
  color: var(--app-text-muted);
}
.benchmark-status {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.benchmark-status p {
  color: var(--app-warning-text);
  font-size: var(--analytics-label-size);
  line-height: 1.7;
  margin: 0;
}
.analytics-error {
  margin-bottom: 16px;
}
.curve-figure {
  min-width: 0;
}
.chart-performance {
  height: 440px;
}
.analytics-summary-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px 16px;
  padding: 16px 0;
  margin-top: 8px;
  border-top: 1px solid var(--app-border-soft);
  border-bottom: 1px solid var(--app-border-soft);
}
.primary-metric {
  grid-column: 1 / -1;
}
.analytics-metric {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: 4px;
}
.metric-label {
  display: flex;
  align-items: center;
  gap: 4px;
  min-height: 24px;
  min-width: 0;
  font-size: var(--analytics-label-size);
  color: var(--app-text-muted);
  line-height: 1.5;
}
.benchmark-label > span:first-child {
  flex-shrink: 0;
}
.benchmark-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}
.metric-value {
  font-family: var(--app-font-serif);
  font-size: var(--analytics-number-secondary);
  font-weight: 600;
  font-variant-numeric: lining-nums tabular-nums;
  line-height: 1.4;
  white-space: nowrap;
}
.analytics-summary-grid .metric-value {
  display: flex;
  align-items: baseline;
  min-height: 42px;
}
.analytics-summary-grid .metric-value::before {
  content: '';
  flex: 0 0 0;
  height: 34px;
}
.primary-metric .metric-value {
  font-size: var(--analytics-number-primary);
}
.metric-value.danger {
  color: var(--app-danger-text);
}
.metric-hint {
  font-size: 12px;
  line-height: 1.5;
  color: var(--app-text-muted);
  overflow-wrap: anywhere;
}
.analytics-warnings {
  display: grid;
  gap: 8px;
  margin: 16px 0;
}
.risk-details {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px 24px;
  padding-top: 12px;
}
.metric-group {
  min-width: 0;
}
.metric-group h3 {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: 0 0 4px;
  min-height: 24px;
  color: var(--app-text);
  font: 500 14px var(--app-font-sans);
}
.risk-metrics .analytics-metric,
.range-cash-summary .analytics-metric {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: baseline;
  gap: 4px 12px;
  padding: 6px 0;
  min-height: 40px;
}
.risk-metrics .analytics-metric + .analytics-metric {
  border-top: 1px solid var(--app-border-soft);
}
.risk-metrics .metric-value,
.range-cash-summary .metric-value {
  font-size: var(--analytics-number-detail);
  text-align: right;
}
.range-cash-summary {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 24px;
  border-top: 1px solid var(--app-border-soft);
  margin-top: 8px;
  padding-top: 4px;
}
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}
@container analytics (min-width: 740px) {
  .analytics-summary-grid {
    grid-template-columns: 1.25fr repeat(4, minmax(0, 1fr));
  }
  .primary-metric {
    grid-column: auto;
  }
}
@container analytics (max-width: 559px) {
  .risk-details,
  .range-cash-summary {
    grid-template-columns: minmax(0, 1fr);
  }
  .range-presets {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    width: 100%;
  }
  .range-presets button {
    padding-inline: 4px;
  }
  .benchmark-select {
    flex: 1 1 100%;
  }
  .chart-performance {
    height: 384px;
  }
}
@media (pointer: coarse) {
  .range-presets button,
  .n-button,
  .benchmark-select :deep(.el-select__wrapper),
  .risk-metrics .analytics-metric {
    min-height: 44px;
  }
}
@media (max-width: 640px) {
  .analytics-section {
    padding: 12px;
  }
}
.retry-action {
  display: block;
  margin-top: 12px;
}
</style>
