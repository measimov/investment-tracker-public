/**
 * 评审 P1 回归：检索缓存必须绑定认证身份 + 失效代际；慢响应不得在失效后写回。
 * 评审 P2：失效判定集中于账本写端点分类。
 */
import { afterEach, describe, expect, it } from 'vitest'
import {
  SEARCH_CACHE_MAX,
  cacheEpoch,
  invalidateSecuritySearchCache,
  isEpochCurrent,
  isLedgerMutation,
  resolveCacheGet,
  resolveCacheSet,
  searchCacheGet,
  searchCacheSet,
  setSecuritySearchCacheScope
} from './securitySearchCache'
import type { SecuritySearchItem } from '@/types'

const item = (symbol: string) => ({ symbol, market: '港股' }) as SecuritySearchItem

afterEach(() => {
  setSecuritySearchCacheScope(null)
  invalidateSecuritySearchCache()
})

describe('scope / generation', () => {
  it('切换用户即换命名空间：A 的条目对 B 不可见，切回 A 也已清空', () => {
    setSecuritySearchCacheScope('A')
    expect(searchCacheSet('k', [item('A1')], 0, cacheEpoch())).toBe(true)
    expect(searchCacheGet('k', 0)).toEqual([item('A1')])
    setSecuritySearchCacheScope('B')
    expect(searchCacheGet('k', 0)).toBeNull()
    setSecuritySearchCacheScope('A')
    expect(searchCacheGet('k', 0)).toBeNull()
  })

  it('发起时抓取的 epoch 在失效/换用户后不再有效，慢响应不得写回', () => {
    setSecuritySearchCacheScope('A')
    const epochA = cacheEpoch()
    setSecuritySearchCacheScope('B')
    expect(isEpochCurrent(epochA)).toBe(false)
    expect(searchCacheSet('k', [item('A1')], 0, epochA)).toBe(false)
    expect(searchCacheGet('k', 0)).toBeNull()

    const epochB = cacheEpoch()
    invalidateSecuritySearchCache()
    expect(searchCacheSet('k', [item('B1')], 0, epochB)).toBe(false)
    expect(resolveCacheSet('r', { symbol: 'X', market: '港股' } as never, epochB)).toBe(false)
    expect(resolveCacheGet('r')).toBeNull()
    expect(searchCacheSet('k', [item('B2')], 0, cacheEpoch())).toBe(true)
  })

  it('同一用户重复 setScope 不清缓存；数字 id 与字符串 id 同义', () => {
    setSecuritySearchCacheScope(7)
    searchCacheSet('k', [item('X')], 0, cacheEpoch())
    setSecuritySearchCacheScope('7')
    expect(searchCacheGet('k', 0)).toEqual([item('X')])
  })

  it('TTL 与 LRU', () => {
    setSecuritySearchCacheScope('A')
    searchCacheSet('old', [item('O')], 0, cacheEpoch())
    expect(searchCacheGet('old', 5 * 60_000 + 1)).toBeNull()
    for (let i = 0; i < SEARCH_CACHE_MAX; i += 1) searchCacheSet(`k${i}`, [], 0, cacheEpoch())
    searchCacheGet('k0', 0) // 命中 → 变最新
    searchCacheSet('new', [], 0, cacheEpoch()) // 淘汰最旧的 k1
    expect(searchCacheGet('k0', 0)).not.toBeNull()
    expect(searchCacheGet('k1', 0)).toBeNull()
  })
})

describe('isLedgerMutation', () => {
  it.each([
    ['post', '/transactions', true],
    ['put', '/transactions/12', true],
    ['delete', '/transactions/12', true],
    ['post', '/transactions/transfer', true],
    ['post', '/watchlist', true],
    ['put', '/watchlist/3', true],
    ['delete', '/watchlist/3', true],
    ['post', '/import/csv', true],
    ['post', '/import/cmb-fund-flows', true],
    ['post', '/import/cmb-fund-flows/preview', false],
    ['delete', '/broker-accounts/1', true],
    ['post', '/holdings/prices/batch-update', true],
    ['get', '/transactions', false],
    ['get', '/watchlist', false],
    ['post', '/securities/analysis-jobs', false],
    ['post', '/auth/refresh', false],
    ['post', '/security-rules', false],
    ['post', 'http://127.0.0.1:18000/api/watchlist?x=1', true],
    [undefined, '/watchlist', false],
    ['post', undefined, false]
  ])('%s %s → %s', (method, url, expected) => {
    expect(isLedgerMutation(method as string | undefined, url as string | undefined)).toBe(expected)
  })
})
