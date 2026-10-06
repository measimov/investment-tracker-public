<script setup lang="ts">
import { NAlert } from 'naive-ui'
import { useMediaQuery } from '@/composables/useMediaQuery'
import HelpTip from '@/components/HelpTip.vue'
import { formatCurrency, formatNumber, formatPercent, profitColor } from '@/utils/helpers'
import type { HoldingsTableFeature } from './useHoldingsTable'

defineProps<{ table: HoldingsTableFeature }>()
const isMobile = useMediaQuery('(max-width: 640px)')
</script>

<template>
  <section class="summary-section" aria-label="持仓范围汇总" :aria-busy="table.state.loading">
    <div class="portfolio-overview">
      <div class="overview-primary">
        <div class="metric-label">
          已知持仓市值 <span>CNY</span>
          <HelpTip label="查看持仓成本与盈亏口径">
            <p v-if="!table.state.loading && !table.state.loadError" data-testid="holdings-count">
              {{ table.securityCount }} 只持仓 · {{ table.pricedSecurityCount }} 只有价
            </p>
            成本与市值均按今日汇率折人民币，不含汇兑损益。按持仓摊薄平均成本计算：卖出不改变剩余持仓均价。仪表盘的「未实现盈亏」与统计页的「当前持仓表现」按
            FIFO
            剩余批次成本计算，部分卖出过的标的两者会有差异；两种口径下「已实现+未实现」的总收益一致，只是拆分归属不同。
          </HelpTip>
        </div>
        <div class="hero-value" data-testid="holdings-total-value">
          {{
            table.state.loading || table.state.loadError
              ? '—'
              : formatCurrency(table.totalMarketValueCNY)
          }}
        </div>
        <div class="metric-caption">
          {{
            table.state.loading || table.state.loadError
              ? '—'
              : formatCurrency(table.totalMarketValueUSD, 'USD')
          }}
        </div>
        <div
          v-if="table.state.loading || table.state.loadError"
          class="scope-caption"
          data-testid="holdings-count"
        >
          <template v-if="table.state.loading">正在读取持仓…</template>
          <template v-else>加载失败，汇总暂不可用</template>
        </div>
      </div>
      <div class="overview-metric">
        <div class="metric-label">持仓成本 <span>CNY</span></div>
        <div class="metric-value">
          {{
            table.state.loading || table.state.loadError ? '—' : formatCurrency(table.totalCostCNY)
          }}
        </div>
        <div class="metric-caption">
          {{
            table.state.loading || table.state.loadError
              ? '—'
              : formatCurrency(table.totalCostUSD, 'USD')
          }}
        </div>
      </div>
      <div class="overview-metric">
        <div class="metric-label">浮动盈亏 <span>CNY</span></div>
        <div class="metric-value" :style="{ color: profitColor(table.totalProfit) }">
          {{
            table.state.loading || table.state.loadError ? '—' : formatCurrency(table.totalProfit)
          }}
        </div>
        <div class="metric-caption" :style="{ color: profitColor(table.totalProfit) }">
          {{
            table.state.loading || table.state.loadError
              ? '—'
              : formatCurrency(table.totalProfitUSD, 'USD')
          }}
        </div>
      </div>
      <div class="overview-metric">
        <div class="metric-label">浮动盈亏率</div>
        <div class="metric-value" :style="{ color: profitColor(table.totalProfitRate) }">
          {{
            table.state.loading || table.state.loadError
              ? '—'
              : formatPercent(table.totalProfitRate)
          }}
        </div>
      </div>
    </div>
    <div
      v-if="!table.state.loading && !table.state.loadError && table.marketSubtotals.length > 1"
      class="overview-baseline"
    >
      <div
        v-if="
          !isMobile &&
          !table.state.loading &&
          !table.state.loadError &&
          table.marketSubtotals.length > 1
        "
        class="market-subtotals"
        data-testid="market-subtotals"
      >
        <div v-for="item in table.marketSubtotals" :key="item.market" class="market-subtotal">
          <span class="market-name">{{ item.market }}</span>
          <span
            >{{ item.count }} 只<template v-if="item.pricedCount < item.count"
              >（{{ item.pricedCount }} 有价）</template
            ></span
          >
          <span class="num">{{ formatCurrency(item.valueCNY) }}</span>
          <span v-if="item.share !== null" class="num">{{ formatNumber(item.share, 1) }}%</span>
          <span class="num" :style="{ color: profitColor(item.profitCNY) }">{{
            formatCurrency(item.profitCNY)
          }}</span>
        </div>
      </div>
      <details
        v-if="
          isMobile &&
          !table.state.loading &&
          !table.state.loadError &&
          table.marketSubtotals.length > 1
        "
        class="market-breakdown"
      >
        <summary aria-label="展开各市场持仓小计">
          <span v-for="item in table.marketSubtotals" :key="item.market"
            >{{ item.market }}
            <span class="num">{{
              item.share === null ? '—' : `${formatNumber(item.share, 1)}%`
            }}</span></span
          >
        </summary>
        <div class="market-subtotals" data-testid="market-subtotals">
          <div v-for="item in table.marketSubtotals" :key="item.market" class="market-subtotal">
            <span class="market-name">{{ item.market }}</span>
            <span
              >{{ item.count }} 只<template v-if="item.pricedCount < item.count"
                >（{{ item.pricedCount }} 有价）</template
              ></span
            >
            <span class="num">{{ formatCurrency(item.valueCNY) }}</span>
            <span v-if="item.share !== null" class="num">{{ formatNumber(item.share, 1) }}%</span>
            <span class="num" :style="{ color: profitColor(item.profitCNY) }">{{
              formatCurrency(item.profitCNY)
            }}</span>
          </div>
        </div>
      </details>
    </div>
    <NAlert
      v-if="!table.state.loading && !table.state.loadError && table.unpricedCount > 0"
      type="warning"
      :show-icon="false"
      class="summary-alert"
    >
      {{ table.unpricedCount }} 只持仓暂无价格或缺汇率，已同时从总成本与总市值中剔除（口径自洽）。
    </NAlert>
  </section>
</template>

<style scoped>
.summary-section {
  margin: 0 0 24px;
}
.portfolio-overview {
  display: grid;
  grid-template-columns: minmax(290px, 1.35fr) 1fr 1fr 0.85fr;
  gap: 24px;
  align-items: start;
  padding: 10px 0 20px;
}
.overview-primary {
  min-width: 0;
}
.metric-label {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 20px;
  font-size: 13px;
  color: var(--app-text-muted);
}
.metric-label > span {
  font-size: 11px;
  color: var(--app-text-soft);
}
.hero-value {
  font-size: var(--app-number-hero);
  font-weight: 600;
  letter-spacing: -1px;
  line-height: 1.4;
  font-variant-numeric: tabular-nums;
  margin-top: 10px;
  white-space: nowrap;
}
.overview-metric {
  border-left: 1px solid var(--app-border);
  padding-left: 24px;
  min-width: 0;
}
.metric-value {
  font-size: var(--app-number-secondary);
  font-weight: 600;
  letter-spacing: -0.5px;
  margin-top: 16px;
  line-height: 1.4;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.metric-caption,
.scope-caption {
  font-size: 12px;
  color: var(--app-text-soft);
  margin-top: 8px;
  font-variant-numeric: tabular-nums;
}
.overview-baseline {
  padding: 12px 0;
}
.market-subtotals {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 24px;
  font-size: 12px;
  color: var(--app-text-muted);
}
.market-subtotal {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 8px;
}
.market-subtotal > span {
  white-space: nowrap;
}
.market-name {
  font-weight: 500;
  color: var(--app-text);
}
.num {
  font-variant-numeric: tabular-nums;
}
.summary-alert {
  margin-top: 12px;
}
@media (min-width: 1001px) and (max-width: 1200px) {
  .portfolio-overview {
    grid-template-columns: minmax(260px, 1.2fr) repeat(3, minmax(0, 1fr));
    gap: 16px;
  }
  .overview-metric {
    padding-left: 16px;
  }
  .metric-value {
    font-size: 22px;
  }
}
@media (min-width: 1025px) {
  .summary-section {
    margin-bottom: var(--app-space-md);
  }
  .portfolio-overview {
    gap: var(--app-space-md);
    padding: var(--app-space-xs) 0 var(--app-space-sm);
  }
  .overview-metric {
    padding-left: var(--app-space-md);
  }
  .metric-value {
    font-size: var(--app-number-secondary);
    margin-top: var(--app-space-sm);
  }
}
@media (max-width: 1000px) {
  .portfolio-overview {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 16px 24px;
    padding: 8px 0 20px;
  }
  .overview-metric:nth-child(3) {
    border: 0;
    padding-left: 0;
  }
}
.market-breakdown summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  font-size: 12px;
  color: var(--app-text-muted);
  cursor: pointer;
}
.market-breakdown summary::after {
  content: '⌄';
  margin-left: auto;
}
.market-breakdown[open] summary::after {
  content: '⌃';
}
.market-breakdown .market-subtotals {
  margin-top: 12px;
}
@media (max-width: 640px) {
  .summary-section {
    margin-bottom: 16px;
  }
  .portfolio-overview {
    gap: 16px;
    padding: 4px 0 16px;
  }
  .metric-caption,
  .scope-caption {
    margin-top: 6px;
  }
  .overview-baseline {
    padding: 10px 0;
  }
  .overview-primary {
    grid-column: 1 / -1;
  }
  .hero-value {
    font-size: 32px;
  }
  .overview-metric {
    padding-left: 0;
    border: 0;
  }
  .overview-metric:nth-child(3) {
    border-left: 1px solid var(--app-border);
    padding-left: 16px;
  }
  .metric-value {
    font-size: var(--app-number-secondary);
    margin-top: 12px;
  }
  .overview-metric:last-child {
    grid-column: 1 / -1;
    display: flex;
    flex-wrap: wrap;
    gap: 4px 14px;
    align-items: baseline;
  }
  .overview-metric:last-child .metric-value,
  .overview-metric:last-child .metric-caption {
    margin-top: 0;
  }
}
@media (max-width: 360px) {
  .portfolio-overview {
    grid-template-columns: 1fr;
  }
  .overview-metric:nth-child(3) {
    border: 0;
    padding: 0;
  }
  .metric-value {
    font-size: var(--app-number-secondary);
  }
}
</style>
