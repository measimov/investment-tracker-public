// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h } from 'vue'
import { useAutoReload } from './useAutoReload'

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
  document.dispatchEvent(new Event('visibilitychange'))
}

function mountWith(reload: () => unknown, options: Parameters<typeof useAutoReload>[1] = {}) {
  const app = createApp(
    defineComponent({
      setup() {
        useAutoReload(reload, { intervalMs: 1000, ...options })
        return () => h('div')
      }
    })
  )
  app.mount(document.createElement('div'))
  return app
}

describe('useAutoReload', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setVisibility('visible')
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('按间隔重读，卸载后停止', async () => {
    const reload = vi.fn()
    const app = mountWith(reload)
    await vi.advanceTimersByTimeAsync(3000)
    expect(reload).toHaveBeenCalledTimes(3)
    app.unmount()
    await vi.advanceTimersByTimeAsync(3000)
    expect(reload).toHaveBeenCalledTimes(3)
  })

  it('隐藏时不重读，切回可见立即重读一次', async () => {
    const reload = vi.fn()
    const app = mountWith(reload)
    setVisibility('hidden')
    await vi.advanceTimersByTimeAsync(3000)
    expect(reload).not.toHaveBeenCalled()
    setVisibility('visible')
    await vi.advanceTimersByTimeAsync(0)
    expect(reload).toHaveBeenCalledTimes(1)
    app.unmount()
  })

  it('paused 时跳过；上一轮未结束不叠加；失败静默', async () => {
    let paused = true
    let release: () => void = () => {}
    const reload = vi.fn(
      () =>
        new Promise<void>((resolve, reject) => {
          release = () => reject(new Error('boom'))
          void resolve
        })
    )
    const app = mountWith(reload, { paused: () => paused })
    await vi.advanceTimersByTimeAsync(2000)
    expect(reload).not.toHaveBeenCalled()
    paused = false
    await vi.advanceTimersByTimeAsync(3000)
    expect(reload).toHaveBeenCalledTimes(1)
    release()
    await vi.advanceTimersByTimeAsync(1000)
    expect(reload).toHaveBeenCalledTimes(2)
    app.unmount()
  })
})
