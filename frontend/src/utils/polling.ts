/**
 * Poll a background job until it reaches a terminal state.
 */

export interface BackgroundJob {
  status?: string
  error?: string | null
  result?: { error?: string | null } | null
  [key: string]: unknown
}

export interface PollJobOptions {
  /** Delay between polls (default 2000ms) */
  intervalMs?: number
  /** Maximum poll attempts before giving up */
  maxAttempts?: number
  /** Return true to abort polling (e.g. component unmounted) */
  isCancelled?: () => boolean
  /** Called with the job after every poll */
  onUpdate?: ((job: BackgroundJob) => void) | null
  /** Error message when attempts are exhausted */
  timeoutMessage?: string
  /** Fallback error message when the job fails */
  failureMessage?: string
  /**
   * 连续几次**取状态请求本身**失败（断网、部署窗口的 502/503/504）才放弃。
   * 任务在后台照跑；此前一次抖动就让数小时批量任务的进度卡报错停更（#219）。
   */
  maxConsecutiveFetchErrors?: number
  /** A terminal failed job may still contain useful partial results. Opt in per caller. */
  acceptFailedResult?: (job: BackgroundJob) => boolean
}

export class PollingTimeoutError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'PollingTimeoutError'
  }
}

/** 取状态请求的失败是否值得重试：无响应/超时/5xx 是暂态，4xx（任务不存在、无权限）不是 */
function isTransientFetchError(error: unknown): boolean {
  const err = error as { response?: { status?: number }; code?: string } | null
  const status = err?.response?.status
  if (status === undefined) return true
  return status >= 500
}

/**
 * @returns The completed job, or null when cancelled
 */
export async function pollJobUntilDone(
  fetchJob: () => Promise<{ data: BackgroundJob }>,
  options: PollJobOptions = {}
): Promise<BackgroundJob | null> {
  const {
    intervalMs = 2000,
    maxAttempts = 240,
    isCancelled = () => false,
    onUpdate = null,
    timeoutMessage = '任务仍在后台运行，请稍后查看',
    failureMessage = '后台任务失败',
    maxConsecutiveFetchErrors = 5
  } = options

  let consecutiveFetchErrors = 0

  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (isCancelled()) return null

    let response: { data: BackgroundJob }
    try {
      response = await fetchJob()
      consecutiveFetchErrors = 0
    } catch (error) {
      consecutiveFetchErrors += 1
      if (!isTransientFetchError(error) || consecutiveFetchErrors >= maxConsecutiveFetchErrors) {
        throw error
      }
      await new Promise((resolve) => setTimeout(resolve, intervalMs))
      continue
    }
    const job = response.data
    onUpdate?.(job)

    if (isCancelled()) return null

    if (job.status === 'succeeded') {
      return job
    }

    if (job.status === 'failed' || job.status === 'interrupted') {
      if (job.status === 'failed' && options.acceptFailedResult?.(job)) return job
      throw new Error(job.error || job.result?.error || failureMessage)
    }

    await new Promise((resolve) => setTimeout(resolve, intervalMs))
  }

  throw new PollingTimeoutError(timeoutMessage)
}
