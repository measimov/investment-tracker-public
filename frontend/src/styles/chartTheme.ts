import { computed } from 'vue'
import { useTheme } from './theme'
import { MARKET_CHART_INDICES, BENCHMARK_CHART_INDICES } from './tokens'

/** Read the resolved CSS palette after theme state applies the root class. */
export function useChartColors() {
  const theme = useTheme()
  return computed(() => {
    // Explicit dependency also covers system changes; chart options update in place.
    void theme.resolved.value
    const style = getComputedStyle(document.documentElement)
    const color = (name: string) => style.getPropertyValue(`--app-${name}`).trim()
    const palette = Array.from({ length: 7 }, (_, index) => color(`chart-${index + 1}`))
    return {
      palette,
      marketPalette: MARKET_CHART_INDICES.map((index) => palette[index]),
      benchmarkPalette: BENCHMARK_CHART_INDICES.map((index) => palette[index]),
      text: color('text'),
      muted: color('text-muted'),
      surface: color('surface'),
      secondary: color('surface-secondary'),
      border: color('border'),
      separator: color('separator'),
      primary: color('primary'),
      success: color('success'),
      danger: color('danger'),
      dangerSoft: color('danger-soft')
    }
  })
}
