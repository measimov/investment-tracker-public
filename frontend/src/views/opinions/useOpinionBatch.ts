/**
 * 批量观点摘要（观点页「一键生成」，克隆 holdings/useBatchAnalysis 的骨架）。
 *
 * 与批量分析的差异：每标的只有一次 LLM 调用（十几秒量级）、目标数只信后端
 * 预览（零匹配标的已在后端剔除，本地无法估算）、无 deep 模式。
 * 三种语义同款：离开页面任务继续 / 停止查看只停轮询 / 终止真正中止。
 */

import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'
import { useBatchJobProgress } from '@/composables/useBatchJobProgress'
import { getApiErrorMessage } from '@/utils/apiErrors'
import type { OpinionBatchJob, OpinionBatchTarget } from './types'

export const OPINION_BATCH_POLL_INTERVAL_MS = 3000
// 墙钟上限 1 小时（后端 BATCH_MAX_SECONDS=1800，留一倍余量）
export const OPINION_BATCH_POLL_MAX_ATTEMPTS = 1200

const STATUS_LABELS: Record<string, string> = {
  queued: '批量观点摘要排队中',
  running: '批量观点摘要进行中',
  succeeded: '批量观点摘要完成',
  failed: '批量观点摘要失败',
  interrupted: '批量观点摘要已终止'
}

export function useOpinionBatch({
  isUnmounted,
  refreshSummaries
}: {
  isUnmounted: () => boolean
  refreshSummaries: () => Promise<void>
}) {
  const targets = ref<OpinionBatchTarget[] | null>(null)

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
  } = useBatchJobProgress<OpinionBatchJob>({
    fetchJob: (jobId) => api.getOpinionBatchJob(jobId),
    cancelJob: (jobId) => api.cancelOpinionBatchJob(jobId),
    statusLabels: STATUS_LABELS,
    isUnmounted,
    pollIntervalMs: OPINION_BATCH_POLL_INTERVAL_MS,
    pollMaxAttempts: OPINION_BATCH_POLL_MAX_ATTEMPTS,
    timeoutMessage: '批量观点摘要仍在后台运行；重新进入本页可继续查看进度',
    failureMessage: '批量观点摘要失败',
    cancelConfirm: {
      message: '已生成的摘要会保留，未开始的标的不再处理。',
      title: '终止批量观点摘要'
    },
    cancelledMessage: '批量观点摘要已终止，已生成的摘要已保留',
    onUpdate: (current, previous) => {
      if (Number(current.completed || 0) > Number(previous?.completed || 0)) refreshSummaries()
    },
    onSuccess: async (finished) => {
      await refreshSummaries()
      const success = Number(finished.success_count || 0)
      const skipped = Number(finished.skipped_count || 0)
      const failed = Number(finished.failed_count || 0)
      const summary =
        `批量观点摘要完成：成功 ${success} 只` +
        (skipped ? `，跳过 ${skipped} 只` : '') +
        (failed ? `，失败 ${failed} 只` : '')
      if (!failed) ElMessage.success(summary)
      else if (success > 0) ElMessage.warning(summary)
      else ElMessage.error('批量观点摘要全部失败，请检查 LLM 配置后重试')
    },
    onCancelled: async () => {
      await refreshSummaries()
    }
  })

  const targetCount = computed(() => targets.value?.length ?? null)

  async function loadTargets(): Promise<OpinionBatchTarget[]> {
    const response = await api.getOpinionBatchTargets()
    const list = (response.data?.targets || []) as OpinionBatchTarget[]
    if (!isUnmounted()) targets.value = list
    return list
  }

  async function adoptActiveJob() {
    try {
      const response = await api.listActiveAnalysisJobs()
      const active = (response.data as OpinionBatchJob[]).find(
        (item) => item.type === 'opinion_summary_batch'
      )
      if (active?.id && !isUnmounted()) {
        adopt(active)
        await watchJob(active.id)
      }
    } catch {
      // 恢复进度失败静默：不影响页面主流程
    }
  }

  async function generateAll(force = false) {
    starting.value = true
    try {
      const list = await loadTargets()
      if (isUnmounted()) return
      if (!list.length) {
        ElMessage.info('近期关注作者未提及任何持仓/自选标的，没有可摘要的目标')
        return
      }
      const matchedTotal = list.reduce((sum, item) => sum + (item.matched_count || 0), 0)
      try {
        await ElMessageBox.confirm(
          `将对 ${list.length} 只有观点提及的标的（覆盖 ${matchedTotal} 条发言）` +
            `逐个调用 LLM 生成观点摘要，每标的约一次调用。\n` +
            `摘要仍新鲜（24 小时内或无新发言）的标的会自动跳过；` +
            `任务在后台运行，关闭页面不会中断。`,
          '批量生成观点标签',
          { type: 'warning', confirmButtonText: '开始生成', cancelButtonText: '取消' }
        )
      } catch {
        return // 用户取消
      }
      const response = await api.startOpinionBatchJob(force ? { force: true } : undefined)
      if (isUnmounted()) return
      adopt(response.data as OpinionBatchJob)
      starting.value = false
      await watchJob(response.data.id)
    } catch (error) {
      if (isUnmounted()) return
      ElMessage.error(getApiErrorMessage(error, '批量观点摘要启动失败'))
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
    requestCancel,
    stopWatching,
    targetCount,
    adoptActiveJob,
    generateAll
  })
}
