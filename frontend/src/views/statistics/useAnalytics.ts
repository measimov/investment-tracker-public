import { useChartColors } from '@/styles/chartTheme'
/**
 * TTWR 分析 feature（issue #140：Statistics 的五件事之"analytics 曲线 +
 * 基准对比 + 历史同步"）。状态、区间/基准选择、竞态防护、同步 job 轮询与
 * 图表 option 全在这里；AnalyticsCard.vue 只做展示与交互绑定。
 *
 * 返回 reactive 包：作为单个 prop 传给卡片后模板可直接读写
 * （rangePreset/customRange/selectedBenchmarks 由卡片双向绑定）。
 */

import { computed, reactive, watch } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import { presetRangeParams } from '@/utils/dateRange'
import { EMPTY, formatDate, formatNumber } from '@/utils/helpers'
import { pollJobUntilDone, type BackgroundJob } from '@/utils/polling'
import { CHART_FONT_FAMILY } from '@/styles/tokens'
import type { HistorySyncJob, PerformanceAnalytics } from './types'
import { isShortRange, nullableFiniteNumber, rangeSpanDays, riskFreeText } from './format'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { showApiError } from '@/utils/showApiError'

// 基准对比：选择持久化 localStorage；无数据基准降级为标签提示
const BENCHMARK_STORAGE_KEY = 'statistics.benchmarks'
const CHART_AXIS_FONT_SIZE = 12

// 区间选择：预设即业界主交互，自定义次之。切换只做便宜的重算（不触发行情同步）。
export const RANGE_PRESETS = [
  { value: 'all', label: '成立以来' },
  { value: '1m', label: '近1月' },
  { value: '3m', label: '近3月' },
  { value: '6m', label: '近6月' },
  { value: '1y', label: '近1年' },
  { value: 'ytd', label: '今年以来' },
  { value: 'custom', label: '自定义' }
]

function storedBenchmarks(): string[] {
  try {
    const stored = JSON.parse(window.localStorage.getItem(BENCHMARK_STORAGE_KEY) || 'null')
    if (Array.isArray(stored))
      return [...new Set(stored.filter((code) => typeof code === 'string'))].slice(0, 3)
  } catch {
    // 损坏的本地存储按默认值处理
  }
  return ['000300.SH']
}

export function useAnalytics({ isUnmounted }: { isUnmounted: () => boolean }) {
  const chartColors = useChartColors()
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)')
  const state = reactive({
    data: {
      calculation_level: 'empty',
      curve: [],
      metrics: {},
      trade_skill: {},
      data_quality: { warnings: [] }
    } as PerformanceAnalytics,
    loading: false,
    loaded: false,
    error: '',
    rangePreset: 'all',
    customRange: null as [string, string] | null,
    benchmarkCatalogError: '',
    benchmarkCatalogLoading: false,
    benchmarkOptions: [] as { code: string; name: string; currency: string }[],
    selectedBenchmarks: storedBenchmarks(),
    historyRefreshing: false,
    syncJob: null as HistorySyncJob | null,
    // 手工价试算（#218）：非 null 时每次 load（切区间/基准/同步历史后重算）都带着
    // 这组价格走 POST——此前只有点「计算」那一次用手工价，之后静默回到服务端价，
    // TTWR 卡与上方摘要卡用的不是同一组价格
    whatIfPrices: null as Record<string, number> | null
  })

  function rangeParams() {
    const preset = state.rangePreset
    if (preset === 'all') return {}
    if (preset === 'custom') {
      if (!state.customRange || state.customRange.length !== 2) return {}
      return { start_date: state.customRange[0], end_date: state.customRange[1] }
    }
    return presetRangeParams(preset) || {}
  }

  // 快速切换区间时的请求竞态防护：只有最后一次发起的请求可以写入数据、
  // 清除 loading 或弹错误——较慢的旧响应直接丢弃。
  let requestSeq = 0

  async function load(options: { refresh_history?: boolean } = {}) {
    const seq = ++requestSeq
    state.loading = options.refresh_history !== true
    state.error = ''
    try {
      const response = await api.getPerformanceAnalytics(state.whatIfPrices, {
        refresh_history: options.refresh_history === true,
        benchmarks: state.selectedBenchmarks.join(','),
        ...rangeParams()
      })
      if (seq !== requestSeq || isUnmounted()) return false
      state.data = response.data
      state.loaded = true
      state.error = ''
      return true
    } catch (error) {
      if (seq !== requestSeq || isUnmounted()) return false
      state.error = getApiErrorMessage(error, '加载收益率曲线失败')
      showApiError(error, { prefix: '加载收益率曲线失败' })
      return false
    } finally {
      if (seq === requestSeq && !isUnmounted()) {
        state.loading = false
      }
    }
  }

  async function loadBenchmarkCatalog() {
    state.benchmarkCatalogLoading = true
    state.benchmarkCatalogError = ''
    try {
      const response = await api.getBenchmarkCatalog()
      if (isUnmounted()) return
      state.benchmarkOptions = response.data
    } catch (error) {
      if (!isUnmounted())
        state.benchmarkCatalogError = getApiErrorMessage(error, '基准目录暂不可用')
    } finally {
      if (!isUnmounted()) state.benchmarkCatalogLoading = false
    }
  }

  async function refreshHistoryAndReload() {
    state.historyRefreshing = true
    try {
      const response = await api.startPerformanceHistorySync()
      state.syncJob = response.data
      const completedJob = await pollJobUntilDone(
        () => api.getPerformanceHistorySyncJob(response.data.id),
        {
          maxAttempts: 1800,
          isCancelled: isUnmounted,
          onUpdate: (job: BackgroundJob) => {
            state.syncJob = job as HistorySyncJob
          },
          timeoutMessage: '历史行情同步仍在后台运行，请稍后刷新统计页查看',
          failureMessage: '历史行情同步失败',
          acceptFailedResult: (job) =>
            Number(job.success_count) > 0 &&
            Number(job.failed_count) > 0 &&
            !job.error &&
            !job.result?.error
        }
      )
      if (!completedJob) return

      await load()

      if (completedJob.status === 'succeeded') {
        ElMessage.success(
          `历史行情同步完成：成功${completedJob.success_count || 0}项，跳过${
            completedJob.skipped_count || 0
          }项`
        )
      } else {
        ElMessage.warning(
          `历史行情同步完成但有失败：成功${completedJob.success_count || 0}项，失败${
            completedJob.failed_count || 0
          }项`
        )
      }
    } catch (error) {
      showApiError(error, { prefix: '历史行情同步失败' })
    } finally {
      state.historyRefreshing = false
    }
  }

  watch(
    () => [state.rangePreset, state.customRange] as const,
    () => {
      if (
        state.rangePreset === 'custom' &&
        (!state.customRange || state.customRange.length !== 2)
      ) {
        return // 等待自定义区间选完再重算
      }
      load()
    }
  )

  watch(
    () => state.selectedBenchmarks,
    (codes) => {
      try {
        window.localStorage.setItem(BENCHMARK_STORAGE_KEY, JSON.stringify(codes))
      } catch {
        // 存储失败不阻断（隐私模式等）
      }
      load()
    }
  )

  const curve = computed(() => state.data.curve || [])
  const metrics = computed(() => state.data.metrics || {})
  const tradeSkill = computed(() => state.data.trade_skill || {})
  const rangeSummary = computed(() => state.data.range_summary || {})
  const warnings = computed(() => state.data.data_quality?.warnings || [])

  const benchmarks = computed(() => state.data.benchmarks || [])
  const okBenchmarks = computed(() => benchmarks.value.filter((b) => b.status === 'ok'))
  const unavailableBenchmarks = computed(() => benchmarks.value.filter((b) => b.status !== 'ok'))
  // 基准数据晚于区间起点（first_available）：曲线仍画（头部空洞可见），
  // 但不产出超额收益——tag 说明降级原因
  const partialBenchmarks = computed(() =>
    benchmarks.value.filter((b) => b.status === 'ok' && b.alignment === 'first_available')
  )
  const primaryBenchmarkComparison = computed(() => {
    const first = okBenchmarks.value.find((b) => b.comparison)
    if (!first?.comparison) return null
    return { name: first.name, excess_return_rate: first.comparison.excess_return_rate ?? null }
  })

  // 区间不足半年：年化（TTWR 与 XIRR）只标注「仅供参考」，数值照常显示
  const rangeDays = computed(() => {
    const range = state.data.date_range
    const fromRange = rangeSpanDays(range?.start_date, range?.end_date)
    if (fromRange !== null) return fromRange
    const span = state.data.metrics?.observation_span_days
    return typeof span === 'number' ? span : null
  })
  const shortRange = computed(() => isShortRange(rangeDays.value))

  const effectiveRangeLabel = computed(() => {
    const range = state.data.date_range
    if (!range) return ''
    let label = `区间 ${formatDate(range.start_date)} 至 ${formatDate(range.end_date)}`
    if (range.clamped) label += '（已按有效数据区间调整）'
    return label
  })

  const syncPercent = computed(() =>
    Math.max(0, Math.min(100, Math.round(Number(state.syncJob?.progress_percent || 0))))
  )
  const syncProgressStatus = computed(() => {
    if (state.syncJob?.status === 'failed' && Number(state.syncJob.success_count) > 0)
      return 'warning'
    if (state.syncJob?.status === 'failed' || state.syncJob?.status === 'interrupted')
      return 'exception'
    if (state.syncJob?.status === 'succeeded') return 'success'
    return undefined
  })
  const syncStatusText = computed(() => {
    const status = state.syncJob?.status
    if (status === 'queued') return '历史行情同步排队中'
    if (status === 'running') return '历史行情同步中'
    if (status === 'succeeded') return '历史行情同步完成'
    if (status === 'failed')
      return Number(state.syncJob?.success_count) > 0 ? '历史行情同步部分完成' : '历史行情同步失败'
    if (status === 'interrupted') return '历史行情同步已中断'
    return '历史行情同步'
  })

  const chartOption = computed(() => {
    // 基准虚线沿用分类色板后段，避开组合主线与回撤。
    const benchmarkColors = chartColors.value.benchmarkPalette
    const dates = curve.value.map((item) => item.date)
    const returns = curve.value.map((item) => nullableFiniteNumber(item.cumulative_return_rate))
    const drawdowns = curve.value.map((item) => nullableFiniteNumber(item.drawdown_rate))

    // 基准虚线：与组合曲线同栅格生成，按日期对齐（first_available 时头部为空洞）
    const benchmarkSeries = okBenchmarks.value.map((block, index) => {
      const rateByDate = new Map(
        (block.points || []).map((point) => [
          point.date,
          nullableFiniteNumber(point.cumulative_return_rate)
        ])
      )
      return {
        name: block.name,
        type: 'line',
        smooth: false,
        symbol: 'none',
        xAxisIndex: 0,
        yAxisIndex: 0,
        connectNulls: false,
        data: dates.map((day) => rateByDate.get(day) ?? null),
        lineStyle: {
          width: 2,
          type: 'dashed',
          color: benchmarkColors[index % benchmarkColors.length]
        },
        itemStyle: {
          color: benchmarkColors[index % benchmarkColors.length]
        },
        emphasis: { focus: 'series' },
        z: 2
      }
    })

    return {
      animation: !reducedMotion.value,
      animationDuration: 0,
      animationDurationUpdate: 180,
      textStyle: { fontFamily: CHART_FONT_FAMILY, color: chartColors.value.text },
      tooltip: {
        backgroundColor: chartColors.value.surface,
        borderColor: chartColors.value.border,
        textStyle: { color: chartColors.value.text },
        trigger: 'axis',
        confine: true,
        // 基准线头部空洞（null）显示占位符，不拼成「—%」
        valueFormatter: (value: number | string | null | undefined) => {
          const text = formatNumber(nullableFiniteNumber(value), 2)
          return text === EMPTY ? EMPTY : `${text}%`
        }
      },
      legend: {
        type: 'scroll',
        top: 0,
        left: 52,
        right: 16,
        icon: 'roundRect',
        itemWidth: 18,
        itemHeight: 3,
        itemGap: 20,
        textStyle: { color: chartColors.value.muted, fontSize: 13, lineHeight: 18 },
        pageTextStyle: { color: chartColors.value.text },
        pageIconColor: chartColors.value.text,
        formatter: (name: string) => (name === '累计 TTWR 收益率' ? '组合 TTWR' : name),
        data: ['累计 TTWR 收益率', ...benchmarkSeries.map((series) => series.name)]
      },
      grid: [
        {
          left: 52,
          right: 16,
          // 图例独占顶部一行，与收益率轴名留出明确间距。
          top: 64,
          bottom: 128,
          containLabel: false
        },
        { left: 52, right: 16, height: 64, bottom: 40, containLabel: false }
      ],
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      dataZoom: [
        {
          type: 'inside',
          xAxisIndex: [0, 1],
          zoomOnMouseWheel: false,
          moveOnMouseWheel: false,
          preventDefaultMouseMove: false
        },
        {
          type: 'slider',
          xAxisIndex: [0, 1],
          borderColor: chartColors.value.border,
          backgroundColor: chartColors.value.surface,
          fillerColor: chartColors.value.secondary,
          textStyle: { color: chartColors.value.muted },
          dataBackground: {
            lineStyle: { color: chartColors.value.muted },
            areaStyle: { color: chartColors.value.secondary }
          },
          height: 18,
          bottom: 0,
          showDetail: false,
          brushSelect: false
        }
      ],
      xAxis: [
        {
          gridIndex: 0,
          axisLine: { onZero: true, lineStyle: { color: chartColors.value.border } },
          axisTick: { show: false },
          axisLabel: { show: false },
          type: 'category',
          boundaryGap: false,
          data: dates
        },
        {
          gridIndex: 1,
          axisLine: { lineStyle: { color: chartColors.value.border } },
          axisLabel: {
            color: chartColors.value.muted,
            fontSize: CHART_AXIS_FONT_SIZE,
            hideOverlap: true,
            showMinLabel: true,
            showMaxLabel: true,
            alignMinLabel: 'left',
            alignMaxLabel: 'right'
          },
          type: 'category',
          boundaryGap: false,
          data: dates
        }
      ],
      yAxis: ['收益率（%）', '回撤（%）'].map((name, gridIndex) => ({
        gridIndex,
        name,
        nameGap: 12,
        nameTextStyle: {
          color: chartColors.value.muted,
          fontSize: CHART_AXIS_FONT_SIZE,
          align: 'left'
        },
        splitNumber: gridIndex === 0 ? 4 : 2,
        splitLine: { lineStyle: { color: chartColors.value.separator } },
        axisLine: { show: false },
        axisTick: { show: false },
        type: 'value',
        axisLabel: {
          color: chartColors.value.muted,
          fontSize: CHART_AXIS_FONT_SIZE,
          formatter: '{value}%'
        }
      })),
      series: [
        {
          name: '累计 TTWR 收益率',
          type: 'line',
          smooth: false,
          symbol: 'circle',
          symbolSize: 6,
          showSymbol: false,
          xAxisIndex: 0,
          yAxisIndex: 0,
          connectNulls: false,
          data: returns,
          lineStyle: {
            width: 3,
            color: chartColors.value.primary
          },
          itemStyle: {
            color: chartColors.value.primary
          },
          emphasis: { focus: 'series' },
          z: 3
        },
        {
          name: '回撤',
          type: 'line',
          smooth: false,
          symbol: 'none',
          xAxisIndex: 1,
          yAxisIndex: 1,
          connectNulls: false,
          data: drawdowns,
          areaStyle: {
            color: chartColors.value.dangerSoft
          },
          lineStyle: {
            width: 2,
            color: chartColors.value.danger
          },
          itemStyle: {
            color: chartColors.value.danger
          }
        },
        ...benchmarkSeries
      ]
    }
  })

  // 无风险利率不再由前端指定：后端默认按参考利率序列（SHIBOR 3M）计算（#200）
  const riskFreeNote = computed(() => riskFreeText(state.data?.risk_free))

  return reactive({
    state,
    curve,
    metrics,
    riskFreeNote,
    tradeSkill,
    rangeSummary,
    warnings,
    unavailableBenchmarks,
    partialBenchmarks,
    primaryBenchmarkComparison,
    shortRange,
    effectiveRangeLabel,
    syncPercent,
    syncProgressStatus,
    syncStatusText,
    chartOption,
    load,
    loadBenchmarkCatalog,
    refreshHistoryAndReload
  })
}

export type AnalyticsFeature = ReturnType<typeof useAnalytics>
