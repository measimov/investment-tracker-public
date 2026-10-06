import { readonly, ref } from 'vue'

export type ThemePreference = 'system' | 'light' | 'dark'
export const THEME_STORAGE_KEY = 'investment-theme'

/** Browser-only preference; it never changes account or server state. */
export function createThemeState(browser: Window = window) {
  const media = browser.matchMedia('(prefers-color-scheme: dark)')
  const preference = ref<ThemePreference>('system')
  const resolved = ref<'light' | 'dark'>('light')
  try {
    const stored = browser.localStorage.getItem(THEME_STORAGE_KEY)
    if (stored === 'light' || stored === 'dark') preference.value = stored
  } catch {
    // A denied storage read must not prevent mounting the application.
  }

  function apply() {
    resolved.value =
      preference.value === 'system' ? (media.matches ? 'dark' : 'light') : preference.value
    const root = browser.document.documentElement
    root.classList.toggle('dark', resolved.value === 'dark')
    root.dataset.theme = resolved.value
    root.style.colorScheme = resolved.value
  }
  function setPreference(value: ThemePreference) {
    preference.value = value
    apply()
    try {
      browser.localStorage.setItem(THEME_STORAGE_KEY, value)
    } catch {
      // The current session remains usable when persistence is unavailable.
    }
  }
  function onSystemChange() {
    if (preference.value === 'system') apply()
  }
  media.addEventListener('change', onSystemChange)
  apply()
  return {
    preference: readonly(preference),
    resolved: readonly(resolved),
    setPreference,
    dispose: () => media.removeEventListener('change', onSystemChange)
  }
}

let state: ReturnType<typeof createThemeState> | undefined
export function useTheme() {
  return (state ??= createThemeState())
}
