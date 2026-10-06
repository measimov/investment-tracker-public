import { beforeEach, expect, it, vi } from 'vitest'
import api from '@/api'
import { showApiError } from '@/utils/showApiError'
import { useBrokerAccounts } from './useBrokerAccounts'

vi.mock('@/api', () => ({ default: { getBrokerAccounts: vi.fn() } }))
vi.mock('@/utils/showApiError', () => ({ showApiError: vi.fn() }))

beforeEach(() => vi.clearAllMocks())

it('慢请求和失败不能伪报删除，重试成功后按实际列表判断', async () => {
  let reject!: (error: Error) => void
  vi.mocked(api.getBrokerAccounts).mockReturnValueOnce(
    new Promise((_, rejectRequest) => (reject = rejectRequest))
  )
  const accounts = useBrokerAccounts()
  const pending = accounts.load()
  expect(accounts.label(8)).toBe('账户加载中')
  reject(new Error('账户接口暂不可用'))
  await pending
  expect(accounts.label(8)).toBe('账户暂不可用')
  expect(showApiError).toHaveBeenCalledOnce()

  vi.mocked(api.getBrokerAccounts).mockResolvedValueOnce({
    data: [{ id: 8, account_name: 'IBKR U12345678' }]
  } as never)
  await accounts.load()
  expect(api.getBrokerAccounts).toHaveBeenLastCalledWith({ limit: 1000 })
  expect(accounts.label(8)).toBe('IBKR U***5678')
  expect(accounts.label(9)).toBe('已删除账户')
})

it('刷新失败保留已知名称，各页面列表状态独立', async () => {
  vi.mocked(api.getBrokerAccounts).mockResolvedValueOnce({
    data: [{ id: 8, account_name: '现有账户' }]
  } as never)
  const first = useBrokerAccounts()
  const second = useBrokerAccounts()
  await first.load()
  vi.mocked(api.getBrokerAccounts).mockRejectedValueOnce(new Error('刷新失败'))
  await first.load()
  expect(first.label(8)).toBe('现有账户')
  expect(first.label(9)).toBe('账户暂不可用')
  expect(second.label(8)).toBe('账户加载中')
})
