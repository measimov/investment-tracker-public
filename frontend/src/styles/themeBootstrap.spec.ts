// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const bootstrap = readFileSync('public/theme-init.js', 'utf8')

function paint(stored: string | null, systemDark: boolean, denied = false) {
  const storage = {
    getItem() {
      if (denied) throw new Error('denied')
      return stored
    }
  }
  const browser = { matchMedia: () => ({ matches: systemDark }) }
  new Function('localStorage', 'window', 'document', bootstrap)(storage, browser, document)
  return document.documentElement.dataset.theme
}

describe('pre-styles theme bootstrap', () => {
  it('applies a saved preference before Vue mounts', () => {
    expect(paint('dark', false)).toBe('dark')
    expect(paint('light', true)).toBe('light')
  })
  it('falls back to system for absent, invalid or inaccessible storage', () => {
    expect(paint(null, true)).toBe('dark')
    expect(paint('invalid', false)).toBe('light')
    expect(paint(null, true, true)).toBe('dark')
  })
})
