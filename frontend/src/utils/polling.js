/**
 * Poll a background job until it reaches a terminal state.
 *
 * @param {() => Promise<{data: object}>} fetchJob - Fetches the latest job state
 * @param {object} options
 * @param {number} options.intervalMs - Delay between polls (default 2000ms)
 * @param {number} options.maxAttempts - Maximum poll attempts before giving up
 * @param {() => boolean} options.isCancelled - Return true to abort polling (e.g. component unmounted)
 * @param {(job: object) => void} options.onUpdate - Called with the job after every poll
 * @param {string} options.timeoutMessage - Error message when attempts are exhausted
 * @param {string} options.failureMessage - Fallback error message when the job fails
 * @returns {Promise<object|null>} The completed job, or null when cancelled
 */
export async function pollJobUntilDone(fetchJob, options = {}) {
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
