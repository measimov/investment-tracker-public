import { expect, it, vi } from 'vitest'
import api from '@/api'
import { useSecurityProfile } from './useSecurityProfile'

vi.mock('@/api', () => ({
  default: {
    getSecurityAnalysis: vi.fn(),
    getSecurityProfile: vi.fn(),
    watchlistContains: vi.fn().mockResolvedValue({ data: { watching: false } })
  }
}))
vi.mock('@/utils/showApiError', () => ({ showApiError: vi.fn() }))

function deferred() {
  let resolve!: (value: unknown) => void
  const promise = new Promise((done) => (resolve = done))
  return { promise, resolve }
}

it('分析与档案独立加载，旧路由响应不能提前结束新路由的加载态', async () => {
  const oldAnalysis = deferred()
  const newAnalysis = deferred()
  const newProfile = deferred()
  vi.mocked(api.getSecurityAnalysis)
    .mockReturnValueOnce(oldAnalysis.promise as never)
    .mockReturnValueOnce(newAnalysis.promise as never)
  vi.mocked(api.getSecurityProfile)
    .mockResolvedValueOnce({ data: { business: { profile: { 商业模式: '旧档案' } } } } as never)
    .mockReturnValueOnce(newProfile.promise as never)
  let symbol = 'A'
  const profile = useSecurityProfile({
    market: () => 'A股',
    symbol: () => symbol,
    isUnmounted: () => false
  })
  profile.init()
  await vi.waitFor(() => expect(profile.state.profileLoading).toBe(false))
  expect(profile.state.analysisLoading).toBe(true)
  expect(profile.state.business.profile.商业模式).toBe('旧档案')

  symbol = 'B'
  profile.resetAndReload()
  oldAnalysis.resolve({ data: { symbol: 'A' } })
  await oldAnalysis.promise
  expect(profile.state.analysisLoading).toBe(true)
  expect(profile.state.profileLoading).toBe(true)
  expect(profile.state.analysis).toBeNull()

  newAnalysis.resolve({ data: { symbol: 'B' } })
  await vi.waitFor(() => expect(profile.state.analysisLoading).toBe(false))
  expect(profile.state.profileLoading).toBe(true)
  newProfile.resolve({ data: {} })
  await vi.waitFor(() => expect(profile.state.profileLoading).toBe(false))
})

it('404 is known absent analysis while service failure remains unknown and read-only retry can recover', async () => {
  vi.mocked(api.getSecurityAnalysis)
    .mockRejectedValueOnce({ response: { status: 503, data: { detail: '分析暂不可用' } } })
    .mockRejectedValueOnce({ response: { status: 404 } })
  vi.mocked(api.getSecurityProfile).mockRejectedValueOnce({ response: { status: 503 } })
  const profile = useSecurityProfile({
    market: () => 'A股',
    symbol: () => 'UI',
    isUnmounted: () => false
  })
  profile.init()
  await vi.waitFor(() => expect(profile.state.analysisLoading).toBe(false))
  expect(profile.state.analysisHasLoaded).toBe(false)
  expect(profile.state.analysisError).toBe('分析暂不可用')
  expect(profile.state.profileHasLoaded).toBe(false)
  expect(profile.state.profileError).not.toBe('')
  await profile.retryAnalysis()
  expect(profile.state.analysisHasLoaded).toBe(true)
  expect(profile.state.analysis).toBeNull()
  expect(profile.state.analysisError).toBe('')
})

it('same-security refresh failure retains known data while stale failures cannot label the next security', async () => {
  const oldFailure = deferred()
  vi.mocked(api.getSecurityAnalysis)
    .mockResolvedValueOnce({ data: { name: '虚构甲' } } as never)
    .mockRejectedValueOnce({ response: { status: 503 } })
    .mockReturnValueOnce(oldFailure.promise as never)
    .mockResolvedValueOnce({ data: { name: '虚构乙' } } as never)
  vi.mocked(api.getSecurityProfile).mockResolvedValue({ data: {} } as never)
  let symbol = 'A'
  const profile = useSecurityProfile({
    market: () => 'A股',
    symbol: () => symbol,
    isUnmounted: () => false
  })
  profile.init()
  await vi.waitFor(() => expect(profile.state.analysisLoading).toBe(false))
  await profile.retryAnalysis()
  expect(profile.state.analysis?.name).toBe('虚构甲')
  expect(profile.state.analysisError).not.toBe('')
  const pending = profile.retryAnalysis()
  symbol = 'B'
  profile.resetAndReload()
  await vi.waitFor(() => expect(profile.state.analysis?.name).toBe('虚构乙'))
  oldFailure.resolve(Promise.reject({ response: { status: 503 } }))
  await pending
  expect(profile.state.analysis?.name).toBe('虚构乙')
  expect(profile.state.analysisError).toBe('')
})
