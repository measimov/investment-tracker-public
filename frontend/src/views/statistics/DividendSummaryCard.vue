<script setup lang="ts">
import { COLOR } from '@/styles/tokens'
import type { DividendSummary } from './types'

defineProps<{ dividendSummary: DividendSummary }>()
</script>

<template>
  <el-row :gutter="20" class="section-gap-bottom">
    <el-col :span="24">
      <el-card class="stat-card" shadow="hover">
        <template #header>
          <div class="card-header">
            <span>股息收入统计</span>
          </div>
        </template>

        <el-alert
          v-if="dividendSummary.missing_rate_currencies?.length"
          type="warning"
          :closable="false"
          show-icon
          class="methodology-alert"
          :title="`缺少 ${dividendSummary.missing_rate_currencies.join('/')} 汇率，对应股息未计入 CNY 折算总额，请先在汇率页补录`"
        />

        <el-row :gutter="20">
          <el-col :xs="24" :sm="8">
            <el-statistic
              title="累计股息（税前）"
              :value="dividendSummary.total_dividend_gross"
              :precision="2"
              prefix="¥"
            />
          </el-col>
          <el-col :xs="24" :sm="8">
            <el-statistic
              title="累计税费"
              :value="dividendSummary.total_tax"
              :precision="2"
              prefix="¥"
            />
          </el-col>
          <el-col :xs="24" :sm="8">
            <el-statistic
              title="累计股息（税后）"
              :value="dividendSummary.total_dividend_net"
              :precision="2"
              prefix="¥"
              :value-style="{ color: COLOR.success }"
            />
          </el-col>
        </el-row>
      </el-card>
    </el-col>
  </el-row>
</template>
