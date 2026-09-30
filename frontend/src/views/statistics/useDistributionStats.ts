/**
 * 分布统计 feature（市场分布/时间趋势/持仓排行/概览摘要）：
 * 四个只读统计块 + 饼图/柱图 option。
 */

import { computed, reactive } from 'vue'
import api from '@/api'
import { formatNumber } from '@/utils/helpers'
import { showApiError } from '@/utils/showApiError'
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

  // 统计块加载器：fetch 返回错误而不自己弹窗，由调用方决定怎么提示——页面首次加载时四块一起失败
  // 只提示一次（后端一挂，此前是 4 条消息 + 全局通知，#284）。silent 的块（摘要卡片允许静默降级）
  // 失败只记 console，不进汇总提示
  interface StatsBlock {
    label: string
    silent: boolean
    fetch: () => Promise<unknown>
  }

  function makeStatsBlock<T>(
    assign: (data: T) => void,
    fetcher: () => Promise<{ data: T }>,
    label: string,
    { silent = false }: { silent?: boolean } = {}
  ): StatsBlock {
    return {
      label,
      silent,
      fetch: async () => {
        try {
          assign((await fetcher()).data)
          return null
        } catch (error) {
          if (silent) console.error(`加载${label}失败`, error)
          return error
        }
      }
    }
  }

  const marketBlock = makeStatsBlock<MarketStat[]>(
    (data) => (state.marketStats = data),
    () => api.getStatsByMarket(),
    '市场统计'
  )
  const timeBlock = makeStatsBlock<TimeStat[]>(
    (data) => (state.timeStats = data),
    () => api.getStatsByTime(state.timeGroupBy),
    '时间统计'
  )
  const profitLossBlock = makeStatsBlock<ProfitLossItem[]>(
    (data) => (state.profitLossData = data),
    () => api.getHoldingsCostBreakdown(),
    '持仓成本分布'
  )
  const summaryBlock = makeStatsBlock<SummaryStats>(
    (data) => (state.summaryStats = data),
    () => api.getSummary(),
    '统计摘要',
    { silent: true }
  )

  /** 加载若干统计块，失败的（非 silent）合并成一条提示。 */
  async function loadBlocks(blocks: StatsBlock[]) {
    const errors = await Promise.all(blocks.map((block) => block.fetch()))
    const failed = blocks.filter((block, index) => errors[index] && !block.silent)
    if (!failed.length) return
    const firstError = errors[blocks.indexOf(failed[0])]
    showApiError(firstError, { prefix: `加载${failed.map((block) => block.label).join('、')}失败` })
  }

  const loadAll = () => loadBlocks([marketBlock, timeBlock, profitLossBlock, summaryBlock])
  // 切换时间粒度只重载时间统计（模板里 @change 会传入新值，这里不收参数）
  const loadTimeStats = () => loadBlocks([timeBlock])

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
    loadAll,
    loadTimeStats
  })
}

export type DistributionStatsFeature = ReturnType<typeof useDistributionStats>
