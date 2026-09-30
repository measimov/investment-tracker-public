/**
 * #268：持仓/交易缓存不得跨用户、跨账本写入存活；#286：服务不可用不等于未登录。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import {
  dataEpoch,
  emitLedgerEvent,
  isDataEpochCurrent,
  onLedgerEvent
} from '../utils/ledgerEvents'

const pending: Array<(value: unknown) => void> = []
const apiMock = {
  getHoldings: vi.fn(() => new Promise((resolve) => pending.push(resolve))),
  getUserInfo: vi.fn(),
  login: vi.fn(),
  logout: vi.fn()
}
vi.mock('../api', () => ({ default: apiMock }))

const { useHoldingsStore } = await import('./holdings')
const { useAuthStore } = await import('./auth')

function installLocalStorage() {
  const data = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => void data.set(key, value),
    removeItem: (key: string) => void data.delete(key)
  })
}

function apiError(status?: number) {
  return Object.assign(new Error('x'), {
    userMessage: 'x',
    response: status === undefined ? undefined : { status }
  })
}

const user = (id: number) => ({ id, username: `u${id}`, is_admin: false, is_active: true })

beforeEach(() => {
  installLocalStorage()
  setActivePinia(createPinia())
  pending.length = 0
  vi.clearAllMocks()
})

describe('ledgerEvents', () => {
  it('每个信号推进代际并通知订阅者；退订后不再通知', () => {
    const listener = vi.fn()
    const off = onLedgerEvent('ledger-mutated', listener)
    const before = dataEpoch()
    emitLedgerEvent('ledger-mutated')
    expect(listener).toHaveBeenCalledTimes(1)
    expect(isDataEpochCurrent(before)).toBe(false)
    off()
    emitLedgerEvent('ledger-mutated')
    expect(listener).toHaveBeenCalledTimes(1)
  })
})

describe('holdings store', () => {
  it('会话变更与账本写入都清空缓存', async () => {
    const store = useHoldingsStore()
    const first = store.fetchHoldings()
    pending.shift()!({ data: [{ id: 1 }] })
    await first
    expect(Object.keys(store.cache)).toHaveLength(1)

    emitLedgerEvent('session-changed')
    expect(store.cache).toEqual({})

    const second = store.fetchHoldings()
    pending.shift()!({ data: [{ id: 2 }] })
    await second
    emitLedgerEvent('ledger-mutated')
    expect(store.cache).toEqual({})
  })

  it('在途请求期间换了用户：迟到的响应不写回缓存', async () => {
    const store = useHoldingsStore()
    const inFlight = store.fetchHoldings()
    emitLedgerEvent('session-changed') // A 登出、B 登录
    pending.shift()!({ data: [{ id: 'A 的持仓' }] })
    await inFlight
    expect(store.cache).toEqual({})
  })
})

describe('auth store', () => {
  it('换用户发 session-changed；同一用户刷新资料不发', async () => {
    const listener = vi.fn()
    const off = onLedgerEvent('session-changed', listener)
    const auth = useAuthStore()
    apiMock.getUserInfo.mockResolvedValueOnce({ data: user(1) })
    await auth.checkAuth()
    apiMock.getUserInfo.mockResolvedValueOnce({ data: user(1) })
    await auth.fetchUserInfo()
    expect(listener).toHaveBeenCalledTimes(1)
    apiMock.getUserInfo.mockResolvedValueOnce({ data: user(2) })
    await auth.fetchUserInfo()
    expect(listener).toHaveBeenCalledTimes(2)
    off()
  })

  it('401 才是未登录：清会话', async () => {
    const auth = useAuthStore()
    apiMock.getUserInfo.mockResolvedValueOnce({ data: user(1) })
    await auth.checkAuth()
    apiMock.getUserInfo.mockRejectedValueOnce(apiError(401))
    expect(await auth.checkAuth()).toBe('unauthenticated')
    expect(auth.isAuthenticated).toBe(false)
    expect(auth.user).toBeNull()
    expect(auth.sessionChecked).toBe(true)
  })

  it.each([[undefined], [500], [502], [408], [429]])(
    '网络错误/%s 是服务不可用：保留缓存用户，下次导航重新探测',
    async (status) => {
      const auth = useAuthStore()
      apiMock.getUserInfo.mockResolvedValueOnce({ data: user(1) })
      await auth.checkAuth()
      apiMock.getUserInfo.mockRejectedValueOnce(apiError(status))
      expect(await auth.checkAuth()).toBe('unavailable')
      expect(auth.user?.id).toBe(1)
      expect(auth.isAuthenticated).toBe(true)
      expect(auth.lastAuthCheck).toBe('unavailable')
      expect(auth.sessionChecked).toBe(false)
    }
  )

  it.each([[400], [404], [422]])(
    '%s 是服务端的明确回答（停用账号的 /auth/me 返回 400）：按未登录处理并清会话',
    async (status) => {
      const auth = useAuthStore()
      apiMock.getUserInfo.mockResolvedValueOnce({ data: user(1) })
      await auth.checkAuth()
      apiMock.getUserInfo.mockRejectedValueOnce(apiError(status))
      expect(await auth.checkAuth()).toBe('unauthenticated')
      expect(auth.user).toBeNull()
      expect(auth.sessionChecked).toBe(true)
    }
  )

  it('一次「不可用」之后登录、再登出：lastAuthCheck 跟着会话走，不停在旧值', async () => {
    const auth = useAuthStore()
    apiMock.getUserInfo.mockRejectedValueOnce(apiError())
    expect(await auth.checkAuth()).toBe('unavailable')
    apiMock.login.mockResolvedValueOnce({ data: { user: user(1) } })
    await auth.login('u1', 'pw')
    expect(auth.lastAuthCheck).toBe('ok')
    apiMock.logout.mockResolvedValueOnce({})
    await auth.logout()
    expect(auth.lastAuthCheck).toBe('unauthenticated')
  })

  it('服务不可用且没有缓存用户：视为未登录但不清任何东西', async () => {
    const auth = useAuthStore()
    apiMock.getUserInfo.mockRejectedValueOnce(apiError())
    expect(await auth.checkAuth()).toBe('unavailable')
    expect(auth.isAuthenticated).toBe(false)
  })
})
