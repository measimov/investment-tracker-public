/**
 * 评审 P1 回归：成功生成后 generating/job 必须复位（此前 load() 递增世代
 * 计数导致 finally 里 isStale 恒真，UI 永久停在 loading）。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api', () => ({
  default: {
    getOpinionSummary: vi.fn(),
    startOpinionJob: vi.fn(),
    getOpinionJob: vi.fn()
  }
}))

import api from '@/api'
import { useOpinionSummary } from './useOpinionSummary'

const mocked = api as unknown as {
  getOpinionSummary: ReturnType<typeof vi.fn>
  startOpinionJob: ReturnType<typeof vi.fn>
  getOpinionJob: ReturnType<typeof vi.fn>
}

function build(symbol = '600519', market = 'A股') {
  const context = { symbol, market }
  const feature = useOpinionSummary({
    symbol: () => context.symbol,
    market: () => context.market,
    isUnmounted: () => false
  })
  return { ...feature, context }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('useOpinionSummary.generate', () => {
  it('成功完成后复位 generating/job 并载入新摘要', async () => {
    mocked.startOpinionJob.mockResolvedValue({ data: { id: 'job-1', status: 'queued' } })
    mocked.getOpinionJob.mockResolvedValue({ data: { id: 'job-1', status: 'succeeded' } })
    mocked.getOpinionSummary.mockResolvedValue({
      data: { id: 1, tags: ['偏多'], summary: '新摘要', author_stances: [], content: '## 全文' }
    })

    const { state, generate } = build()
    await generate()

    expect(state.generating).toBe(false)
    expect(state.job).toBeNull()
    expect(state.summary?.summary).toBe('新摘要')
    expect(state.error).toBe('')
  })

  it('生成中切换标的：新页面不残留旧任务的 loading 与进度', async () => {
    vi.useFakeTimers()
    const pollA = deferred<{ data: { id: string; status: string } }>()
    mocked.startOpinionJob.mockResolvedValue({ data: { id: 'job-A', status: 'queued' } })
    mocked.getOpinionJob.mockReturnValue(pollA.promise)
    mocked.getOpinionSummary.mockRejectedValue({ response: { status: 404 } })

    const { state, generate, load, context } = build('600519', 'A股')
    const generating = generate()
    await vi.advanceTimersByTimeAsync(0) // 进入轮询的首次 fetch
    expect(state.generating).toBe(true)

    // 模拟路由切到另一标的：watch 触发的 load 必须清掉旧任务的生成态
    context.symbol = '000001'
    await load()
    expect(state.generating).toBe(false)
    expect(state.job).toBeNull()

    // 旧任务稍后返回 running：不得把新页面重新写回 loading
    pollA.resolve({ data: { id: 'job-A', status: 'running' } })
    await vi.advanceTimersByTimeAsync(5000)
    await generating
    expect(state.generating).toBe(false)
    expect(state.job).toBeNull()
  })

  it('旧任务收尾不得清掉新标的上后启动的任务', async () => {
    vi.useFakeTimers()
    const pollA = deferred<{ data: { id: string; status: string } }>()
    const pollB = deferred<{ data: { id: string; status: string } }>()
    mocked.startOpinionJob
      .mockResolvedValueOnce({ data: { id: 'job-A', status: 'queued' } })
      .mockResolvedValueOnce({ data: { id: 'job-B', status: 'queued' } })
    mocked.getOpinionJob.mockReturnValueOnce(pollA.promise).mockReturnValueOnce(pollB.promise)
    mocked.getOpinionSummary.mockRejectedValue({ response: { status: 404 } })

    const { state, generate, load, context } = build('600519', 'A股')
    const runA = generate()
    await vi.advanceTimersByTimeAsync(0)

    context.symbol = '000001'
    await load()
    const runB = generate()
    await vi.advanceTimersByTimeAsync(0)
    expect(state.job?.id).toBe('job-B')

    // A 返回 running 并被取消收尾：B 的生成态必须原样保留
    pollA.resolve({ data: { id: 'job-A', status: 'running' } })
    await vi.advanceTimersByTimeAsync(2100)
    await runA
    expect(state.generating).toBe(true)
    expect(state.job?.id).toBe('job-B')

    pollB.resolve({ data: { id: 'job-B', status: 'succeeded' } })
    await vi.advanceTimersByTimeAsync(0)
    await runB
    expect(state.generating).toBe(false)
    expect(state.job).toBeNull()
  })

  it('409 预检以信息条呈现且同样复位 loading', async () => {
    mocked.startOpinionJob.mockRejectedValue({
      response: { status: 409, data: { detail: '雪球观点数据源未接入' } }
    })

    const { state, generate } = build()
    await generate()

    expect(state.generating).toBe(false)
    expect(state.notice).toContain('未接入')
    expect(state.error).toBe('')
  })
})
