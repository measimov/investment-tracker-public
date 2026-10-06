import { useChartColors } from '@/styles/chartTheme'
/**
 * 分布统计 feature（市场分布/时间趋势/持仓排行/概览摘要）：
 * 四个只读统计块 + 饼图/柱图 option。
 */

import { computed, getCurrentScope, onScopeDispose, reactive } from 'vue'
import api from '@/api'
import { formatCurrency, formatNumber } from '@/utils/helpers'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { showApiError } from '@/utils/showApiError'
import { CHART_FONT_FAMILY } from '@/styles/tokens'
import { formatAmountTick } from './format'
import type { MarketStat } from '@/types'
import type { ProfitLossItem, SummaryStats, TimeStat } from './types'

export function useDistributionStats() {
  const chartColors = useChartColors()
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)')
  const state = reactive({
    marketStats: [] as MarketStat[],
    timeStats: [] as TimeStat[],
    profitLossData: [] as ProfitLossItem[],
    summaryStats: {} as SummaryStats,
    timeGroupBy: 'month',
    timeResultGroupBy: 'month',
    marketStatus: { loading: false, loaded: false, error: '' },
    timeStatus: { loading: false, loaded: false, error: '' },
    profitLossStatus: { loading: false, loaded: false, error: '' }
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
    {
      silent = false,
      status
    }: { silent?: boolean; status?: { loading: boolean; loaded: boolean; error: string } } = {}
  ): StatsBlock {
    let requestSequence = 0
    if (getCurrentScope()) onScopeDispose(() => ++requestSequence)
    return {
      label,
      silent,
      fetch: async () => {
        const sequence = ++requestSequence
        if (status) {
          status.loading = true
          status.error = ''
        }
        try {
          const response = await fetcher()
          if (sequence !== requestSequence) return null
          assign(response.data)
          if (status) {
            status.loaded = true
            status.error = ''
          }
          return null
        } catch (error) {
          if (sequence !== requestSequence) return null
          if (status) status.error = getApiErrorMessage(error, `加载${label}失败`)
          if (silent) console.error(`加载${label}失败`, error)
          return error
        } finally {
          if (sequence === requestSequence && status) status.loading = false
        }
      }
    }
  }

  const marketBlock = makeStatsBlock<MarketStat[]>(
    (data) => (state.marketStats = data),
    () => api.getStatsByMarket(),
    '市场统计',
    { status: state.marketStatus }
  )
  let requestedTimeGroupBy = state.timeGroupBy
  const timeBlock = makeStatsBlock<TimeStat[]>(
    (data) => {
      state.timeStats = data
      state.timeResultGroupBy = requestedTimeGroupBy
    },
    () => {
      requestedTimeGroupBy = state.timeGroupBy
      return api.getStatsByTime(requestedTimeGroupBy)
    },
    '时间统计',
    { status: state.timeStatus }
  )
  const profitLossBlock = makeStatsBlock<ProfitLossItem[]>(
    (data) => (state.profitLossData = data),
    () => api.getHoldingsCostBreakdown(),
    '持仓成本分布',
    { status: state.profitLossStatus }
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
    color: chartColors.value.marketPalette,
    animation: !reducedMotion.value,
    textStyle: { fontFamily: CHART_FONT_FAMILY, color: chartColors.value.text },
    tooltip: {
      backgroundColor: chartColors.value.surface,
      borderColor: chartColors.value.border,
      textStyle: { color: chartColors.value.text },
      trigger: 'item',
      formatter: (params: { name: string; value: number; percent: number }) =>
        `${params.name}: ${formatCurrency(params.value)} (${formatNumber(params.percent, 1)}%)`
    },
    legend: {
      textStyle: { color: chartColors.value.muted },
      bottom: 0,
      left: 'center'
    },
    series: [
      {
        type: 'pie',
        percentPrecision: 1,
        radius: ['42%', '68%'],
        center: ['50%', '44%'],
        avoidLabelOverlap: true,
        itemStyle: {
          borderRadius: 0,
          borderColor: chartColors.value.surface,
          borderWidth: 2
        },
        labelLine: { length: 4, length2: 4 },
        label: {
          color: chartColors.value.text,
          show: true,
          alignTo: 'edge',
          edgeDistance: 4,
          bleedMargin: 8,
          distanceToLabelLine: 2,
          fontSize: 12,
          formatter: (params: { name: string; percent: number }) =>
            `${params.name}\n${formatNumber(params.percent, 1)}%`
        },
        emphasis: {
          scale: false,
          label: {
            show: true,
            fontSize: 12,
            fontWeight: 'normal'
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
      animation: !reducedMotion.value,
      textStyle: { fontFamily: CHART_FONT_FAMILY, color: chartColors.value.text },
      tooltip: {
        backgroundColor: chartColors.value.surface,
        borderColor: chartColors.value.border,
        textStyle: { color: chartColors.value.text },
        trigger: 'axis',
        axisPointer: {
          type: 'shadow'
        },
        valueFormatter: (value: number | string) => formatCurrency(value)
      },
      legend: {
        textStyle: { color: chartColors.value.muted },
        data: ['买入金额', '卖出金额']
      },
      grid: {
        left: '3%',
        right: '4%',
        bottom: '3%',
        containLabel: true
      },
      xAxis: {
        axisLine: { lineStyle: { color: chartColors.value.border } },
        axisLabel: { color: chartColors.value.muted },
        type: 'category',
        data: periods
      },
      yAxis: {
        splitLine: { lineStyle: { color: chartColors.value.separator } },
        type: 'value',
        name: '人民币',
        splitNumber: 3,
        axisLabel: {
          color: chartColors.value.muted,
          formatter: formatAmountTick
        }
      },
      series: [
        {
          name: '买入金额',
          type: 'bar',
          data: buyAmounts,
          itemStyle: {
            color: chartColors.value.success
          }
        },
        {
          name: '卖出金额',
          type: 'bar',
          data: sellAmounts,
          itemStyle: {
            color: chartColors.value.danger
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
