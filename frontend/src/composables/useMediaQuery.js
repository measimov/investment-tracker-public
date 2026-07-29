import { ref, onMounted, onUnmounted } from 'vue'

/**
 * 响应式媒体查询：返回一个随视口变化自动更新的 ref，并自动清理监听。
 * @param {string} query - CSS 媒体查询，如 '(max-width: 640px)'
 */
export function useMediaQuery(query) {
  const matches = ref(false)
  const mediaQueryList = window.matchMedia(query)

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
