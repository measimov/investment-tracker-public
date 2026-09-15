<script setup lang="ts">
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { PieChart, BarChart } from 'echarts/charts'
import {
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  GridComponent
} from 'echarts/components'
import VChart from 'vue-echarts'
import { COLOR } from '@/styles/tokens'
import { useExchangeRates } from '@/composables/useExchangeRates'
import { formatCurrency, formatNumber } from '@/utils/helpers'
import type { DistributionStatsFeature } from './useDistributionStats'

use([
  CanvasRenderer,
  PieChart,
  BarChart,
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  GridComponent
])

defineProps<{ dist: DistributionStatsFeature }>()

const { convertToCNY } = useExchangeRates()
</script>

<template>
  <div>
    <!-- 原有的图表和表格 -->
    <el-row :gutter="20">
      <!-- Market Distribution -->
      <el-col :xs="24" :md="12">
        <el-card class="stat-card">
          <template #header>
            <span>市场分布统计</span>
          </template>
          <el-empty
            v-if="dist.state.marketStats.length === 0"
            description="暂无市场分布数据"
            :image-size="88"
          />
          <v-chart v-else :option="dist.marketChartOption" class="chart" autoresize />
        </el-card>
      </el-col>

      <!-- Market Table -->
      <el-col :xs="24" :md="12">
        <el-card class="stat-card">
          <template #header>
            <span>市场详细数据</span>
          </template>
          <div class="responsive-table">
            <el-table :data="dist.state.marketStats" stripe>
              <template #empty>
                <el-empty description="暂无市场统计数据" :image-size="88" />
              </template>
              <el-table-column prop="market" label="市场" min-width="100" />
              <el-table-column prop="holdings_count" label="持仓数" min-width="90" align="right" />
              <el-table-column prop="total_cost" label="总成本" min-width="130" align="right">
                <template #default="{ row }">
                  <span style="font-weight: bold">
                    {{ formatCurrency(row.total_cost) }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="占比" min-width="90" align="right">
                <template #default="{ row }">
                  {{ formatNumber((row.total_cost / dist.totalInvested) * 100, 2) }}%
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- Transaction Timeline -->
    <el-row :gutter="20" class="section-gap">
      <el-col :span="24">
        <el-card class="stat-card">
          <template #header>
            <div class="chart-header">
              <span>交易时间趋势</span>
              <el-radio-group v-model="dist.state.timeGroupBy" @change="dist.loadTimeStats">
                <el-radio-button value="month">按月</el-radio-button>
                <el-radio-button value="year">按年</el-radio-button>
              </el-radio-group>
            </div>
          </template>
          <el-empty
            v-if="dist.state.timeStats.length === 0"
            description="暂无交易时间趋势"
            :image-size="88"
          />
          <v-chart v-else :option="dist.timeChartOption" class="chart" autoresize />
        </el-card>
      </el-col>
    </el-row>

    <!-- Holdings Ranking -->
    <el-row :gutter="20" class="section-gap">
      <el-col :span="24">
        <el-card class="stat-card">
          <template #header>
            <span>持仓排行</span>
          </template>
          <div class="responsive-table">
            <el-table :data="dist.state.profitLossData" stripe>
              <template #empty>
                <el-empty description="暂无持仓排行数据" :image-size="88" />
              </template>
              <el-table-column type="index" label="排名" width="80" />
              <el-table-column prop="symbol" label="代码" min-width="100" />
              <el-table-column prop="name" label="名称" min-width="130" show-overflow-tooltip />
              <el-table-column prop="market" label="市场" width="80" />
              <el-table-column prop="quantity" label="数量" min-width="110" align="right">
                <template #default="{ row }">
                  {{ formatNumber(row.quantity, 4) }}
                </template>
              </el-table-column>
              <el-table-column prop="avg_cost" label="成本价" min-width="105" align="right">
                <template #default="{ row }">
                  {{ formatNumber(row.avg_cost, 4) }}
                </template>
              </el-table-column>
              <el-table-column prop="total_cost" label="总成本" min-width="130" align="right">
                <template #default="{ row }">
                  <span :style="{ fontWeight: 'bold', color: COLOR.primary }">
                    {{ formatCurrency(row.total_cost, row.currency) }}
                  </span>
                  <div
                    v-if="row.currency !== 'CNY'"
                    style="font-size: 12px; color: var(--app-text-soft); margin-top: 2px"
                  >
                    ≈ {{ formatCurrency(convertToCNY(row.total_cost, row.currency)) }}
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="占比" width="100" align="right">
                <template #default="{ row }">
                  {{
                    formatNumber(
                      (convertToCNY(row.total_cost, row.currency) / dist.totalInvestedCNY) * 100,
                      2
                    )
                  }}%
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>
