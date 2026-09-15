/**
 * 批量分析 feature（一键分析所有持仓，issue #140）。
 *
 * 后端是串行长任务（数十分钟到数小时），前端只负责启动、轮询展示与终止。
 * 三种语义严格区分，文案不得混用：
 *   离开页面   → 任务继续，回来自动恢复（无 UI）
 *   停止查看   → 只停本页轮询，token 照烧
 *   终止任务   → 真正中止（后端在标的边界收尾）
 *
 * 状态机（percent/ETA/轮询/终止/停止查看）在 useBatchJobProgress 收敛一处；
 * 这里只留批量分析特有的：目标预览、确认框、每完成一只刷标签列、汇总消息。
 */

import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'
import { useBatchJobProgress } from '@/composables/useBatchJobProgress'
import { getApiErrorMessage } from '@/utils/apiErrors'
import type { Holding } from '@/stores/holdings'
import type { AnalysisBatchJob, BatchResultRow } from './types'

// 与后端 security_profile_service.SUPPORTED_MARKETS 对齐；其余市场后端 409
const ANALYZABLE_MARKETS = new Set(['A股', '美股', '港股'])
// 纯 UI 量级提示：单标的实测 1~3 分钟（同步基本面 + 已有摘要 + LLM 生成）
const MINUTES_PER_SYMBOL_LOW = 1
const MINUTES_PER_SYMBOL_HIGH = 3
export const BATCH_POLL_INTERVAL_MS = 5000
// 先定墙钟上限（6 小时）再反推次数；超时只是停止本页轮询，任务仍在后台
export const BATCH_POLL_MAX_ATTEMPTS = 4320

const BATCH_STATUS_LABELS: Record<string, string> = {
  queued: '批量分析排队中',
  running: '批量分析进行中',
  succeeded: '批量分析完成',
  failed: '批量分析失败',
  interrupted: '批量分析已终止'
}

// 目标数以后端预览为准：后端还会排除已清仓、EXCLUDE 与 CASH_MANAGEMENT 标的，
// 只按支持市场在本地算会虚高（持有货币基金时尤其明显），启动后 job.total 又
// 突然变小。
//
// 三态而不是"count 为 null 就回退"：null 既表示"还没取到"也表示"取失败了"，
// 混在一起会让预览**在途期间**也用本地数——用户此时点按钮就会看到错误的目标数。
// 因此确认框前必须等预览定案，只有**确定失败**才回退本地估算。
type BatchTargetsState = 'idle' | 'loading' | 'ready' | 'failed'

export function useBatchAnalysis({
  isUnmounted,
  holdings,
  refreshAnalyses
}: {
  isUnmounted: () => boolean
  holdings: () => Holding[]
  refreshAnalyses: () => Promise<void>
}) {
  const batchTargetCount = ref<number | null>(null)
  const batchTargetsState = ref<BatchTargetsState>('idle')
  let batchTargetsPromise: Promise<void> | null = null

  const {
    job,
    starting,
    isActive,
    percent,
    progressStatus,
    statusText,
    etaText,
    adopt,
    watchJob,
    requestCancel,
    stopWatching
  } = useBatchJobProgress<AnalysisBatchJob>({
    fetchJob: (jobId) => api.getSecurityAnalysisBatchJob(jobId),
    cancelJob: (jobId) => api.cancelSecurityAnalysisBatchJob(jobId),
    statusLabels: BATCH_STATUS_LABELS,
    isUnmounted,
    pollIntervalMs: BATCH_POLL_INTERVAL_MS,
    pollMaxAttempts: BATCH_POLL_MAX_ATTEMPTS,
    timeoutMessage: '批量分析仍在后台运行；重新进入持仓页可继续查看进度',
    failureMessage: '批量分析失败',
    cancelConfirm: {
      message: '已生成的分析会保留，未开始的标的不再分析。当前正在分析的标的会先跑完再停止。',
      title: '终止批量分析'
    },
    cancelledMessage: '批量分析已终止，已生成的分析已保留',
    onUpdate: (job, previous) => {
      // 每完成一只就刷一次标签列：标签一格一格亮起来是最好的进度反馈
      if (Number(job.completed || 0) > Number(previous?.completed || 0)) refreshAnalyses()
    },
    onSuccess: async (job) => {
      await refreshAnalyses()
      loadTargetCount(true) // 新鲜度窗口变了，下次的目标数随之变化
      const success = Number(job.success_count || 0)
      const skipped = Number(job.skipped_count || 0)
      const failed = Number(job.failed_count || 0)
      const summary =
        `批量分析完成：成功 ${success} 只` +
        (skipped ? `，跳过 ${skipped} 只` : '') +
        (failed ? `，失败 ${failed} 只` : '')
      if (!failed) ElMessage.success(summary)
      else if (success > 0) ElMessage.warning(summary)
      else ElMessage.error('批量分析全部失败，请检查 LLM 配置后重试')
    },
    onCancelled: async () => {
      await refreshAnalyses()
    }
  })

  const localAnalyzableCount = computed(
    () =>
      new Set(
        holdings()
          .filter((row) => ANALYZABLE_MARKETS.has(row.market))
          .map((row) => `${row.symbol}:${row.market}`)
      ).size
  )

  // 按钮的启用判据：预览在途/失败时用本地估算保持可点（宁可点开后再告知
  // "没有可分析标的"，也不要把按钮错误地禁掉）。确认框里的数字另走 resolve。
  const analyzableCount = computed(() =>
    batchTargetsState.value === 'ready' && batchTargetCount.value !== null
      ? batchTargetCount.value
      : localAnalyzableCount.value
  )

  function loadTargetCount(force = false): Promise<void> {
    if (batchTargetsPromise && !force) return batchTargetsPromise
    batchTargetsState.value = 'loading'
    batchTargetsPromise = (async () => {
      try {
        const response = await api.getSecurityAnalysisBatchTargets()
        if (isUnmounted()) return
        batchTargetCount.value = Number(response.data?.total ?? 0)
        batchTargetsState.value = 'ready'
      } catch {
        if (isUnmounted()) return
        batchTargetsState.value = 'failed' // 确定失败后才允许回退本地估算
      }
    })()
    return batchTargetsPromise
  }

  /** 确认框用的目标数：等预览定案；只有确定失败才退回本地估算。 */
  async function resolveTargetCount(): Promise<number> {
    if (batchTargetsState.value !== 'ready') await loadTargetCount()
    if (batchTargetsState.value === 'ready' && batchTargetCount.value !== null) {
      return batchTargetCount.value
    }
    return localAnalyzableCount.value
  }

  const recentResults = computed<BatchResultRow[]>(() =>
    [...(job.value?.results || [])].slice(-5).reverse()
  )

  function resultLabel(item: BatchResultRow): string {
    if (item.status === 'succeeded') return '已分析'
    if (item.status === 'skipped') return '已跳过'
    return '失败'
  }

  function estimateText(count: number): string {
    const low = count * MINUTES_PER_SYMBOL_LOW
    const high = count * MINUTES_PER_SYMBOL_HIGH
    if (high <= 90) return `${low}~${high} 分钟`
    return `约 ${(low / 60).toFixed(1)}~${(high / 60).toFixed(1)} 小时`
  }

  async function analyzeAll() {
    starting.value = true
    // 先等目标预览定案，确认框里的数量/耗时/token 预期不能用在途的本地估算
    const targetCount = await resolveTargetCount()
    if (isUnmounted()) return
    if (targetCount === 0) {
      starting.value = false
      ElMessage.info('当前没有可分析的持仓标的（A股/美股/港股，且不含已排除与现金管理标的）')
      return
    }
    try {
      await ElMessageBox.confirm(
        `将对 ${targetCount} 只持仓标的（A股/美股/港股）逐个同步基本面并调用 LLM 生成分析，` +
          `预计耗时 ${estimateText(targetCount)}，会消耗较多 LLM token。\n` +
          `24 小时内已分析过的标的会自动跳过；任务在后台运行，关闭页面不会中断。`,
        '一键分析所有持仓',
        { type: 'warning', confirmButtonText: '开始分析', cancelButtonText: '取消' }
      )
    } catch {
      starting.value = false
      return // 用户取消
    }

    try {
      const response = await api.startSecurityAnalysisBatchJob()
      if (isUnmounted()) return
      adopt(response.data as AnalysisBatchJob)
      // 启动完成即收起 loading：轮询要跑数十分钟到数小时，让按钮一直转圈既无
      // 信息量（进度块已经在显示了），也与"活跃时只禁用"的设计不符
      starting.value = false
      await watchJob(response.data.id)
    } catch (error) {
      if (isUnmounted()) return
      ElMessage.error(getApiErrorMessage(error, '批量分析启动失败'))
    } finally {
      if (!isUnmounted()) starting.value = false
    }
  }

  return reactive({
    job,
    starting,
    isActive,
    percent,
    progressStatus,
    statusText,
    etaText,
    adopt,
    watchJob,
    requestCancel,
    stopWatching,
    analyzableCount,
    loadTargetCount,
    recentResults,
    resultLabel,
    analyzeAll
  })
}

export type BatchAnalysisFeature = ReturnType<typeof useBatchAnalysis>
