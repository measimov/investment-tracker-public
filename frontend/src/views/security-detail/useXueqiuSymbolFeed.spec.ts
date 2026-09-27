/**
 * 雪球公告/讨论：首次展开才拉、in-flight 防重复、跨标的所有权、失败不抛。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api', () => ({
  default: {
    getXueqiuSymbolFeed: vi.fn()
  }
}))

import api from '@/api'
import { useXueqiuSymbolFeed } from './useXueqiuSymbolFeed'

const mocked = api as unknown as { getXueqiuSymbolFeed: ReturnType<typeof vi.fn> }

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function build(symbol = '600519') {
  const context = { symbol }
  const feed = useXueqiuSymbolFeed({
    symbol: () => context.symbol,
    market: () => 'A股',
    isUnmounted: () => false
  })
  return { feed, context }
}

const post = (id: string) => ({ post_id: id, title: '', text: id, created_at_ms: 1 })

afterEach(() => vi.clearAllMocks())

describe('useXueqiuSymbolFeed', () => {
  it('收起不拉取，展开才拉取且 in-flight 不重复', async () => {
    const pending = deferred<{ data: unknown }>()
    mocked.getXueqiuSymbolFeed.mockReturnValue(pending.promise)
    const { feed } = build()

    feed.onToggle([])
    expect(mocked.getXueqiuSymbolFeed).not.toHaveBeenCalled()

    feed.onToggle(['xueqiu'])
    feed.onToggle(['xueqiu'])
    expect(mocked.getXueqiuSymbolFeed).toHaveBeenCalledTimes(1)
    expect(mocked.getXueqiuSymbolFeed).toHaveBeenCalledWith({
      symbol: '600519',
      market: 'A股',
      limit: 20
    })

    pending.resolve({
      data: {
        announcements: [post('a1')],
        discussions: [post('d1'), post('d2')],
        last_cycle_finished_at: '2026-09-27T00:00:00Z'
      }
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(feed.loaded.value).toBe(true)
    expect(feed.announcements.value).toHaveLength(1)
    expect(feed.discussions.value).toHaveLength(2)
    expect(feed.lastCycleFinishedAt.value).toBe('2026-09-27T00:00:00Z')
  })

  it('切标的后旧响应不得写数据', async () => {
    const first = deferred<{ data: unknown }>()
    const second = deferred<{ data: unknown }>()
    mocked.getXueqiuSymbolFeed
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
    const { feed, context } = build('600519')

    const runA = feed.load()
    feed.reset()
    context.symbol = '000858'
    const runB = feed.load()

    first.resolve({ data: { announcements: [post('old')], discussions: [] } })
    await runA
    expect(feed.announcements.value).toEqual([])
    expect(feed.loading.value).toBe(true)

    second.resolve({ data: { announcements: [post('new')], discussions: [] } })
    await runB
    expect(feed.announcements.value.map((item) => item.post_id)).toEqual(['new'])
    expect(feed.loading.value).toBe(false)
  })

  it('失败只置 failed，允许再次展开重试', async () => {
    mocked.getXueqiuSymbolFeed.mockRejectedValueOnce(new Error('boom'))
    const { feed } = build()
    await feed.load()
    expect(feed.failed.value).toBe(true)
    expect(feed.loaded.value).toBe(false)

    mocked.getXueqiuSymbolFeed.mockResolvedValueOnce({
      data: { announcements: [], discussions: [] }
    })
    await feed.load()
    expect(feed.failed.value).toBe(false)
    expect(feed.loaded.value).toBe(true)
  })
})
