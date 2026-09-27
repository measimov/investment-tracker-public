<script setup lang="ts">
import { QuestionFilled } from '@element-plus/icons-vue'
import { formatCurrency, formatNumber, formatPercent, profitColor } from '@/utils/helpers'
import type { HoldingsTableFeature } from './useHoldingsTable'

defineProps<{ table: HoldingsTableFeature }>()
</script>

<template>
  <div class="summary-section">
    <el-alert
      v-if="table.unpricedCount > 0"
      type="info"
      :closable="false"
      show-icon
      class="summary-alert"
      :title="`${table.unpricedCount} 只持仓暂无价格或缺汇率，已同时从总成本与总市值中剔除（口径自洽）`"
    />
    <el-row :gutter="20" class="summary-row">
      <el-col :xs="12" :sm="12" :lg="6">
        <div class="summary-item summary-cost">
          <div class="summary-label">总成本</div>
          <div class="summary-value">{{ formatCurrency(table.totalCostCNY) }}</div>
          <div class="summary-sub-value">{{ formatCurrency(table.totalCostUSD, 'USD') }}</div>
        </div>
      </el-col>
      <el-col :xs="12" :sm="12" :lg="6">
        <div class="summary-item summary-market">
          <div class="summary-label">总市值</div>
          <div class="summary-value">{{ formatCurrency(table.totalMarketValueCNY) }}</div>
          <div class="summary-sub-value">
            {{ formatCurrency(table.totalMarketValueUSD, 'USD') }}
          </div>
        </div>
      </el-col>
      <el-col :xs="12" :sm="12" :lg="6">
        <div class="summary-item summary-profit">
          <div class="summary-label">
            浮动盈亏
            <el-tooltip placement="top">
              <template #content>
                <div class="tooltip-body">
                  成本与市值均按今日汇率折人民币，不含汇兑损益（与仪表盘按交易日汇率的口径可能不一致）。<br />
                  按持仓摊薄平均成本计算：卖出不改变剩余持仓均价。统计分析页的“当前持仓表现”按 FIFO
                  剩余批次成本计算，部分卖出过的证券两者会有差异；两种口径下“已实现+未实现”的总收益一致，只是拆分归属不同。
                </div>
              </template>
              <el-icon class="label-help"><QuestionFilled /></el-icon>
            </el-tooltip>
          </div>
          <div class="summary-value" :style="{ color: profitColor(table.totalProfit) }">
            {{ formatCurrency(table.totalProfit) }}
          </div>
          <div class="summary-sub-value" :style="{ color: profitColor(table.totalProfit) }">
            {{ formatCurrency(table.totalProfitUSD, 'USD') }}
          </div>
        </div>
      </el-col>
      <el-col :xs="12" :sm="12" :lg="6">
        <div class="summary-item summary-rate">
          <div class="summary-label">浮动收益率</div>
          <div class="summary-value" :style="{ color: profitColor(table.totalProfitRate) }">
            {{ formatPercent(table.totalProfitRate) }}
          </div>
          <div class="summary-sub-value" data-testid="holdings-count">
            {{ table.securityCount }} 只持仓 · {{ table.pricedSecurityCount }} 只有价
          </div>
        </div>
      </el-col>
    </el-row>
    <!-- 分市场小计：只数 / 市值折人民币 / 占比 / 浮动盈亏（口径同上方汇总卡） -->
    <div
      v-if="table.marketSubtotals.length > 1"
      class="market-subtotals"
      data-testid="market-subtotals"
    >
      <div v-for="item in table.marketSubtotals" :key="item.market" class="market-subtotal">
        <span class="market-name">{{ item.market }}</span>
        <span class="market-count"
          >{{ item.count }} 只<template v-if="item.pricedCount < item.count"
            >（{{ item.pricedCount }} 有价）</template
          ></span
        >
        <span class="num">{{ formatCurrency(item.valueCNY) }}</span>
        <span v-if="item.share !== null" class="num market-share"
          >{{ formatNumber(item.share, 1) }}%</span
        >
        <span class="num" :style="{ color: profitColor(item.profitCNY) }">{{
          formatCurrency(item.profitCNY)
        }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.summary-section {
  margin-bottom: 16px;
}

.summary-alert {
  margin-bottom: 12px;
}

.label-help {
  margin-left: 4px;
  color: var(--app-text-soft);
  cursor: help;
  vertical-align: -2px;
}

.summary-item {
  min-height: 0;
  padding: 12px 16px;
  background-color: var(--app-surface-muted);
  border: 1px solid var(--app-border-soft);
  border-left: 3px solid var(--summary-accent);
  border-radius: var(--app-radius-sm);
}

.summary-cost {
  --summary-accent: var(--app-primary);
}

.summary-market {
  --summary-accent: var(--app-info);
}

.summary-profit {
  --summary-accent: var(--app-success);
}

.summary-rate {
  --summary-accent: var(--app-warning);
}

.summary-label {
  font-size: 13px;
  color: var(--app-text-muted);
  margin-bottom: 8px;
}

.summary-value {
  font-size: 22px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: var(--app-text);
  line-height: 1.2;
}

.summary-sub-value {
  font-size: 14px;
  color: var(--app-text-muted);
  margin-top: 4px;
}

.summary-row > .el-col {
  display: flex;
}

.summary-row .summary-item {
  flex: 1;
}

.tooltip-body {
  max-width: 360px;
  line-height: 1.5;
}

.market-subtotals {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 20px;
  margin-top: 10px;
  font-size: 13px;
  color: var(--app-text-muted);
}

.market-subtotal {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0 8px;
  min-width: 0;
}

.market-subtotal > span {
  white-space: nowrap;
}

.market-name {
  font-weight: 600;
  color: var(--app-text);
}

.market-share {
  color: var(--app-text-soft);
}

.num {
  font-variant-numeric: tabular-nums;
}

@media (max-width: 900px) {
  .summary-section {
    margin-bottom: 12px;
  }

  .summary-item {
    margin-bottom: 8px;
    padding: 10px 12px;
  }

  .summary-sub-value {
    font-size: 12px;
  }

  .summary-value {
    font-size: 22px;
    overflow-wrap: anywhere;
  }
}
</style>
