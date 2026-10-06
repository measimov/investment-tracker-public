// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createPinia, disposePinia, setActivePinia, type Pinia } from 'pinia'
import { nextTick } from 'vue'
import api from '@/api'
import { useAuthStore } from './auth'
import { useXueqiuCapabilitiesStore } from './xueqiuCapabilities'

vi.mock('@/api', () => ({ default: { getCapabilities: vi.fn(), login: vi.fn() } }))
let pinia: Pinia
const absent = {
  opinions: { available: false, reason: 'unconfigured' },
  xueqiu_symbol_feed: { available: false, reason: 'unconfigured' }
}
const posts = { ...absent, xueqiu_symbol_feed: { available: true, reason: 'history' } }
async function identity(id = 2, isAdmin = false) {
  const auth = useAuthStore()
  vi.mocked(api.login).mockResolvedValueOnce({
    data: {
      user: { id, username: `UI_USER_${id}`, email: null, is_active: true, is_admin: isAdmin }
    }
  } as never)
  expect(await auth.login(`UI_USER_${id}`, 'UI_SYNTH_PASSWORD')).toEqual({ success: true })
  return auth
}
beforeEach(() => {
  localStorage.clear()
  pinia = createPinia()
  setActivePinia(pinia)
  vi.mocked(api.getCapabilities).mockReset()
})
afterEach(() => disposePinia(pinia))

it('unknown stays visible; a single pending read resolves the two independent entries', async () => {
  await identity()
  let reply!: (value: unknown) => void
  vi.mocked(api.getCapabilities).mockReturnValueOnce(
    new Promise((resolve) => (reply = resolve)) as never
  )
  const store = useXueqiuCapabilitiesStore()
  expect(store.showOpinions).toBe(true)
  expect(store.showSymbolFeed).toBe(true)
  const pending = store.load()
  expect(api.getCapabilities).toHaveBeenCalledTimes(1)
  reply({ data: posts })
  await pending
  expect(store.showOpinions).toBe(false)
  expect(store.showSymbolFeed).toBe(true)
})

it('a refresh failure is unknown rather than a retained absence; explicit refresh may recover', async () => {
  await identity()
  vi.mocked(api.getCapabilities).mockResolvedValueOnce({ data: absent } as never)
  const store = useXueqiuCapabilitiesStore()
  await store.load()
  expect(store.showOpinions).toBe(false)
  vi.mocked(api.getCapabilities).mockRejectedValueOnce(new Error('UI明确虚构读取失败'))
  await store.load(true)
  expect(store.capabilities).toBeNull()
  expect(store.showOpinions).toBe(true)
  expect(store.error).not.toBe('')
  vi.mocked(api.getCapabilities).mockResolvedValueOnce({ data: posts } as never)
  await store.load(true)
  expect(store.showOpinions).toBe(false)
  expect(store.showSymbolFeed).toBe(true)
})

it('session ABA late success cannot overwrite the latest confirmed identity, and logout clears it', async () => {
  const auth = await identity()
  let oldReply!: (value: unknown) => void
  vi.mocked(api.getCapabilities)
    .mockReturnValueOnce(new Promise((resolve) => (oldReply = resolve)) as never)
    .mockResolvedValueOnce({ data: posts } as never)
    .mockResolvedValueOnce({ data: absent } as never)
  const store = useXueqiuCapabilitiesStore()
  const old = store.load()
  await identity(3)
  await nextTick()
  await store.load()
  expect(store.showSymbolFeed).toBe(true)
  await identity(2)
  await nextTick()
  await store.load()
  oldReply({ data: posts })
  await old
  expect(store.capabilities).toEqual(absent)
  auth.endLocalSession()
  await nextTick()
  expect(store.capabilities).toBeNull()
  expect(store.loading).toBe(false)
})

it('administrators retain both configuration entries despite known absence', async () => {
  await identity(1, true)
  vi.mocked(api.getCapabilities).mockResolvedValueOnce({ data: absent } as never)
  const store = useXueqiuCapabilitiesStore()
  await store.load()
  expect(store.capabilities).toEqual(absent)
  expect(store.showOpinions).toBe(true)
  expect(store.showSymbolFeed).toBe(true)
})
