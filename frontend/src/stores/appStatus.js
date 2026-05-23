import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

export const useAppStatusStore = defineStore('appStatus', () => {
  const connectionLost = ref(false)
  const maintenanceMode = ref(false)
  const message = ref('')
  const lastUpdatedAt = ref(null)
  const lastIssueAt = ref(0)

  const hasBlockingIssue = computed(() => connectionLost.value || maintenanceMode.value)
  const statusTitle = computed(() => {
    if (maintenanceMode.value) return '服务暂时不可用'
    if (connectionLost.value) return '连接已中断'
    return ''
  })

  function markConnectionLost(nextMessage = '网络连接失败，请检查网络') {
    const now = Date.now()
    connectionLost.value = true
    maintenanceMode.value = false
    message.value = nextMessage
    lastUpdatedAt.value = new Date(now)
    lastIssueAt.value = now
  }

  function markMaintenance(nextMessage = '服务正在维护或暂时不可用，请稍后重试') {
    const now = Date.now()
    maintenanceMode.value = true
    connectionLost.value = false
    message.value = nextMessage
    lastUpdatedAt.value = new Date(now)
    lastIssueAt.value = now
  }

  function clear() {
    connectionLost.value = false
    maintenanceMode.value = false
    message.value = ''
    lastUpdatedAt.value = null
    lastIssueAt.value = 0
  }

  function shouldClearForRequest(startedAt) {
    return hasBlockingIssue.value && Number.isFinite(startedAt) && startedAt > lastIssueAt.value
  }

  return {
    connectionLost,
    maintenanceMode,
    message,
    lastUpdatedAt,
    lastIssueAt,
    hasBlockingIssue,
    statusTitle,
    markConnectionLost,
    markMaintenance,
    shouldClearForRequest,
    clear
  }
})
