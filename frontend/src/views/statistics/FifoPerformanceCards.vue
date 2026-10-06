<script setup lang="ts">
import HelpTip from '@/components/HelpTip.vue'
import FinancialStatistic from '@/components/FinancialStatistic.vue'
import { formatCurrency, formatPercent, profitColor as getProfitColor } from '@/utils/helpers'
import type { CurrentPerformance, RealizedPnL, TotalRealizedReturn } from './types'

defineProps<{
  currentPerformance: CurrentPerformance
  realizedPnL: RealizedPnL
  totalRealizedReturn: TotalRealizedReturn
}>()
</script>

<template>
  <section class="fifo-grid" aria-label="FIFO 持仓与平仓表现">
    <!-- 卡片1：当前持仓表现 -->
    <article class="fifo-section">
      <header>
        <div class="card-header">
          <h2>
            当前持仓表现
            <HelpTip label="查看当前持仓 FIFO 口径"
              >按 FIFO 剩余批次成本计算，与持仓页的平均成本口径可能不同。</HelpTip
            >
          </h2>
        </div>
      </header>

      <div class="fifo-metrics">
        <div>
          <FinancialStatistic
            title="未实现盈亏"
            :value="currentPerformance.unrealized_pnl_cny"
            :formatter="formatCurrency"
            :value-style="{ color: getProfitColor(currentPerformance.unrealized_pnl_cny) }"
          />
        </div>
        <div>
          <FinancialStatistic
            title="未实现盈亏率"
            :value="currentPerformance.unrealized_pnl_rate"
            :formatter="formatPercent"
            :value-style="{ color: getProfitColor(currentPerformance.unrealized_pnl_cny) }"
          />
        </div>
      </div>

      <div class="stat-detail">
        <div class="stat-item">
          <span>当前持仓成本（FIFO）：</span>
          <!-- 只显示 FIFO 口径本身：为 0 时不再回退到平均成本口径的总投入（#218），
                 否则同一行混了两种成本口径 -->
          <span class="value">{{
            formatCurrency(currentPerformance.current_holdings_cost_cny)
          }}</span>
        </div>
        <div class="stat-item">
          <span>当前市值：</span>
          <span class="value">
            {{ formatCurrency(currentPerformance.current_market_value_cny) }}
            <span
              class="price-warning"
              v-if="currentPerformance.data_quality?.unpriced_position_count"
            >
              （{{ currentPerformance.data_quality.unpriced_position_count }}
              只持仓缺价，成本与市值均未计入）
            </span>
          </span>
        </div>
      </div>
    </article>

    <!-- 卡片2：历史交易能力 -->
    <article class="fifo-section">
      <header>
        <div class="card-header">
          <h2>
            已平仓交易能力
            <HelpTip label="查看已平仓 FIFO 口径">
              已实现收益率只评价已卖出的交易；分母为被卖出部分的 FIFO 成本。已实现收益（含股息） =
              已实现盈亏 + 税后股息。
            </HelpTip>
          </h2>
        </div>
      </header>

      <div class="fifo-metrics">
        <div>
          <FinancialStatistic
            title="已实现盈亏"
            :value="realizedPnL.realized_pnl"
            :formatter="formatCurrency"
            :value-style="{ color: getProfitColor(realizedPnL.realized_pnl) }"
          />
        </div>
        <div>
          <FinancialStatistic
            title="已实现收益率"
            :value="realizedPnL.realized_pnl_rate"
            :formatter="formatPercent"
            :value-style="{ color: getProfitColor(realizedPnL.realized_pnl) }"
          />
        </div>
      </div>

      <div class="stat-detail">
        <div class="stat-item">
          <span>已实现收益（含股息）：</span>
          <span
            class="value"
            :style="{ color: getProfitColor(totalRealizedReturn.total_realized_return) }"
          >
            {{ formatCurrency(totalRealizedReturn.total_realized_return) }}
          </span>
        </div>
        <div class="stat-item">
          <span>已实现收益率（含股息）：</span>
          <span
            class="value"
            :style="{ color: getProfitColor(totalRealizedReturn.total_realized_return) }"
          >
            {{ formatPercent(totalRealizedReturn.total_realized_return_rate) }}
          </span>
        </div>
        <div class="stat-item">
          <span>已卖出FIFO成本：</span>
          <span class="value">{{ formatCurrency(realizedPnL.sold_cost) }}</span>
        </div>
        <div class="stat-item">
          <span>税后股息：</span>
          <span
            class="value"
            :style="{ color: getProfitColor(totalRealizedReturn.net_dividend_income_cny) }"
          >
            {{ formatCurrency(totalRealizedReturn.net_dividend_income_cny) }}
          </span>
        </div>
      </div>
    </article>
  </section>
</template>

<style scoped>
.fifo-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 24px;
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius);
  padding: 16px;
  background: var(--app-surface);
  font-family: var(--app-font-sans);
}
.fifo-section {
  min-width: 0;
}
.card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 16px;
  margin-bottom: 12px;
}
h2 {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: 0;
  font-size: 18px;
  font-weight: 500;
  font-family: var(--app-font-sans);
}
.fifo-metrics {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 12px;
}
.fifo-metrics :deep(.financial-statistic) {
  gap: 4px;
}
.fifo-grid .stat-detail {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 16px;
  border-top: 1px solid var(--app-border-soft);
  padding-top: 10px;
}
.fifo-grid .stat-item {
  flex-direction: column;
  align-items: flex-start;
  justify-content: flex-start;
  gap: 4px;
  padding: 0;
  border: 0;
  font-size: 13px;
  min-width: 0;
}
.fifo-grid .stat-item .value {
  text-align: left;
  overflow-wrap: anywhere;
}
.price-warning {
  color: var(--app-warning-text);
  font-size: 13px;
}
.fifo-grid :deep(.financial-statistic-label),
.fifo-grid .stat-item {
  color: var(--app-text);
}
.fifo-grid .stat-note {
  font-size: 13px;
  color: var(--app-text);
  line-height: 1.7;
}
@media (max-width: 760px) {
  .fifo-grid {
    grid-template-columns: 1fr;
    gap: 20px;
  }
}
@media (max-width: 640px) {
  .fifo-metrics {
    gap: 16px;
  }
}
</style>
