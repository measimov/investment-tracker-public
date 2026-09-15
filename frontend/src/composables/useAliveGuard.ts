import { onUnmounted } from 'vue'

/**
 * 组件存活守卫（issue #139）。
 *
 * 统一替代逐 view 手写的 `let isUnmounted = false` + onUnmounted 翻转：
 * 手抄版在 Holdings（28 处引用）/Statistics/SecurityDetail 各写一遍，而同样
 * 长轮询的 Reports、CorporateActions 完全没抄到——不是设计取舍，是复制遗漏。
 *
 * 返回函数而不是布尔量：闭包捕获后永远读到最新值，可直接作
 * `pollJobUntilDone` 的 `isCancelled` 谓词。
 */
export function useAliveGuard() {
  let unmounted = false
  onUnmounted(() => {
    unmounted = true
  })
  return {
    /** 组件已卸载（守卫长轮询回调里的状态写入与消息弹出） */
    isUnmounted: () => unmounted
  }
}
