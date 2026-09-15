<script setup lang="ts">
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
import { QuestionFilled, Refresh } from '@element-plus/icons-vue'
import { formatCurrency, profitColor as getProfitColor } from '@/utils/helpers'
import { RANGE_PRESETS, type AnalyticsFeature } from './useAnalytics'
import { formatNullableNumber, formatNullablePercent } from './types'

use([
  CanvasRenderer,
  LineChart,
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  GridComponent,
  DataZoomComponent
])

defineProps<{ analytics: AnalyticsFeature }>()
</script>

<template>
  <el-row :gutter="20" class="performance-cards">
    <el-col :span="24">
      <el-card class="stat-card" shadow="hover" v-loading="analytics.state.loading">
        <template #header>
          <div class="chart-header">
            <div class="title-with-tag">
              <span>证券组合 TTWR 与风险指标</span>
              <el-tag type="warning" effect="plain" size="small">实验指标</el-tag>
            </div>
            <el-button
              type="primary"
              size="small"
              :icon="Refresh"
              :loading="analytics.state.historyRefreshing"
              @click="analytics.refreshHistoryAndReload"
            >
              同步历史行情
            </el-button>
          </div>
        </template>

        <el-alert
          title="TTWR 与风险指标为实验指标，基于证券交易现金流估算，未包含账户现金和真实外部入出金；胜率/盈亏比按平仓日落在所选区间内的每笔平仓交易统计。"
          type="warning"
          :closable="false"
          show-icon
          class="methodology-alert"
        />

        <div class="range-toolbar">
          <el-radio-group v-model="analytics.state.rangePreset" size="small">
            <el-radio-button
              v-for="preset in RANGE_PRESETS"
              :key="preset.value"
              :value="preset.value"
            >
              {{ preset.label }}
            </el-radio-button>
          </el-radio-group>
          <el-date-picker
            v-if="analytics.state.rangePreset === 'custom'"
            v-model="analytics.state.customRange"
            type="daterange"
            size="small"
            value-format="YYYY-MM-DD"
            range-separator="~"
            start-placeholder="开始日期"
            end-placeholder="结束日期"
            class="range-picker"
          />
          <span v-if="analytics.effectiveRangeLabel" class="range-label">
            {{ analytics.effectiveRangeLabel }}
          </span>
          <el-select
            v-model="analytics.state.selectedBenchmarks"
            multiple
            collapse-tags
            :multiple-limit="3"
            size="small"
            placeholder="对比基准"
            class="benchmark-select"
            data-testid="benchmark-select"
          >
            <el-option
              v-for="option in analytics.state.benchmarkOptions"
              :key="option.code"
              :label="option.name"
              :value="option.code"
            />
            <template #empty>
              <p class="benchmark-empty">
                基准目录加载失败：后端版本过旧或请求失败，请重启后端后刷新页面
              </p>
            </template>
          </el-select>
          <el-tag
            v-for="block in analytics.unavailableBenchmarks"
            :key="block.code"
            type="info"
            size="small"
            effect="plain"
          >
            {{ block.name }} 无基准数据
          </el-tag>
          <el-tooltip
            v-for="block in analytics.partialBenchmarks"
            :key="`partial-${block.code}`"
            content="基准数据晚于区间起点，计量区间不一致，不计算超额收益；可同步更早历史行情补齐"
          >
            <el-tag type="warning" size="small" effect="plain">
              {{ block.name }} 起点晚于区间
            </el-tag>
          </el-tooltip>
        </div>

        <div class="analytics-summary-grid">
          <div class="analytics-metric">
            <span class="metric-label">年化收益率（TTWR估算）</span>
            <span
              class="metric-value"
              :style="{ color: getProfitColor(analytics.metrics.annualized_return_rate || 0) }"
            >
              {{ formatNullablePercent(analytics.metrics.annualized_return_rate) }}
            </span>
          </div>
          <div class="analytics-metric">
            <span class="metric-label">夏普率</span>
            <span class="metric-value">{{
              formatNullableNumber(analytics.metrics.sharpe_ratio)
            }}</span>
          </div>
          <div class="analytics-metric">
            <span class="metric-label">索提诺率</span>
            <span class="metric-value">{{
              formatNullableNumber(analytics.metrics.sortino_ratio)
            }}</span>
          </div>
          <div class="analytics-metric">
            <span class="metric-label">最大回撤</span>
            <span class="metric-value danger">
              {{ formatNullablePercent(analytics.metrics.max_drawdown_rate) }}
            </span>
          </div>
          <div class="analytics-metric">
            <span class="metric-label">卡玛率</span>
            <span class="metric-value">{{
              formatNullableNumber(analytics.metrics.calmar_ratio)
            }}</span>
          </div>
          <div v-if="analytics.primaryBenchmarkComparison" class="analytics-metric">
            <span class="metric-label">
              区间超额收益（vs {{ analytics.primaryBenchmarkComparison.name }}）
              <el-tooltip
                content="组合 TTWR 与基准累计收益的算术差（百分点）；基准为价格指数、不含股息、原币口径"
              >
                <el-icon class="label-help"><QuestionFilled /></el-icon>
              </el-tooltip>
            </span>
            <span
              class="metric-value"
              :style="{
                color: getProfitColor(analytics.primaryBenchmarkComparison.excess_return_rate)
              }"
            >
              {{ formatNullablePercent(analytics.primaryBenchmarkComparison.excess_return_rate) }}
            </span>
          </div>
          <div class="analytics-metric">
            <span class="metric-label">标的胜率（实验）</span>
            <span class="metric-value">{{
              formatNullablePercent(analytics.tradeSkill.win_rate)
            }}</span>
          </div>
          <div class="analytics-metric">
            <span class="metric-label">盈亏比</span>
            <span class="metric-value">{{
              formatNullableNumber(analytics.tradeSkill.payoff_ratio)
            }}</span>
          </div>
          <div class="analytics-metric">
            <span class="metric-label">Profit Factor</span>
            <span class="metric-value">{{
              formatNullableNumber(analytics.tradeSkill.profit_factor)
            }}</span>
          </div>
          <div class="analytics-metric">
            <span class="metric-label">区间已实现盈亏</span>
            <span
              class="metric-value"
              :style="{ color: getProfitColor(analytics.rangeSummary.realized_pnl_cny || 0) }"
            >
              {{ formatCurrency(analytics.rangeSummary.realized_pnl_cny || 0) }}
            </span>
          </div>
          <div class="analytics-metric">
            <span class="metric-label">区间税后股息</span>
            <span class="metric-value">
              {{ formatCurrency(analytics.rangeSummary.dividend_net_cny || 0) }}
            </span>
          </div>
          <div class="analytics-metric">
            <span class="metric-label">区间年化（XIRR·资金加权）</span>
            <span
              class="metric-value"
              :style="{ color: getProfitColor(analytics.rangeSummary.xirr_annualized_rate || 0) }"
            >
              {{ formatNullablePercent(analytics.rangeSummary.xirr_annualized_rate) }}
            </span>
          </div>
        </div>

        <div v-if="analytics.warnings.length" class="analytics-warnings">
          <el-tag
            v-for="warning in analytics.warnings"
            :key="warning"
            type="warning"
            effect="plain"
          >
            {{ warning }}
          </el-tag>
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

        <el-empty
          v-if="analytics.curve.length === 0"
          description="暂无收益率曲线数据"
          :image-size="88"
        />
        <v-chart
          v-else
          :option="analytics.chartOption"
          class="chart chart-performance"
          autoresize
        />
      </el-card>
    </el-col>
  </el-row>
</template>

<style scoped>
.range-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
}

.range-label {
  color: var(--app-text-soft);
  font-size: 12px;
}

.range-picker {
  max-width: 260px;
}

.benchmark-select {
  min-width: 180px;
  max-width: 260px;
}

.benchmark-empty {
  padding: 10px 12px;
  margin: 0;
  font-size: 12px;
  color: var(--app-text-soft);
}

.label-help {
  margin-left: 4px;
  color: var(--app-text-soft);
  cursor: help;
  vertical-align: -2px;
}

.chart-performance {
  height: 420px;
}

.analytics-summary-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 20px;
}

.analytics-metric {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-height: 72px;
  padding: 14px;
  background:
    linear-gradient(150deg, var(--app-primary-soft), transparent 60%), var(--app-surface-muted);
  border: 1px solid var(--app-border-soft);
  border-radius: var(--app-radius-inner);
  transition:
    transform 0.18s ease,
    box-shadow 0.18s ease;
}

.analytics-metric:hover {
  transform: translateY(-2px);
  box-shadow: var(--app-shadow-sm);
}

.metric-label {
  color: var(--app-text-muted);
  font-size: 13px;
  font-weight: 500;
}

.metric-value {
  color: var(--app-text);
  font-size: 22px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}

.metric-value.danger {
  color: var(--app-danger);
}

.analytics-warnings {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

@media (max-width: 900px) {
  .chart-performance {
    height: 340px;
  }

  .analytics-summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .metric-value {
    font-size: 18px;
  }
}

@media (max-width: 640px) {
  .analytics-summary-grid {
    grid-template-columns: 1fr;
  }
}
</style>
