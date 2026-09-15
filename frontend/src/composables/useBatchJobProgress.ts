import { computed, ref, type Ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { pollJobUntilDone, type BackgroundJob } from '../utils/polling'
import { getApiErrorMessage } from '../utils/apiErrors'

/** 批量 job 的公共字段（各 job 的差异字段经索引签名透传） */
export interface BatchJobBase {
  id?: string
  type?: string
  status?: string
  total?: number
  completed?: number
  progress_percent?: number | string | null
  success_count?: number
  failed_count?: number
  current_symbol?: string | null
  current_market?: string | null
  cancelled?: boolean
  abort_reason?: string | null
  started_at?: string | null
  [key: string]: unknown
}

export interface BatchJobProgressOptions<T extends BatchJobBase> {
  /** 轮询单个 job */
  fetchJob: (jobId: string) => Promise<{ data: BackgroundJob }>
  /** 请求终止 job */
  cancelJob: (jobId: string) => Promise<unknown>
  /** 状态 → 文案（queued/running/succeeded/failed/interrupted；缺省回退 running 文案） */
  statusLabels: Record<string, string>
  /** 组件卸载谓词（useAliveGuard().isUnmounted） */
  isUnmounted: () => boolean
  pollIntervalMs: number
  pollMaxAttempts: number
  timeoutMessage: string
  failureMessage: string
  /** 终止确认框 */
  cancelConfirm: { message: string; title: string }
  /** cancelled 收尾的提示文案（info 级；用户主动行为不弹红） */
  cancelledMessage: string
  /** 每次轮询更新后的差异化钩子（如批量分析的"完成一只刷一次标签列"） */
  onUpdate?: (job: T, previous: T | null) => void
  /** 成功收尾（各 job 的汇总消息差异很大，整体交回调用方） */
  onSuccess: (job: T) => void | Promise<void>
  /** cancelled 收尾里、提示之前要做的事（可选，如刷新标签列） */
  onCancelled?: () => void | Promise<void>
}

/**
 * 批量后台 job 的进度状态机（issue #139）。
 *
 * 此前批量分析与财报回填在 Holdings.vue 里同构克隆：percent 钳制、
 * progressStatus、状态 label 表、13 行 ETA 计算、watch/cancel/stop 三件套
 * 各存两份——后端 ANALYSIS_EXCLUSIVE_JOB_TYPES 已是四向互斥，第三个批量
 * job 进来前先把骨架抽走。差异化逻辑（启动预览/确认框/汇总消息）留在视图。
 */
export function useBatchJobProgress<T extends BatchJobBase>(options: BatchJobProgressOptions<T>) {
  const job = ref<T | null>(null) as Ref<T | null>
  const starting = ref(false)
  const watchStopped = ref(false)

  const isActive = computed(() => job.value?.status === 'queued' || job.value?.status === 'running')

  const percent = computed(() =>
    Math.max(0, Math.min(100, Math.round(Number(job.value?.progress_percent || 0))))
  )

  const progressStatus = computed(() => {
    const status = job.value?.status
    if (status === 'failed') return 'exception'
    if (status === 'interrupted') return 'warning'
    if (status === 'succeeded') return 'success'
    return undefined
  })

  const statusText = computed(
    () => options.statusLabels[String(job.value?.status || '')] || options.statusLabels.running
  )

  // 剩余时间估计：数小时的任务没有 ETA，用户读不出"还要多久"与"是不是卡死了"
  const etaText = computed(() => {
    const current = job.value
    if (!current || !isActive.value) return ''
    const completed = Number(current.completed || 0)
    const total = Number(current.total || 0)
    if (completed < 2 || total <= completed || !current.started_at) return ''
    const elapsedMs = Date.now() - Date.parse(current.started_at)
    if (!Number.isFinite(elapsedMs) || elapsedMs <= 0) return ''
    const remainingMinutes = Math.round(((elapsedMs / completed) * (total - completed)) / 60000)
    if (remainingMinutes < 1) return '不到 1 分钟'
    if (remainingMinutes < 90) return `约 ${remainingMinutes} 分钟`
    return `约 ${(remainingMinutes / 60).toFixed(1)} 小时`
  })

  /** 接管一个已在运行的 job（启动后或恢复活跃任务时） */
  function adopt(activeJob: T) {
    watchStopped.value = false
    job.value = activeJob
  }

  async function watchJob(jobId: string) {
    try {
      const finished = await pollJobUntilDone(() => options.fetchJob(jobId), {
        intervalMs: options.pollIntervalMs,
        maxAttempts: options.pollMaxAttempts,
        isCancelled: () => options.isUnmounted() || watchStopped.value,
        onUpdate: (polled) => {
          if (options.isUnmounted() || watchStopped.value) return
          const previous = job.value
          job.value = polled as T
          options.onUpdate?.(polled as T, previous)
        },
        timeoutMessage: options.timeoutMessage,
        failureMessage: options.failureMessage
      })
      if (!finished || options.isUnmounted()) return
      await options.onSuccess(finished as T)
    } catch (error) {
      if (options.isUnmounted() || watchStopped.value) return
      // 终止是用户主动行为，不当作错误弹红
      if (job.value?.cancelled) {
        await options.onCancelled?.()
        ElMessage.info(options.cancelledMessage)
        return
      }
      ElMessage.error(getApiErrorMessage(error, options.failureMessage))
    }
  }

  async function requestCancel() {
    const jobId = job.value?.id
    if (!jobId) return
    try {
      await ElMessageBox.confirm(options.cancelConfirm.message, options.cancelConfirm.title, {
        type: 'warning',
        confirmButtonText: '终止',
        cancelButtonText: '继续运行'
      })
    } catch {
      return
    }
    try {
      await options.cancelJob(jobId)
      if (!options.isUnmounted()) ElMessage.info('已请求终止，当前标的完成后停止')
    } catch (error) {
      if (!options.isUnmounted()) ElMessage.error(getApiErrorMessage(error, '终止失败'))
    }
  }

  function stopWatching() {
    watchStopped.value = true
    job.value = null
  }

  return {
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
  }
}
