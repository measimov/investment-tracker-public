import { ref, onMounted, onUnmounted, type Ref } from 'vue'

/**
 * 响应式媒体查询：返回一个随视口变化自动更新的 ref，并自动清理监听。
 * @param query - CSS 媒体查询，如 '(max-width: 640px)'
 */
export function useMediaQuery(query: string): Ref<boolean> {
  const mediaQueryList = window.matchMedia(query)
  // 初值直接取当前匹配结果：ref(false) 要等 onMounted 才同步，手机首帧会先渲染
  // 桌面表格再切卡片，页面闪一下（#219）
  const matches = ref(mediaQueryList.matches)

  const updateMatches = () => {
    matches.value = mediaQueryList.matches
  }

  onMounted(() => {
    updateMatches()
    mediaQueryList.addEventListener('change', updateMatches)
  })

  onUnmounted(() => {
    mediaQueryList.removeEventListener('change', updateMatches)
  })

  return matches
}
