<script setup lang="ts">
import { QuestionFilled } from '@element-plus/icons-vue'
import { formatCurrency, formatPercent, profitColor } from '@/utils/helpers'
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
      :title="`${table.unpricedCount} 只持仓暂无价格，已同时从总成本与总市值中剔除（口径自洽）`"
    />
    <el-row :gutter="20">
      <el-col :xs="24" :sm="12" :lg="6">
        <div class="summary-item summary-cost">
          <div class="summary-label">总成本</div>
          <div class="summary-value">{{ formatCurrency(table.totalCostCNY) }}</div>
          <div class="summary-sub-value">{{ formatCurrency(table.totalCostUSD, 'USD') }}</div>
        </div>
      </el-col>
      <el-col :xs="24" :sm="12" :lg="6">
        <div class="summary-item summary-market">
          <div class="summary-label">总市值</div>
          <div class="summary-value">{{ formatCurrency(table.totalMarketValueCNY) }}</div>
          <div class="summary-sub-value">
            {{ formatCurrency(table.totalMarketValueUSD, 'USD') }}
          </div>
        </div>
      </el-col>
      <el-col :xs="24" :sm="12" :lg="6">
        <div class="summary-item summary-profit">
          <div class="summary-label">
            总盈亏（摊薄成本口径）
            <el-tooltip
              placement="top"
              content="按持仓摊薄平均成本计算：卖出不改变剩余持仓均价。统计分析页的“当前持仓表现”按 FIFO 剩余批次成本计算，部分卖出过的证券两者会有差异；两种口径下“已实现+未实现”的总收益一致，只是拆分归属不同。"
            >
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
      <el-col :xs="24" :sm="12" :lg="6">
        <div class="summary-item summary-rate">
          <div class="summary-label">总收益率</div>
          <div class="summary-value" :style="{ color: profitColor(table.totalProfitRate) }">
            {{ formatPercent(table.totalProfitRate) }}
          </div>
        </div>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.summary-section {
  margin-top: 24px;
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
  min-height: 116px;
  padding: 18px;
  background-color: var(--app-surface-muted);
  border: 1px solid var(--app-border-soft);
  border-left: 3px solid var(--summary-accent);
  border-radius: 8px;
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

@media (max-width: 900px) {
  .summary-section {
    margin-top: 18px;
  }

  .summary-value {
    font-size: 22px;
    overflow-wrap: anywhere;
  }
}
</style>
