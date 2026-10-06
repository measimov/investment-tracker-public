<script setup lang="ts">
import { computed } from 'vue'
import { NAlert, NButton, NEmpty, NTable } from 'naive-ui'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart } from 'echarts/charts'
import {
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  GridComponent
} from 'echarts/components'
import VChart from 'vue-echarts'
import { useChartFontOption } from '@/composables/useChartFontOption'
import { EMPTY, formatCurrency, formatPrice, formatQuantity } from '@/utils/helpers'
import type { DistributionStatsFeature } from './useDistributionStats'
import { formatPlainPercent } from './format'
use([CanvasRenderer, BarChart, TitleComponent, TooltipComponent, LegendComponent, GridComponent])
const props = defineProps<{ dist: DistributionStatsFeature }>()
const timeChartOption = useChartFontOption(() => props.dist.timeChartOption)
function selectTimeGroup(value: string) {
  props.dist.state.timeGroupBy = value
  props.dist.loadTimeStats()
}
function share(value: number | null | undefined, total: number): string {
  if (value === null || value === undefined || !total) return EMPTY
  return formatPlainPercent((value / total) * 100, 1)
}
function accountLabel(count: number | undefined): string {
  return count && count > 1 ? `${count} 个` : '1'
}
const timeDescription = computed(
  () =>
    `${props.dist.state.timeResultGroupBy === 'month' ? '按月' : '按年'}交易金额趋势，共 ${props.dist.state.timeStats.length} 期，金额单位为人民币。买入与卖出金额可通过图表提示或展开明细查看。`
)
</script>
<template>
  <section class="distribution-section" aria-label="成本分布与交易趋势">
    <section aria-labelledby="market-details-title">
      <h2 id="market-details-title">市场详细数据 <small>按人民币成本</small></h2>
      <NAlert v-if="dist.state.marketStatus.error" type="warning" data-testid="market-stats-error"
        >市场统计加载失败：{{ dist.state.marketStatus.error }}；{{
          dist.state.marketStatus.loaded ? '保留上次成功数据' : '当前分布未知'
        }}<span class="retry-action"
          ><NButton size="small" @click="dist.loadAll">重试分布统计</NButton></span
        ></NAlert
      >
      <p v-else-if="dist.state.marketStatus.loading" class="status-note" role="status">
        正在加载市场分布…
      </p>
      <NEmpty
        v-if="!dist.state.marketStats.length"
        :description="
          dist.state.marketStatus.error
            ? '市场分布暂不可用'
            : dist.state.marketStatus.loaded
              ? '暂无市场分布数据'
              : '市场分布尚未加载'
        "
      />
      <p
        v-if="dist.state.marketStats.some((row) => row.missing_rate_currencies?.length)"
        class="status-note"
      >
        缺少汇率的金额未计入，总成本与占比只覆盖可折算部分。
      </p>
      <div
        v-if="dist.state.marketStats.length"
        class="table-scroll"
        role="region"
        aria-label="市场详细数据表，可横向滚动"
        tabindex="0"
      >
        <NTable :bordered="false" :single-line="true" class="market-table"
          ><thead>
            <tr>
              <th>市场</th>
              <th class="numeric">持仓数</th>
              <th class="numeric">总成本</th>
              <th class="numeric">占比</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in dist.state.marketStats" :key="row.market">
              <th scope="row">{{ row.market }}</th>
              <td class="numeric">{{ row.holdings_count }}</td>
              <td class="numeric">{{ formatCurrency(row.total_cost) }}</td>
              <td class="numeric">{{ share(row.total_cost, dist.totalInvested) }}</td>
            </tr>
          </tbody></NTable
        >
      </div>
    </section>
    <section class="time-section" aria-labelledby="time-trend-title">
      <header class="section-header">
        <h2 id="time-trend-title">交易时间趋势</h2>
        <div class="time-options" role="group" aria-label="交易趋势时间粒度">
          <button
            v-for="option in [
              { value: 'month', label: '按月' },
              { value: 'year', label: '按年' }
            ]"
            :key="option.value"
            type="button"
            :aria-pressed="dist.state.timeGroupBy === option.value"
            @click="selectTimeGroup(option.value)"
          >
            {{ option.label }}
          </button>
        </div>
      </header>
      <NAlert v-if="dist.state.timeStatus.error" type="warning" data-testid="time-stats-error"
        >时间统计加载失败：{{ dist.state.timeStatus.error }}；{{
          dist.state.timeStatus.loaded
            ? '保留上次成功数据，当前时间粒度尚未确认'
            : '当前交易趋势未知'
        }}<span class="retry-action"
          ><NButton size="small" @click="dist.loadTimeStats">重试时间统计</NButton></span
        ></NAlert
      >
      <p v-else-if="dist.state.timeStatus.loading" class="status-note" role="status">
        正在加载时间统计，尚未确认当前时间粒度…
      </p>
      <NEmpty
        v-if="!dist.state.timeStats.length"
        :description="
          dist.state.timeStatus.error
            ? '交易趋势暂不可用'
            : dist.state.timeStatus.loaded
              ? '暂无交易时间趋势'
              : '交易趋势尚未加载'
        "
      />
      <div v-else>
        <div role="img" :aria-label="timeDescription">
          <VChart
            :update-options="{ notMerge: false, replaceMerge: ['series'] }"
            :option="timeChartOption"
            class="time-chart"
            autoresize
          />
        </div>
        <p class="chart-description">{{ timeDescription }}</p>
        <details class="time-details" data-testid="time-trend-details">
          <summary>查看交易金额明细</summary>
          <div
            class="table-scroll"
            role="region"
            aria-label="交易金额明细表，可横向滚动"
            tabindex="0"
          >
            <NTable :bordered="false" :single-line="true" class="time-table">
              <caption>
                交易金额（人民币）
              </caption>
              <thead>
                <tr>
                  <th scope="col">期间</th>
                  <th scope="col" class="numeric">买入金额</th>
                  <th scope="col" class="numeric">卖出金额</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in dist.state.timeStats" :key="row.period">
                  <th scope="row">{{ row.period }}</th>
                  <td class="numeric">{{ formatCurrency(row.buy_amount) }}</td>
                  <td class="numeric">{{ formatCurrency(row.sell_amount) }}</td>
                </tr>
              </tbody>
            </NTable>
          </div>
        </details>
      </div>
    </section>
    <section class="holdings-section" aria-labelledby="holdings-ranking-title">
      <h2 id="holdings-ranking-title">持仓排行 <small>按人民币成本</small></h2>
      <NAlert
        v-if="dist.state.profitLossStatus.error"
        type="warning"
        data-testid="holdings-cost-error"
        >持仓成本加载失败：{{ dist.state.profitLossStatus.error }}；{{
          dist.state.profitLossStatus.loaded ? '保留上次成功数据' : '当前排行未知'
        }}<span class="retry-action"
          ><NButton size="small" @click="dist.loadAll">重试持仓成本</NButton></span
        ></NAlert
      >
      <p v-else-if="dist.state.profitLossStatus.loading" class="status-note" role="status">
        正在加载持仓成本…
      </p>
      <div class="table-scroll" role="region" aria-label="持仓成本排行表，可横向滚动" tabindex="0">
        <NTable :bordered="false" :single-line="true" class="holdings-table"
          ><thead>
            <tr>
              <th>排名</th>
              <th>标的</th>
              <th>市场</th>
              <th class="numeric">账户</th>
              <th class="numeric">数量</th>
              <th class="numeric">平均成本</th>
              <th class="numeric">总成本</th>
              <th class="numeric">占比</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(row, index) in dist.state.profitLossData"
              :key="`${row.market}:${row.symbol}`"
            >
              <td>{{ index + 1 }}</td>
              <th scope="row">
                <strong class="symbol">{{ row.symbol }}</strong
                ><span class="security-name">{{ row.name }}</span>
              </th>
              <td>{{ row.market }}</td>
              <td class="numeric">{{ accountLabel(row.account_count) }}</td>
              <td class="numeric">{{ formatQuantity(row.quantity) }}</td>
              <td class="numeric">
                {{ formatPrice(row.avg_cost) }}<span class="currency-code">{{ row.currency }}</span>
              </td>
              <td class="numeric">
                <strong>{{ formatCurrency(row.total_cost, row.currency) }}</strong
                ><span v-if="row.currency !== 'CNY'" class="cny-line">{{
                  row.total_cost_cny != null
                    ? `≈ ${formatCurrency(row.total_cost_cny)}`
                    : `缺 ${row.currency} 汇率`
                }}</span>
              </td>
              <td class="numeric">{{ share(row.total_cost_cny, dist.totalInvestedCNY) }}</td>
            </tr>
          </tbody></NTable
        >
        <p v-if="!dist.state.profitLossData.length" class="status-note">
          {{
            dist.state.profitLossStatus.error
              ? '持仓排行暂不可用'
              : dist.state.profitLossStatus.loaded
                ? '暂无持仓排行数据'
                : '持仓排行尚未加载'
          }}
        </p>
      </div>
    </section>
  </section>
</template>
<style scoped>
.distribution-section {
  border-top: 1px solid var(--app-border);
  padding: 28px 0;
  font-family: var(--app-font-sans);
}
h2 {
  font-size: 20px;
  font-weight: 500;
  margin: 0 0 22px;
  font-family: var(--app-font-sans);
}
h2 small {
  font-size: 13px;
  color: var(--app-text);
  margin-left: 8px;
}
.table-scroll {
  overflow-x: auto;
  max-width: 100%;
}
/* 行标题保留 th 语义，底色与同一行的数据单元格一致。 */
.table-scroll :deep(tbody th) {
  background: var(--n-td-color);
  color: var(--n-td-text-color);
  font-weight: 400;
  white-space: normal;
}
.time-details summary {
  cursor: pointer;
  min-height: 36px;
  align-content: center;
  color: var(--app-text);
}
.time-details summary:focus-visible {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: 3px;
}
.time-table {
  min-width: 360px;
  margin-top: 12px;
}
.time-table caption {
  text-align: left;
  padding: 8px 0;
  color: var(--app-text-muted);
  font-size: 12px;
}
.market-table {
  min-width: 380px;
}
.holdings-table {
  min-width: 940px;
}
.numeric {
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.holdings-table th[scope='row'] {
  min-width: 160px;
  max-width: 240px;
  font-weight: 400;
  overflow-wrap: anywhere;
}
.symbol,
.security-name {
  display: block;
}
.security-name {
  font-size: 12px;
  color: var(--app-text-muted);
  margin-top: 4px;
}
.currency-code {
  font-size: 12px;
  color: var(--app-text-muted);
  margin-left: 4px;
}
.cny-line {
  display: block;
  font-size: 13px;
  color: var(--app-text);
  margin-top: 4px;
}
.status-note,
.chart-description {
  color: var(--app-text);
  font-size: 13px;
  line-height: 1.8;
  margin: 12px 0;
}
.chart-description {
  overflow-wrap: anywhere;
}
.time-chart {
  height: 320px;
}
.time-section,
.holdings-section {
  margin-top: 32px;
  padding-top: 28px;
  border-top: 1px solid var(--app-border-soft);
}
.section-header {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 16px;
  align-items: center;
}
.time-options {
  display: flex;
  gap: 4px;
}
.time-options button {
  font: inherit;
  font-size: 13px;
  min-height: 36px;
  padding: 6px 12px;
  cursor: pointer;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--app-text-muted);
}
.time-options button[aria-pressed='true'] {
  color: var(--app-primary-strong);
  background: var(--app-surface-secondary);
}
@media (max-width: 640px) {
  .time-details summary,
  .time-options button,
  .n-button {
    min-height: 44px;
  }
  .time-chart {
    height: 280px;
  }
}
.retry-action {
  display: block;
  margin-top: 12px;
}
</style>
