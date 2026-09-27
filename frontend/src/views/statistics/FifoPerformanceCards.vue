<script setup lang="ts">
import { Refresh } from '@element-plus/icons-vue'
import { COLOR } from '@/styles/tokens'
import { formatCurrency, formatPercent, profitColor as getProfitColor } from '@/utils/helpers'
import type { CurrentPerformance, RealizedPnL, TotalRealizedReturn } from './types'
import { signedNumber } from './format'

defineProps<{
  currentPerformance: CurrentPerformance
  realizedPnL: RealizedPnL
  totalRealizedReturn: TotalRealizedReturn
  refreshing: boolean
}>()

const signed = (value: number) => signedNumber(value)

defineEmits<{ 'refresh-prices': []; 'open-price-dialog': [] }>()
</script>

<template>
  <el-row :gutter="20" class="performance-cards">
    <!-- 卡片1：当前持仓表现 -->
    <el-col :xs="24" :md="12">
      <el-card class="stat-card" shadow="hover">
        <template #header>
          <div class="card-header">
            <span>当前持仓表现（FIFO分析口径）</span>
            <div>
              <el-button
                type="success"
                size="small"
                :icon="Refresh"
                @click="$emit('refresh-prices')"
                :loading="refreshing"
                class="refresh-button"
              >
                一键刷新股价
              </el-button>
              <el-button type="primary" size="small" @click="$emit('open-price-dialog')">
                输入价格
              </el-button>
            </div>
          </div>
        </template>

        <el-row :gutter="20">
          <el-col :xs="24" :sm="12">
            <el-statistic
              title="未实现盈亏"
              :value="currentPerformance.unrealized_pnl"
              :precision="2"
              prefix="¥"
              :value-style="{ color: getProfitColor(currentPerformance.unrealized_pnl) }"
            />
          </el-col>
          <el-col :xs="24" :sm="12">
            <el-statistic
              title="浮盈率"
              :value="currentPerformance.unrealized_pnl_rate"
              :formatter="signed"
              suffix="%"
              :value-style="{ color: getProfitColor(currentPerformance.unrealized_pnl) }"
            />
          </el-col>
        </el-row>

        <el-divider />

        <div class="stat-detail">
          <div class="stat-item">
            <span>当前持仓成本（FIFO）：</span>
            <!-- 只显示 FIFO 口径本身：为 0 时不再回退到平均成本口径的总投入（#218），
                 否则同一行混了两种成本口径 -->
            <span class="value">{{
              formatCurrency(currentPerformance.current_holdings_cost)
            }}</span>
          </div>
          <div class="stat-item">
            <span>当前市值：</span>
            <span class="value">
              {{ formatCurrency(currentPerformance.current_market_value) }}
              <el-text
                v-if="currentPerformance.data_quality?.unpriced_position_count"
                type="warning"
                size="small"
              >
                （{{ currentPerformance.data_quality.unpriced_position_count }}
                只持仓缺价，成本与市值均未计入）
              </el-text>
            </span>
          </div>
        </div>
      </el-card>
    </el-col>

    <!-- 卡片2：历史交易能力 -->
    <el-col :xs="24" :md="12">
      <el-card class="stat-card" shadow="hover">
        <template #header>
          <div class="card-header">
            <span>已平仓交易能力 (FIFO)</span>
          </div>
        </template>

        <el-row :gutter="20">
          <el-col :xs="24" :sm="12">
            <el-statistic
              title="已实现盈亏"
              :value="realizedPnL.realized_pnl"
              :precision="2"
              prefix="¥"
              :value-style="{ color: getProfitColor(realizedPnL.realized_pnl) }"
            />
          </el-col>
          <el-col :xs="24" :sm="12">
            <el-statistic
              title="已实现收益率"
              :value="realizedPnL.realized_pnl_rate"
              :formatter="signed"
              suffix="%"
              :value-style="{ color: getProfitColor(realizedPnL.realized_pnl) }"
            />
          </el-col>
        </el-row>

        <el-divider />

        <div class="stat-detail">
          <div class="stat-item">
            <span>含股息已实现收益：</span>
            <span
              class="value"
              :style="{ color: getProfitColor(totalRealizedReturn.total_realized_return) }"
            >
              {{ formatCurrency(totalRealizedReturn.total_realized_return) }}
            </span>
          </div>
          <div class="stat-item">
            <span>含股息已实现收益率：</span>
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
            <span>已实现盈亏：</span>
            <span class="value" :style="{ color: getProfitColor(realizedPnL.realized_pnl) }">
              {{ formatCurrency(realizedPnL.realized_pnl) }}
            </span>
          </div>
          <div class="stat-item">
            <span>税后股息：</span>
            <span class="value" :style="{ color: COLOR.success }">
              {{ formatCurrency(totalRealizedReturn.net_dividend_income_cny) }}
            </span>
          </div>
          <div class="stat-note">
            已实现收益率只评价已卖出的交易；分母为被卖出部分的 FIFO 成本。含股息已实现收益 =
            已实现盈亏 + 税后股息。
          </div>
        </div>
      </el-card>
    </el-col>
  </el-row>
</template>

<style scoped>
.refresh-button {
  margin-right: 8px;
}

@media (max-width: 900px) {
  .refresh-button {
    margin-right: 0;
  }
}
</style>
