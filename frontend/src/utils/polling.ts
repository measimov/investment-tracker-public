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
    failureMessage = '后台任务失败'
  } = options

  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (isCancelled()) return null

    const response = await fetchJob()
    const job = response.data
    onUpdate?.(job)

    if (isCancelled()) return null

    if (job.status === 'succeeded') {
      return job
    }

    if (job.status === 'failed' || job.status === 'interrupted') {
      throw new Error(job.error || job.result?.error || failureMessage)
    }

    await new Promise((resolve) => setTimeout(resolve, intervalMs))
  }

  throw new Error(timeoutMessage)
}
