/**
 * 批量财报摘要回填 feature（issue #140）：商业画像与"财报要点"的原料线，
 * 可重复触发续跑加深至十年。与批量分析共用 useBatchJobProgress 状态机，
 * 这里只留回填特有的：预览确认框与完成汇总文案。
 */

import { reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'
import { useBatchJobProgress } from '@/composables/useBatchJobProgress'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { BATCH_POLL_INTERVAL_MS, BATCH_POLL_MAX_ATTEMPTS } from './useBatchAnalysis'
import type { DigestBatchJob } from './types'

const DIGEST_STATUS_LABELS: Record<string, string> = {
  queued: '财报摘要回填排队中',
  running: '财报摘要回填进行中',
  succeeded: '财报摘要回填完成',
  failed: '财报摘要回填失败',
  interrupted: '财报摘要回填已终止'
}

export function useDigestBackfill({ isUnmounted }: { isUnmounted: () => boolean }) {
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
  } = useBatchJobProgress<DigestBatchJob>({
    fetchJob: (jobId) => api.getDigestBackfillJob(jobId),
    cancelJob: (jobId) => api.cancelDigestBackfillJob(jobId),
    statusLabels: DIGEST_STATUS_LABELS,
    isUnmounted,
    pollIntervalMs: BATCH_POLL_INTERVAL_MS,
    pollMaxAttempts: BATCH_POLL_MAX_ATTEMPTS,
    timeoutMessage: '回填仍在后台运行；重新进入持仓页可继续查看进度',
    failureMessage: '批量回填失败',
    cancelConfirm: {
      message: '已生成的摘要会保留，未开始的标的不再处理。当前标的会先跑完再停止。',
      title: '终止财报摘要回填'
    },
    cancelledMessage: '回填已终止，已生成的摘要已保留，可再次触发续跑',
    onSuccess: (job) => {
      const generated = Number(job.digests_generated || 0)
      const blocked = Number(job.digests_blocked || 0)
      const remaining = Number(job.symbols_with_remaining || 0)
      const failed = Number(job.failed_count || 0)
      const statementsGenerated = Number(job.statements_generated || 0)
      const statementsBlocked = Number(job.statements_blocked || 0)
      const statementsSuspect = Number(job.statements_suspect || 0)
      let summary = `财报摘要回填完成：新生成 ${generated} 份`
      if (remaining) summary += `；${remaining} 只标的还有更早年份可补，再次点击可继续加深`
      if (blocked) summary += `；${blocked} 份报告已永久失败（多次重试仍无法下载或摘要）`
      if (failed) summary += `；${failed} 只标的失败`
      if (statementsGenerated || statementsBlocked || statementsSuspect) {
        summary += `；港股报表新抽 ${statementsGenerated} 份`
        if (statementsBlocked) summary += `、${statementsBlocked} 份永久失败`
        if (statementsSuspect) summary += `、${statementsSuspect} 期校验存疑`
      }
      // 有永久失败也不能弹绿：绿色 + "新生成 0 份" 会让用户以为一切正常
      if (!failed && !blocked) ElMessage.success(summary)
      else ElMessage.warning(summary)
    }
  })

  async function backfillAll() {
    starting.value = true
    // 预览是纯 DB 统计（不打外网），失败时如实说取不到，不用本地估算凑数——
    // 本地根本不知道每个标的已有几份摘要
    let preview: {
      targets_total: number
      targets_without_digest: number
      per_symbol_budget: number
    }
    try {
      preview = (await api.getDigestBackfillPreview()).data
    } catch (error) {
      if (!isUnmounted()) {
        starting.value = false
        ElMessage.error(getApiErrorMessage(error, '获取回填预览失败'))
      }
      return
    }
    if (isUnmounted()) return
    if (!preview.targets_total) {
      starting.value = false
      ElMessage.info('当前没有可回填的持仓标的（A股/美股/港股）')
      return
    }
    try {
      await ElMessageBox.confirm(
        `将为 ${preview.targets_total} 只持仓标的下载财报原文并生成 AI 摘要` +
          `（其中 ${preview.targets_without_digest} 只目前一份摘要都没有）。\n` +
          `每只本轮最多补 ${preview.per_symbol_budget} 份（新→旧）；已有的期数自动跳过，` +
          `再次点击本按钮可继续向更早年份加深，直至十年补满。\n` +
          `预计每只 2-8 分钟（下载与解析 PDF 为主），任务在后台运行，关闭页面不会中断。`,
        '补齐财报摘要',
        { type: 'warning', confirmButtonText: '开始回填', cancelButtonText: '取消' }
      )
    } catch {
      starting.value = false
      return // 用户取消
    }

    try {
      const response = await api.startDigestBackfillJob()
      if (isUnmounted()) return
      adopt(response.data as DigestBatchJob)
      starting.value = false
      await watchJob(response.data.id)
    } catch (error) {
      if (isUnmounted()) return
      ElMessage.error(getApiErrorMessage(error, '批量回填启动失败'))
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
    backfillAll
  })
}

export type DigestBackfillFeature = ReturnType<typeof useDigestBackfill>
