import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('element-plus', () => ({
  ElMessage: { info: vi.fn(), error: vi.fn() },
  ElMessageBox: { confirm: vi.fn() }
}))
vi.mock('@/utils/showApiError', () => ({ showApiError: vi.fn() }))

import { ElMessage, ElMessageBox } from 'element-plus'
import { showApiError } from '@/utils/showApiError'
import { useBatchJobProgress, type BatchJobBase } from './useBatchJobProgress'

function setup(overrides: Partial<Parameters<typeof useBatchJobProgress>[0]> = {}) {
  const onSuccess = vi.fn()
  const state = useBatchJobProgress<BatchJobBase>({
    fetchJob: vi.fn(),
    cancelJob: vi.fn().mockResolvedValue({}),
    statusLabels: { running: '运行中', succeeded: '已完成', failed: '失败' },
    isUnmounted: () => false,
    pollIntervalMs: 1,
    pollMaxAttempts: 5,
    timeoutMessage: '超时',
    failureMessage: '失败了',
    cancelConfirm: { message: '确定终止？', title: '终止' },
    cancelledMessage: '已终止',
    onSuccess,
    ...overrides
  })
  return { state, onSuccess }
}

describe('useBatchJobProgress', () => {
  afterEach(() => {
    vi.clearAllMocks()
    vi.useRealTimers()
  })

  it('百分比钳制、状态文案与进度条状态', () => {
    const { state } = setup()
    state.adopt({ id: 'j', status: 'running', progress_percent: '130' })
    expect(state.percent.value).toBe(100)
    expect(state.isActive.value).toBe(true)
    expect(state.statusText.value).toBe('运行中')
    expect(state.progressStatus.value).toBeUndefined()
    state.adopt({ id: 'j', status: 'failed', progress_percent: -5 })
    expect(state.percent.value).toBe(0)
    expect(state.progressStatus.value).toBe('exception')
    state.adopt({ id: 'j', status: 'weird' })
    expect(state.statusText.value).toBe('运行中')
  })

  it('剩余时间：完成不足 2 个不估算，按平均耗时外推', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-30T10:10:00Z'))
    const { state } = setup()
    const started_at = '2026-09-30T10:00:00Z'
    state.adopt({ id: 'j', status: 'running', completed: 1, total: 10, started_at })
    expect(state.etaText.value).toBe('')
    state.adopt({ id: 'j', status: 'running', completed: 5, total: 10, started_at })
    expect(state.etaText.value).toBe('约 10 分钟')
    state.adopt({ id: 'j', status: 'running', completed: 2, total: 40, started_at })
    expect(state.etaText.value).toBe('约 3.2 小时')
    state.adopt({ id: 'j', status: 'succeeded', completed: 5, total: 10, started_at })
    expect(state.etaText.value).toBe('')
  })

  it('成功收尾交给 onSuccess', async () => {
    const fetchJob = vi.fn().mockResolvedValue({ data: { id: 'j', status: 'succeeded' } })
    const { state, onSuccess } = setup({ fetchJob })
    await state.watchJob('j')
    expect(onSuccess).toHaveBeenCalledWith(expect.objectContaining({ status: 'succeeded' }))
  })

  it('用户终止的任务收尾提示 info，不弹错误', async () => {
    const fetchJob = vi
      .fn()
      .mockResolvedValue({ data: { id: 'j', status: 'interrupted', cancelled: true } })
    const onCancelled = vi.fn()
    const { state, onSuccess } = setup({ fetchJob, onCancelled })
    await state.watchJob('j')
    expect(onSuccess).not.toHaveBeenCalled()
    expect(onCancelled).toHaveBeenCalled()
    expect(ElMessage.info).toHaveBeenCalledWith('已终止')
    expect(showApiError).not.toHaveBeenCalled()
  })

  it('失败弹错误；停止观察后不再弹', async () => {
    const fetchJob = vi.fn().mockResolvedValue({ data: { id: 'j', status: 'failed' } })
    const { state } = setup({ fetchJob })
    await state.watchJob('j')
    expect(showApiError).toHaveBeenCalledTimes(1)
    state.stopWatching()
    await state.watchJob('j')
    expect(showApiError).toHaveBeenCalledTimes(1)
    expect(state.job.value).toBeNull()
  })

  it('终止需确认；取消确认不发请求', async () => {
    const cancelJob = vi.fn().mockResolvedValue({})
    const { state } = setup({ cancelJob })
    state.adopt({ id: 'j', status: 'running' })
    vi.mocked(ElMessageBox.confirm).mockRejectedValueOnce('cancel')
    await state.requestCancel()
    expect(cancelJob).not.toHaveBeenCalled()
    vi.mocked(ElMessageBox.confirm).mockResolvedValueOnce('confirm' as never)
    await state.requestCancel()
    expect(cancelJob).toHaveBeenCalledWith('j')
  })
})
