<script setup lang="ts">
import HelpTip from '@/components/HelpTip.vue'
import { NTag } from 'naive-ui'
import FinancialStatistic from '@/components/FinancialStatistic.vue'
import { formatCurrency, formatPercent, profitColor as getProfitColor } from '@/utils/helpers'
import type { AccountReturn } from './types'

withDefaults(defineProps<{ accountReturn: AccountReturn; showMetrics?: boolean }>(), {
  showMetrics: true
})
</script>

<template>
  <section
    class="equity-return stat-section"
    :class="{ 'details-only': !showMetrics }"
    :aria-label="showMetrics ? '证券持仓收益' : '证券收益明细与计算口径'"
  >
    <header v-if="showMetrics" class="section-heading">
      <h2 id="equity-return-title">证券持仓收益</h2>
      <NTag size="small" :bordered="false">权益仓实收口径</NTag>
    </header>
    <div v-if="showMetrics" class="equity-metrics">
      <div>
        <FinancialStatistic
          title="总收益"
          :value="accountReturn.total_return"
          :formatter="formatCurrency"
          :value-style="{ color: getProfitColor(accountReturn.total_return) }"
        />
      </div>
      <div>
        <FinancialStatistic
          title="总收益率"
          :value="accountReturn.total_return_rate"
          :formatter="formatPercent"
          :value-style="{ color: getProfitColor(accountReturn.total_return) }"
        />
      </div>
      <div>
        <FinancialStatistic
          v-if="accountReturn.annualized_return_rate != null"
          title="权益仓 XIRR"
          :value="accountReturn.annualized_return_rate"
          :formatter="formatPercent"
          :value-style="{
            color: getProfitColor(accountReturn.annualized_return_rate)
          }"
        />
        <div v-else class="empty-statistic">
          <span>权益仓 XIRR</span>
          <strong>—</strong>
        </div>
      </div>
    </div>

    <div class="return-components" aria-label="累计收益组成">
      <div class="stat-item">
        <span
          >净投入本金（权益仓）：
          <HelpTip label="查看证券收益组成与计算口径">
            总收益 = 已实现交易盈亏 + 未实现盈亏 +
            税后股息。权益仓口径：仅统计投入证券的资金，账户闲置现金与外部出入金
            不计入、不稀释收益率，不代表全账户表现。汇率口径：上述金额按最新汇率折算人民币； XIRR
            与期间损益按每笔流水当日汇率折算，两者含汇率变动的方式不同。
            <template v-if="accountReturn.rate_denominator === 'peak_invested_principal_cny'">
              净投入本金已不为正（已大部分或全部卖出），总收益率改以峰值投入为分母。
            </template>
          </HelpTip></span
        >
        <span class="value">{{ formatCurrency(accountReturn.net_invested_principal_cny) }}</span>
      </div>
      <div class="stat-item">
        <span>当前市值：</span>
        <span class="value">{{ formatCurrency(accountReturn.current_market_value_cny) }}</span>
      </div>
      <div class="stat-item">
        <span>已实现交易盈亏：</span>
        <span
          class="value"
          :style="{ color: getProfitColor(accountReturn.realized_trading_pnl_cny) }"
        >
          {{ formatCurrency(accountReturn.realized_trading_pnl_cny) }}
        </span>
      </div>
      <div class="stat-item">
        <span>未实现盈亏：</span>
        <span class="value" :style="{ color: getProfitColor(accountReturn.unrealized_pnl_cny) }">
          {{ formatCurrency(accountReturn.unrealized_pnl_cny) }}
        </span>
      </div>
      <div class="stat-item">
        <span>税后股息：</span>
        <span
          class="value"
          :style="{ color: getProfitColor(accountReturn.net_dividend_income_cny) }"
        >
          {{ formatCurrency(accountReturn.net_dividend_income_cny) }}
        </span>
      </div>
    </div>

    <p
      v-if="accountReturn.rate_denominator === 'peak_invested_principal_cny'"
      class="scope-note"
      data-testid="rate-denominator"
    >
      收益率分母：峰值投入
      {{ formatCurrency(accountReturn.peak_invested_principal_cny) }}，净投入本金已不为正。
    </p>
  </section>
</template>

<style scoped>
.stat-section {
  padding: 24px 0;
  border-top: 1px solid var(--app-border);
  font-family: var(--app-font-sans);
}
.details-only {
  padding: 12px 0 20px;
  border-top: 0;
}
.section-heading {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 20px;
}
h2 {
  font-size: 20px;
  margin: 0;
  font-family: var(--app-font-sans);
}
.equity-metrics {
  display: grid;
  grid-template-columns: 1.35fr 1fr 1fr;
  gap: 24px;
}
.equity-metrics > div:first-child :deep(.financial-statistic-value) {
  font-size: var(--app-number-cumulative);
}
.equity-metrics :deep(.financial-statistic-label) {
  color: var(--app-text);
}
.empty-statistic {
  display: grid;
  gap: 8px;
}
.empty-statistic span {
  color: var(--app-text);
  font-size: 13px;
}
.empty-statistic strong {
  color: var(--app-text);
  font-size: var(--app-number-secondary);
  font-weight: 600;
  line-height: 1.35;
}
.scope-note {
  color: var(--app-text);
  font-size: 13px;
  line-height: 1.7;
  margin: 12px 0 0;
}
.return-components {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  column-gap: 24px;
  margin-top: 20px;
}
.details-only .return-components {
  margin-top: 0;
}
.equity-return .stat-item {
  min-width: 0;
  color: var(--app-text);
}
.equity-return .stat-item .value {
  min-width: 0;
  overflow-wrap: anywhere;
  font-variant-numeric: tabular-nums;
}
.equity-return .stat-note {
  color: var(--app-text);
  font-size: 13px;
  line-height: 1.7;
}
@media (max-width: 640px) {
  .stat-section {
    padding: 20px 0;
  }
  .equity-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 20px;
  }
  .equity-metrics > div:first-child {
    grid-column: span 2;
  }
  .equity-metrics > div:first-child :deep(.financial-statistic-value) {
    font-size: var(--app-number-cumulative);
  }
  .return-components {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (min-width: 1025px) {
  .stat-section {
    padding: 16px 0;
  }
  .details-only {
    padding: 12px 0 16px;
  }
  .section-heading {
    margin-bottom: 12px;
  }
  h2 {
    font-size: 18px;
  }
  .equity-metrics {
    gap: 20px;
  }
  .return-components {
    margin-top: 16px;
  }
  .details-only .return-components {
    margin-top: 0;
  }
}
</style>
