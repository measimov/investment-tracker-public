/**
 * 检索取数层契约：缓存命中/过期/LRU、最新胜出的回调所有权、卸载与失败静默。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api', () => ({
  default: {
    searchSecurities: vi.fn(),
    resolveSecurity: vi.fn()
  }
}))

import api from '@/api'
import { SEARCH_CACHE_MAX, setSecuritySearchCacheScope } from '@/utils/securitySearchCache'
import { invalidateSecuritySearchCache, useSecuritySearch } from './useSecuritySearch'

const mocked = api as unknown as {
  searchSecurities: ReturnType<typeof vi.fn>
  resolveSecurity: ReturnType<typeof vi.fn>
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

function item(symbol: string) {
  return { symbol, market: '港股', name: symbol, origins: [] }
}

function build(
  overrides: { unmounted?: () => boolean; now?: () => number; market?: () => string | null } = {}
) {
  return useSecuritySearch({
    isUnmounted: overrides.unmounted ?? (() => false),
    now: overrides.now,
    market: overrides.market
  })
}

afterEach(() => {
  vi.clearAllMocks()
  setSecuritySearchCacheScope(null)
  invalidateSecuritySearchCache()
})

describe('useSecuritySearch.search', () => {
  it('同一查询命中缓存只打一次后端；invalidate 后重拉', async () => {
    mocked.searchSecurities.mockResolvedValue({ data: { items: [item('00700')] } })
    const s = build()
    expect(await s.search('txkg')).toEqual([item('00700')])
    expect(await s.search(' TXKG ')).toEqual([item('00700')]) // 归一化后同键
    expect(mocked.searchSecurities).toHaveBeenCalledTimes(1)
    expect(mocked.searchSecurities).toHaveBeenCalledWith({
      q: 'TXKG',
      market: undefined,
      limit: 20
    })

    invalidateSecuritySearchCache()
    await s.search('txkg')
    expect(mocked.searchSecurities).toHaveBeenCalledTimes(2)
  })

  it('市场限定进入缓存键；空查询也缓存（"我的标的"列表）', async () => {
    mocked.searchSecurities.mockResolvedValue({ data: { items: [] } })
    const s = build({ market: () => '港股' })
    await s.search('')
    await s.search('')
    expect(mocked.searchSecurities).toHaveBeenCalledTimes(1)
    expect(mocked.searchSecurities).toHaveBeenCalledWith({ q: '', market: '港股', limit: 20 })
  })

  it('TTL 过期后重拉', async () => {
    let clock = 0
    mocked.searchSecurities.mockResolvedValue({ data: { items: [] } })
    const s = build({ now: () => clock })
    await s.search('a')
    clock = 5 * 60_000 - 1
    await s.search('a')
    expect(mocked.searchSecurities).toHaveBeenCalledTimes(1)
    clock = 5 * 60_000 + 1
    await s.search('a')
    expect(mocked.searchSecurities).toHaveBeenCalledTimes(2)
  })

  it('LRU 上限：最旧的键被淘汰，刚命中过的保留', async () => {
    mocked.searchSecurities.mockResolvedValue({ data: { items: [] } })
    const s = build()
    for (let i = 0; i < SEARCH_CACHE_MAX; i += 1) await s.search(`k${i}`)
    await s.search('k0') // 命中：k0 变最新
    await s.search('new') // 超限：淘汰最旧的 k1
    const calls = mocked.searchSecurities.mock.calls.length
    await s.search('k0')
    expect(mocked.searchSecurities.mock.calls.length).toBe(calls)
    await s.search('k1')
    expect(mocked.searchSecurities.mock.calls.length).toBe(calls + 1)
  })

  it('两次重叠检索：先发后至的那次不得触发回调，最新一次必须触发', async () => {
    const slow = deferred<{ data: { items: unknown[] } }>()
    const fast = deferred<{ data: { items: unknown[] } }>()
    mocked.searchSecurities.mockReturnValueOnce(slow.promise).mockReturnValueOnce(fast.promise)
    const s = build()
    const callbacks: unknown[][] = []
    s.fetchSuggestions('0', (items) => callbacks.push(items))
    s.fetchSuggestions('00', (items) => callbacks.push(items))
    fast.resolve({ data: { items: [item('00700')] } })
    slow.resolve({ data: { items: [item('0001')] } })
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
    expect(callbacks).toEqual([[item('00700')]])
    expect(s.lastItems.value).toEqual([item('00700')])
    expect(s.loading.value).toBe(false)
  })

  it('组件已卸载：不回调、不写状态', async () => {
    let unmounted = false
    const pending = deferred<{ data: { items: unknown[] } }>()
    mocked.searchSecurities.mockReturnValue(pending.promise)
    const s = build({ unmounted: () => unmounted })
    const callback = vi.fn()
    s.fetchSuggestions('x', callback)
    unmounted = true
    pending.resolve({ data: { items: [item('X')] } })
    await Promise.resolve()
    await Promise.resolve()
    expect(callback).not.toHaveBeenCalled()
    expect(s.lastItems.value).toEqual([])
  })

  it('后端出错：回调空列表，不抛、不弹全局通知', async () => {
    mocked.searchSecurities.mockRejectedValue(new Error('500'))
    const s = build()
    const callback = vi.fn()
    s.fetchSuggestions('x', callback)
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
    expect(callback).toHaveBeenCalledWith([])
    expect(s.loading.value).toBe(false)
  })
})

describe('useSecuritySearch 身份隔离（评审 P1）', () => {
  it('A 的慢响应在切换到 B 后不得写缓存也不得回调；B 同键必须重新请求且只拿到 B 的条目', async () => {
    setSecuritySearchCacheScope('A')
    const slow = deferred<{ data: { items: unknown[] } }>()
    mocked.searchSecurities.mockReturnValueOnce(slow.promise)
    const a = build()
    const callbackA = vi.fn()
    a.fetchSuggestions('', callbackA)

    setSecuritySearchCacheScope('B') // A 登出、B 登录
    mocked.searchSecurities.mockResolvedValueOnce({ data: { items: [item('B1')] } })
    slow.resolve({ data: { items: [item('A1')] } })
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
    expect(callbackA).not.toHaveBeenCalled()
    expect(a.lastItems.value).toEqual([])

    const b = build()
    expect(await b.search('')).toEqual([item('B1')])
    expect(mocked.searchSecurities).toHaveBeenCalledTimes(2)
    expect(await b.search('')).toEqual([item('B1')]) // B 自己的缓存正常命中
    expect(mocked.searchSecurities).toHaveBeenCalledTimes(2)
  })

  it('解析缓存同样按身份隔离', async () => {
    setSecuritySearchCacheScope('A')
    mocked.resolveSecurity.mockResolvedValue({
      data: { symbol: '900926', market: 'B股', name: '宝信B' }
    })
    await build().resolve('900926', 'B股')
    setSecuritySearchCacheScope('B')
    await build().resolve('900926', 'B股')
    expect(mocked.resolveSecurity).toHaveBeenCalledTimes(2)
  })
})

describe('useSecuritySearch.resolve', () => {
  it('成功解析进缓存；解析不到（name=null）不缓存以便下次重试', async () => {
    mocked.resolveSecurity
      .mockResolvedValueOnce({
        data: { symbol: '900926', market: 'B股', name: '宝信B', currency: 'USD' }
      })
      .mockResolvedValueOnce({ data: { symbol: '999999', market: 'A股', name: null, error: 'x' } })
      .mockResolvedValueOnce({ data: { symbol: '999999', market: 'A股', name: null, error: 'x' } })
    const s = build()
    expect((await s.resolve(' 900926', 'B股'))?.name).toBe('宝信B')
    expect((await s.resolve('900926', 'B股'))?.name).toBe('宝信B')
    expect(mocked.resolveSecurity).toHaveBeenCalledTimes(1)
    expect(mocked.resolveSecurity).toHaveBeenCalledWith({ symbol: '900926', market: 'B股' })
    expect((await s.resolve('999999', 'A股'))?.name).toBeNull()
    await s.resolve('999999', 'A股')
    expect(mocked.resolveSecurity).toHaveBeenCalledTimes(3)
  })

  it('缺代码或市场不外呼；出错返回 null', async () => {
    const s = build()
    expect(await s.resolve('', 'A股')).toBeNull()
    expect(await s.resolve('600519', '')).toBeNull()
    expect(mocked.resolveSecurity).not.toHaveBeenCalled()
    mocked.resolveSecurity.mockRejectedValue(new Error('boom'))
    expect(await s.resolve('600519', 'A股')).toBeNull()
  })
})
