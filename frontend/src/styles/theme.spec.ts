// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'
import { createThemeState, THEME_STORAGE_KEY } from './theme'

function browser(dark = false, stored: string | null = null, blocked = false) {
  let listener: (() => void) | undefined
  const media = {
    matches: dark,
    addEventListener: vi.fn((_event: string, callback: () => void) => {
      listener = callback
    }),
    removeEventListener: vi.fn()
  }
  const storage = {
    getItem: vi.fn(() => {
      if (blocked) throw new Error('denied')
      return stored
    }),
    setItem: vi.fn(() => {
      if (blocked) throw new Error('denied')
    })
  }
  const target = { document, matchMedia: () => media, localStorage: storage } as unknown as Window
  return {
    target,
    storage,
    media,
    change(value: boolean) {
      media.matches = value
      listener?.()
    }
  }
}

describe('theme preference', () => {
  it('follows system changes only while system is selected', () => {
    const env = browser()
    const theme = createThemeState(env.target)
    env.change(true)
    expect(theme.resolved.value).toBe('dark')
    theme.setPreference('light')
    env.change(false)
    env.change(true)
    expect(theme.resolved.value).toBe('light')
    theme.setPreference('system')
    expect(theme.resolved.value).toBe('dark')
    expect(env.storage.setItem).toHaveBeenLastCalledWith(THEME_STORAGE_KEY, 'system')
    theme.dispose()
    expect(env.media.removeEventListener).toHaveBeenCalledWith('change', expect.any(Function))
  })
  it('restores explicit preference and updates the document and persistence', () => {
    const env = browser(false, 'dark')
    const theme = createThemeState(env.target)
    expect(document.documentElement.classList.contains('dark')).toBe(true)
    theme.setPreference('light')
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(document.documentElement.style.colorScheme).toBe('light')
    expect(env.storage.setItem).toHaveBeenCalledWith(THEME_STORAGE_KEY, 'light')
    theme.dispose()
  })
  it('ignores invalid saved values and supports denied storage', () => {
    for (const env of [browser(true, 'invalid'), browser(true, null, true)]) {
      const theme = createThemeState(env.target)
      expect(theme.preference.value).toBe('system')
      expect(theme.resolved.value).toBe('dark')
      expect(() => theme.setPreference('light')).not.toThrow()
      expect(theme.resolved.value).toBe('light')
      theme.dispose()
    }
  })
})
