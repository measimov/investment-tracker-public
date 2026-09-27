/**
 * 分布统计 feature（市场分布/时间趋势/持仓排行/概览摘要）：
 * 四个只读统计块 + 饼图/柱图 option。
 */

import { computed, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import { formatNumber } from '@/utils/helpers'
import { CHART_FONT_FAMILY, CHART_PALETTE, COLOR, chartTooltipCurrency } from '@/styles/tokens'
import type { MarketStat } from '@/types'
import type { ProfitLossItem, SummaryStats, TimeStat } from './types'

export function useDistributionStats() {
  const state = reactive({
    marketStats: [] as MarketStat[],
    timeStats: [] as TimeStat[],
    profitLossData: [] as ProfitLossItem[],
    summaryStats: {} as SummaryStats,
    timeGroupBy: 'month'
  })

  // 统计块加载工厂；silent=true 时失败只记 console（摘要卡片允许静默降级）
  function makeStatsLoader<T>(
    assign: (data: T) => void,
    fetcher: () => Promise<{ data: T }>,
    failureMessage: string,
    { silent = false }: { silent?: boolean } = {}
  ) {
    return async () => {
      try {
        const response = await fetcher()
        assign(response.data)
      } catch (error) {
        if (silent) console.error(failureMessage, error)
        else ElMessage.error(failureMessage)
      }
    }
  }

  const loadMarketStats = makeStatsLoader<MarketStat[]>(
    (data) => (state.marketStats = data),
    () => api.getStatsByMarket(),
    '加载市场统计失败'
  )
  const loadTimeStats = makeStatsLoader<TimeStat[]>(
    (data) => (state.timeStats = data),
    () => api.getStatsByTime(state.timeGroupBy),
    '加载时间统计失败'
  )
  const loadProfitLoss = makeStatsLoader<ProfitLossItem[]>(
    (data) => (state.profitLossData = data),
    () => api.getHoldingsCostBreakdown(),
    '加载持仓成本分布失败'
  )
  const loadSummaryStats = makeStatsLoader<SummaryStats>(
    (data) => (state.summaryStats = data),
    () => api.getSummary(),
    '加载统计摘要失败',
    { silent: true }
  )

  const totalInvested = computed(() =>
    state.marketStats.reduce((sum, item) => sum + item.total_cost, 0)
  )

  // 排行占比的分母：后端折好的 CNY 成本之和（与概览同口径）；缺汇率的行
  // total_cost_cny 为 null，不计入分母（与该行占比显示「—」一致）
  const totalInvestedCNY = computed(() =>
    state.profitLossData.reduce((sum, item) => sum + (item.total_cost_cny ?? 0), 0)
  )

  const marketChartOption = computed(() => ({
    color: CHART_PALETTE,
    textStyle: { fontFamily: CHART_FONT_FAMILY },
    tooltip: {
      trigger: 'item',
      formatter: (params: { name: string; value: number; percent: number }) =>
        `${params.name}: ${chartTooltipCurrency(params.value)} (${params.percent}%)`
    },
    legend: { bottom: 0, left: 'center' },
    series: [
      {
        type: 'pie',
        radius: ['42%', '68%'],
        center: ['50%', '44%'],
        avoidLabelOverlap: false,
        itemStyle: {
          borderRadius: 10,
          borderColor: '#fff',
          borderWidth: 2
        },
        label: {
          show: true,
          formatter: '{b}: {d}%'
        },
        emphasis: {
          label: {
            show: true,
            fontSize: 16,
            fontWeight: 'bold'
          }
        },
        data: state.marketStats.map((item) => ({
          name: item.market,
          value: item.total_cost
        }))
      }
    ]
  }))

  const timeChartOption = computed(() => {
    const periods = state.timeStats.map((item) => item.period)
    const buyAmounts = state.timeStats.map((item) => item.buy_amount)
    const sellAmounts = state.timeStats.map((item) => item.sell_amount)

    return {
      textStyle: { fontFamily: CHART_FONT_FAMILY },
      tooltip: {
        trigger: 'axis',
        axisPointer: {
          type: 'shadow'
        },
        valueFormatter: (value: number | string) => chartTooltipCurrency(value)
      },
      legend: {
        data: ['买入金额', '卖出金额']
      },
      grid: {
        left: '3%',
        right: '4%',
        bottom: '3%',
        containLabel: true
      },
      xAxis: {
        type: 'category',
        data: periods
      },
      yAxis: {
        type: 'value',
        axisLabel: {
          // 千分位（此前 ¥1000000 一长串）
          formatter: (value: number) => `¥${formatNumber(value, 0)}`
        }
      },
      series: [
        {
          name: '买入金额',
          type: 'bar',
          data: buyAmounts,
          itemStyle: {
            color: COLOR.success
          }
        },
        {
          name: '卖出金额',
          type: 'bar',
          data: sellAmounts,
          itemStyle: {
            color: COLOR.danger
          }
        }
      ]
    }
  })

  return reactive({
    state,
    totalInvested,
    totalInvestedCNY,
    marketChartOption,
    timeChartOption,
    loadMarketStats,
    loadTimeStats,
    loadProfitLoss,
    loadSummaryStats
  })
}

export type DistributionStatsFeature = ReturnType<typeof useDistributionStats>
