// 请求参数 → 缓存键（#142）。键序稳定与空值剔除决定 store 缓存命中率：
// 同一组参数不同书写顺序必须得到同一个键。
import { describe, expect, test } from 'vitest'
import { paramsKey } from './cacheKey'

describe('paramsKey', () => {
  test('key order does not affect the key', () => {
    expect(paramsKey({ market: 'A股', symbol: '600000' })).toBe(
      paramsKey({ symbol: '600000', market: 'A股' })
    )
  })

  test('null/undefined/empty-string entries are dropped', () => {
    expect(paramsKey({ symbol: '', market: null, limit: undefined })).toBe('{}')
    expect(paramsKey()).toBe('{}')
  })

  test('falsy-but-meaningful values (0, false) survive', () => {
    expect(paramsKey({ skip: 0, active: false })).toBe('{"active":false,"skip":0}')
  })
})
