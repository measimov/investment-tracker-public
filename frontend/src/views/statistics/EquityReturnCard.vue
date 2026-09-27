<script setup lang="ts">
import { COLOR } from '@/styles/tokens'
import { formatCurrency, profitColor as getProfitColor } from '@/utils/helpers'
import type { AccountReturn } from './types'
import { signedNumber } from './format'

defineProps<{ accountReturn: AccountReturn }>()

const signed = (value: number) => signedNumber(value)
</script>

<template>
  <el-row :gutter="20" class="performance-cards">
    <el-col :span="24">
      <el-card class="stat-card" shadow="hover">
        <template #header>
          <div class="card-header">
            <div class="title-with-tag">
              <span>证券持仓收益</span>
              <el-tag type="info" effect="plain" size="small">权益仓口径</el-tag>
            </div>
          </div>
        </template>

        <el-row :gutter="20">
          <el-col :xs="24" :sm="8">
            <el-statistic
              title="总收益"
              :value="accountReturn.total_return"
              :precision="2"
              prefix="¥"
              :value-style="{ color: getProfitColor(accountReturn.total_return) }"
            />
          </el-col>
          <el-col :xs="24" :sm="8">
            <el-statistic
              title="总收益率"
              :value="accountReturn.total_return_rate"
              :formatter="signed"
              suffix="%"
              :value-style="{ color: getProfitColor(accountReturn.total_return) }"
            />
          </el-col>
          <el-col :xs="24" :sm="8">
            <el-statistic
              v-if="accountReturn.annualized_return_rate != null"
              title="权益仓 XIRR"
              :value="accountReturn.annualized_return_rate"
              :formatter="signed"
              suffix="%"
              :value-style="{
                color: getProfitColor(accountReturn.annualized_return_rate)
              }"
            />
            <div v-else class="empty-statistic">
              <span>权益仓 XIRR</span>
              <strong>—</strong>
            </div>
          </el-col>
        </el-row>

        <el-divider />

        <div class="stat-detail">
          <div class="stat-item">
            <span>净投入本金（权益仓）：</span>
            <span class="value">{{
              formatCurrency(accountReturn.net_invested_principal_cny)
            }}</span>
          </div>
          <!-- 清仓后净投入 ≤ 0，收益率分母改用峰值投入（后端 rate_denominator），
               不说明的话「总收益率」与上面的负本金对不上 -->
          <div
            v-if="accountReturn.rate_denominator === 'peak_invested_principal_cny'"
            class="stat-item"
            data-testid="rate-denominator"
          >
            <span>收益率分母：峰值投入</span>
            <span class="value">{{
              formatCurrency(accountReturn.peak_invested_principal_cny)
            }}</span>
          </div>
          <div class="stat-item">
            <span>当前市值：</span>
            <span class="value">{{ formatCurrency(accountReturn.current_market_value_cny) }}</span>
          </div>
          <div class="stat-item">
            <span>已实现盈亏：</span>
            <span
              class="value"
              :style="{ color: getProfitColor(accountReturn.realized_trading_pnl_cny) }"
            >
              {{ formatCurrency(accountReturn.realized_trading_pnl_cny) }}
            </span>
          </div>
          <div class="stat-item">
            <span>未实现盈亏：</span>
            <span
              class="value"
              :style="{ color: getProfitColor(accountReturn.unrealized_pnl_cny) }"
            >
              {{ formatCurrency(accountReturn.unrealized_pnl_cny) }}
            </span>
          </div>
          <div class="stat-item">
            <span>税后股息：</span>
            <span class="value" :style="{ color: COLOR.success }">
              {{ formatCurrency(accountReturn.net_dividend_income_cny) }}
            </span>
          </div>
          <div class="stat-note">
            总收益 = 已实现盈亏 + 未实现盈亏 +
            税后股息。权益仓口径：仅统计投入证券的资金，账户闲置现金与外部出入金
            不计入、不稀释收益率，不代表全账户表现。汇率口径：上述金额按最新汇率折算人民币； XIRR
            与期间损益按每笔流水当日汇率折算，两者含汇率变动的方式不同。
            <template v-if="accountReturn.rate_denominator === 'peak_invested_principal_cny'">
              净投入本金已不为正（已大部分或全部卖出），总收益率改以峰值投入为分母。
            </template>
          </div>
        </div>
      </el-card>
    </el-col>
  </el-row>
</template>

<style scoped>
.empty-statistic {
  display: flex;
  min-height: 65px;
  flex-direction: column;
  gap: 8px;
}

.empty-statistic span {
  color: var(--app-text-muted);
  font-size: 13px;
}

.empty-statistic strong {
  color: var(--app-text);
  font-size: 24px;
  line-height: 1.2;
}
</style>
