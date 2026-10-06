import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import type { EChartsCoreOption } from 'echarts/core'
import { CHART_FONT_FAMILY, CHART_SYSTEM_FONT_FAMILY } from '@/styles/tokens'

/** 避免首次绘图在回退字体下缓存字宽；失败或超时仍展示图表。 */
export function useChartFontOption(
  getOption: (fonts: { text: string; number: string }) => EChartsCoreOption,
  { serifNumbers = false } = {}
) {
  const family = ref<{ text: string; number: string }>()
  let disposed = false
  let timer: ReturnType<typeof setTimeout> | undefined
  onUnmounted(() => {
    disposed = true
    clearTimeout(timer)
  })
  onMounted(async () => {
    await nextTick()
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
    if (disposed) return
    const style = getComputedStyle(document.documentElement)
    const preferred = {
      text: CHART_FONT_FAMILY,
      number: serifNumbers ? style.getPropertyValue('--app-font-serif').trim() : CHART_FONT_FAMILY
    }
    const fallback = {
      text: CHART_SYSTEM_FONT_FAMILY,
      number: serifNumbers
        ? style.getPropertyValue('--app-font-serif-system').trim()
        : CHART_SYSTEM_FONT_FAMILY
    }
    try {
      const fonts = document.fonts
      if (fonts) {
        await Promise.race([
          (async () => {
            // 图表自身尚未绘字，显式触发数字及常用标签；其余中文沿已布局的页面文字加载。
            await Promise.all([
              fonts.load(`12px ${preferred.text}`, '0123456789.,%−¥$ 市场收益买入卖出A股港股美股'),
              ...(serifNumbers ? [fonts.load(`14px ${preferred.number}`, '0123456789.,%−¥$')] : [])
            ])
            await fonts.ready
            if (!disposed && !family.value) family.value = preferred
          })(),
          new Promise<void>((resolve) => {
            timer = setTimeout(() => {
              family.value = fallback
              resolve()
            }, 8000)
          })
        ])
      }
    } catch {
      // 字体不可用时保留系统回退，不阻止用户读取数据。
    } finally {
      clearTimeout(timer)
      if (!disposed && !family.value) family.value = fallback
    }
  })
  return computed(() => {
    if (!family.value) return undefined
    const option = getOption(family.value)
    return { ...option, textStyle: { ...option.textStyle, fontFamily: family.value.text } }
  })
}
