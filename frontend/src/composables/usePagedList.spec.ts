/**
 * 分页列表契约（#284）：页码越界回退、旧请求晚到不覆盖新结果、失败只经 showApiError。
 */
import { describe, expect, it, vi } from 'vitest'

vi.mock('@/utils/showApiError', () => ({ showApiError: vi.fn() }))

import { showApiError } from '@/utils/showApiError'
import { usePagedList } from './usePagedList'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

describe('usePagedList', () => {
  it('当前页超出总页数时回到最后一页并重取', async () => {
    const calls: number[] = []
    const list = usePagedList<number>({
      pageSize: 10,
      failureMessage: '加载失败',
      fetchPage: async ({ skip }) => {
        calls.push(skip)
        // 第 3 页删掉了最后一条：总数只剩 20
        return { items: skip >= 20 ? [] : [skip], total: 20 }
      }
    })
    list.pagination.page = 3
    await list.load()
    expect(list.pagination.page).toBe(2)
    expect(calls).toEqual([20, 10])
    expect(list.items.value).toEqual([10])
    expect(list.loading.value).toBe(false)
  })

  it('旧请求晚到不覆盖新结果', async () => {
    const first = deferred<{ items: string[]; total: number }>()
    const second = deferred<{ items: string[]; total: number }>()
    const pending = [first, second]
    const list = usePagedList<string>({
      failureMessage: '加载失败',
      fetchPage: () => pending.shift()!.promise
    })
    const a = list.load()
    const b = list.search()
    second.resolve({ items: ['新'], total: 1 })
    await b
    first.resolve({ items: ['旧'], total: 1 })
    await a
    expect(list.items.value).toEqual(['新'])
    expect(list.loading.value).toBe(false)
  })

  it('失败经 showApiError 提示，被取代的旧请求失败不提示', async () => {
    const list = usePagedList<number>({
      failureMessage: '加载失败',
      fetchPage: async () => {
        throw new Error('boom')
      }
    })
    await list.load()
    expect(showApiError).toHaveBeenCalledWith(expect.any(Error), '加载失败')
    expect(list.loading.value).toBe(false)
  })

  it('search 与 changePageSize 回到第一页，search 强制重取', async () => {
    const forces: boolean[] = []
    const list = usePagedList<number>({
      failureMessage: '加载失败',
      fetchPage: async (_page, request) => {
        forces.push(request.force)
        return { items: [], total: 0 }
      }
    })
    list.pagination.page = 4
    await list.search()
    expect(list.pagination.page).toBe(1)
    list.pagination.page = 2
    await list.changePageSize()
    expect(list.pagination.page).toBe(1)
    expect(forces).toEqual([true, false])
  })
})
