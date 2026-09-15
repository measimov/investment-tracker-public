/**
 * 评审 P2 回归：动态流的 in-flight 防重复与跨标的所有权。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api', () => ({
  default: {
    getOpinionFeed: vi.fn()
  }
}))

import api from '@/api'
import { useOpinionFeed } from './useOpinionFeed'

const mocked = api as unknown as { getOpinionFeed: ReturnType<typeof vi.fn> }

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

function build(symbol = '600519') {
  const context = { symbol }
  const feed = useOpinionFeed({
    symbol: () => context.symbol,
    market: () => 'A股',
    isUnmounted: () => false
  })
  return { feed, context }
}

afterEach(() => vi.clearAllMocks())

describe('useOpinionFeed', () => {
  it('同一上下文 in-flight 期间重复展开不重复扫描', async () => {
    const pending = deferred<{ data: { authors: [] } }>()
    mocked.getOpinionFeed.mockReturnValue(pending.promise)

    const { feed } = build()
    const first = feed.loadFeed()
    await feed.loadFeed() // 收起再展开
    await feed.loadFeed()
    expect(mocked.getOpinionFeed).toHaveBeenCalledTimes(1)

    pending.resolve({ data: { authors: [] } })
    await first
    expect(feed.loaded.value).toBe(true)
    expect(feed.loading.value).toBe(false)
  })

  it('切标的后旧响应不得写数据、也不得清掉新请求的 loading', async () => {
    const pollA = deferred<{ data: { authors: Array<{ author: string }> } }>()
    const pollB = deferred<{ data: { authors: Array<{ author: string }> } }>()
    mocked.getOpinionFeed.mockReturnValueOnce(pollA.promise).mockReturnValueOnce(pollB.promise)

    const { feed, context } = build('600519')
    const runA = feed.loadFeed()

    // 路由切标的：reset 收回所有权，随后 B 展开
    context.symbol = '000001'
    feed.reset()
    const runB = feed.loadFeed()
    expect(feed.loading.value).toBe(true)

    // A 迟到归来：不得写 authors/loaded，finally 不得把 B 的 loading 清 false
    pollA.resolve({ data: { authors: [{ author: '旧标的作者' }] } })
    await runA
    expect(feed.loading.value).toBe(true)
    expect(feed.authors.value).toEqual([])
    expect(feed.loaded.value).toBe(false)

    pollB.resolve({ data: { authors: [{ author: '新标的作者' }] } })
    await runB
    expect(feed.loading.value).toBe(false)
    expect(feed.loaded.value).toBe(true)
    expect(feed.authors.value).toEqual([{ author: '新标的作者' }])
  })

  it('reset 后再展开会重新拉取（loaded 归零）', async () => {
    mocked.getOpinionFeed.mockResolvedValue({ data: { authors: [] } })
    const { feed } = build()
    await feed.loadFeed()
    expect(mocked.getOpinionFeed).toHaveBeenCalledTimes(1)
    feed.reset()
    await feed.loadFeed()
    expect(mocked.getOpinionFeed).toHaveBeenCalledTimes(2)
  })
})
