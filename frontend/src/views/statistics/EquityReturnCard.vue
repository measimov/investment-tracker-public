<script setup lang="ts">
import { COLOR } from '@/styles/tokens'
import { formatCurrency, profitColor as getProfitColor } from '@/utils/helpers'
import type { AccountReturn } from './types'

defineProps<{ accountReturn: AccountReturn }>()
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
              :precision="2"
              suffix="%"
              :value-style="{ color: getProfitColor(accountReturn.total_return) }"
            />
          </el-col>
          <el-col :xs="24" :sm="8">
            <el-statistic
              v-if="accountReturn.annualized_return_rate != null"
              title="权益仓 XIRR"
              :value="accountReturn.annualized_return_rate"
              :precision="2"
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
          <div class="stat-item">
            <span>当前市值：</span>
            <span class="value">{{ formatCurrency(accountReturn.current_market_value_cny) }}</span>
          </div>
          <div class="stat-item">
            <span>已平仓资本利得：</span>
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
            总收益 = 已平仓资本利得 + 未实现盈亏 +
            税后股息。权益仓口径：仅统计投入证券的资金，账户闲置现金与外部出入金
            不计入、不稀释收益率；收益率及 XIRR 在此口径内精确，不代表全账户表现。
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
