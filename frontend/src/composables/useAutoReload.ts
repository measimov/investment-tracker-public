import { onMounted, onUnmounted } from 'vue'

/** 页面自动重读的默认间隔：后端交易时段每 15 分钟刷新报价，页面 5 分钟重读一次足够跟上 */
export const AUTO_RELOAD_INTERVAL_MS = 5 * 60 * 1000

export interface AutoReloadOptions {
  intervalMs?: number
  /** 返回 true 时本轮跳过（如用户正在编辑价格，重读会覆盖输入） */
  paused?: () => boolean
}

/**
 * 页面可见时定期重读（只读库，不触发任何外部报价请求——报价由后端周期任务刷新）。
 *
 * - 标签页隐藏时不重读；从隐藏切回可见时立即重读一次（离开期间后端可能已刷新过）。
 * - 上一轮还没结束时不叠加新一轮。
 * - 卸载时清理定时器与事件监听。
 *
 * 首次加载仍由页面自己的 onMounted 负责；这里只管之后的周期重读。
 */
export function useAutoReload(reload: () => unknown, options: AutoReloadOptions = {}) {
  const intervalMs = options.intervalMs ?? AUTO_RELOAD_INTERVAL_MS
  let timer: ReturnType<typeof setInterval> | null = null
  let running = false

  async function tick() {
    if (running || document.visibilityState === 'hidden') return
    if (options.paused?.()) return
    running = true
    try {
      await reload()
    } catch {
      // 周期重读失败静默：页面上已有数据，下一轮再试；首次加载的报错由页面自己弹
    } finally {
      running = false
    }
  }

  function onVisibilityChange() {
    if (document.visibilityState === 'visible') void tick()
  }

  onMounted(() => {
    timer = setInterval(() => void tick(), intervalMs)
    document.addEventListener('visibilitychange', onVisibilityChange)
  })

  onUnmounted(() => {
    if (timer !== null) clearInterval(timer)
    timer = null
    document.removeEventListener('visibilitychange', onVisibilityChange)
  })

  return { reloadNow: tick }
}
