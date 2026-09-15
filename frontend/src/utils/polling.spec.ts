// 后台任务轮询（#142：此前完全无测试）。终态判定、取消语义（两处检查点）、
// 失败消息优先级与超时都是行为契约——批量分析/回填/历史同步全都压在它上面。
// intervalMs 传 0：真实定时器零延迟，不引入 fake timers 的复杂度。
import { describe, expect, test, vi } from 'vitest'
import { pollJobUntilDone, type BackgroundJob } from './polling'

function fetchSequence(jobs: BackgroundJob[]) {
  let index = 0
  return vi.fn(async () => ({ data: jobs[Math.min(index++, jobs.length - 1)] }))
}

describe('pollJobUntilDone', () => {
  test('polls until succeeded and returns the final job', async () => {
    const fetchJob = fetchSequence([
      { status: 'running' },
      { status: 'running' },
      { status: 'succeeded', success_count: 3 }
    ])
    const job = await pollJobUntilDone(fetchJob, { intervalMs: 0 })
    expect(job?.status).toBe('succeeded')
    expect(fetchJob).toHaveBeenCalledTimes(3)
  })

  test('onUpdate sees every polled job', async () => {
    const onUpdate = vi.fn()
    await pollJobUntilDone(fetchSequence([{ status: 'running' }, { status: 'succeeded' }]), {
      intervalMs: 0,
      onUpdate
    })
    expect(onUpdate).toHaveBeenCalledTimes(2)
    expect(onUpdate.mock.calls[0][0].status).toBe('running')
  })

  test('failure message priority: job.error > result.error > failureMessage', async () => {
    await expect(
      pollJobUntilDone(fetchSequence([{ status: 'failed', error: '顶层错误' }]), { intervalMs: 0 })
    ).rejects.toThrow('顶层错误')
    await expect(
      pollJobUntilDone(fetchSequence([{ status: 'failed', result: { error: '结果错误' } }]), {
        intervalMs: 0
      })
    ).rejects.toThrow('结果错误')
    await expect(
      pollJobUntilDone(fetchSequence([{ status: 'interrupted' }]), {
        intervalMs: 0,
        failureMessage: '兜底失败文案'
      })
    ).rejects.toThrow('兜底失败文案')
  })

  test('cancellation before the first fetch returns null without requesting', async () => {
    const fetchJob = fetchSequence([{ status: 'running' }])
    const job = await pollJobUntilDone(fetchJob, { intervalMs: 0, isCancelled: () => true })
    expect(job).toBeNull()
    expect(fetchJob).not.toHaveBeenCalled()
  })

  test('cancellation after a fetch still returns null (unmount mid-flight)', async () => {
    let cancelled = false
    const fetchJob = vi.fn(async () => {
      cancelled = true // 请求在飞期间组件卸载
      return { data: { status: 'succeeded' } as BackgroundJob }
    })
    const job = await pollJobUntilDone(fetchJob, { intervalMs: 0, isCancelled: () => cancelled })
    expect(job).toBeNull()
    expect(fetchJob).toHaveBeenCalledTimes(1)
  })

  test('exhausting maxAttempts throws the timeout message', async () => {
    const fetchJob = fetchSequence([{ status: 'running' }])
    await expect(
      pollJobUntilDone(fetchJob, { intervalMs: 0, maxAttempts: 3, timeoutMessage: '仍在后台运行' })
    ).rejects.toThrow('仍在后台运行')
    expect(fetchJob).toHaveBeenCalledTimes(3)
  })
})
