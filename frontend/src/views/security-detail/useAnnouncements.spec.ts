/**
 * 公告 tab：筛选参数、翻页游标、跨标的/筛选切换的所有权、失败不抛。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api', () => ({
  default: {
    getSecurityAnnouncements: vi.fn()
  }
}))

import api from '@/api'
import { useAnnouncements } from './useAnnouncements'

const mocked = api as unknown as { getSecurityAnnouncements: ReturnType<typeof vi.fn> }

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((res) => {
    resolve = res
  })
  return { promise, resolve }
}

const group = (key: string, annDate: string) => ({ group_key: key, ann_date: annDate })
const body = (groups: unknown[], hasMore = false) => ({
  data: {
    groups,
    has_more: hasMore,
    sync_status: 'synced',
    last_synced: '2026-09-29',
    unsupported_reason: null
  }
})

function build() {
  const context = { symbol: '600298' }
  const feed = useAnnouncements({
    symbol: () => context.symbol,
    market: () => 'A股',
    isUnmounted: () => false
  })
  return { feed, context }
}

afterEach(() => vi.clearAllMocks())

describe('useAnnouncements', () => {
  it('首屏带筛选参数，翻页按最后一组公告日', async () => {
    mocked.getSecurityAnnouncements
      .mockResolvedValueOnce(body([group('a', '2026-09-25'), group('b', '2026-09-20')], true))
      .mockResolvedValueOnce(body([group('c', '2026-09-01')]))
    const { feed } = build()
    feed.state.category = 'financing'
    await feed.load()
    expect(mocked.getSecurityAnnouncements).toHaveBeenLastCalledWith('A股', '600298', {
      importance: 'normal',
      category: 'financing',
      before: undefined,
      limit: 30
    })
    expect(feed.state.hasMore).toBe(true)
    expect(feed.state.sync?.sync_status).toBe('synced')
    await feed.loadMore()
    expect(mocked.getSecurityAnnouncements.mock.calls[1][2].before).toBe('2026-09-20')
    expect(feed.state.groups.map((g) => g.group_key)).toEqual(['a', 'b', 'c'])
    expect(feed.state.hasMore).toBe(false)
  })

  it('换标的后旧响应不写入新状态', async () => {
    const stale = deferred<ReturnType<typeof body>>()
    mocked.getSecurityAnnouncements.mockReturnValueOnce(stale.promise)
    const { feed, context } = build()
    const first = feed.load()
    feed.reset()
    context.symbol = '00700'
    mocked.getSecurityAnnouncements.mockResolvedValueOnce(body([group('hk', '2026-09-28')]))
    await feed.load()
    stale.resolve(body([group('old', '2026-09-25')]))
    await first
    expect(feed.state.groups.map((g) => g.group_key)).toEqual(['hk'])
  })

  it('失败只记错误不抛', async () => {
    mocked.getSecurityAnnouncements.mockRejectedValueOnce(new Error('boom'))
    const { feed } = build()
    await feed.load()
    expect(feed.state.error).toBeTruthy()
    expect(feed.state.groups).toEqual([])
    expect(feed.state.loading).toBe(false)
  })
})
