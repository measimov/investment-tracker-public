import { computed, onScopeDispose, ref, watch } from 'vue'
import { defineStore } from 'pinia'
import api from '@/api'
import type { Capabilities } from '@/types'
import { useLatestRequest } from '@/composables/useLatestRequest'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { onLedgerEvent } from '@/utils/ledgerEvents'
import { useAuthStore } from './auth'

/** 仅当前认证身份的入口可见性；未知不等同未配置。 */
export const useXueqiuCapabilitiesStore = defineStore('xueqiu-capabilities', () => {
  const auth = useAuthStore()
  const capabilities = ref<Capabilities | null>(null)
  const loading = ref(false)
  const error = ref('')
  const request = useLatestRequest()
  let attempted = false
  let pending: Promise<void> | null = null

  function reset() {
    request.invalidate()
    capabilities.value = null
    error.value = ''
    loading.value = false
    attempted = false
    pending = null
  }

  function load(force = false): Promise<void> {
    if (!auth.isAuthenticated || !auth.user) return Promise.resolve()
    if (!force && pending) return pending
    if (!force && attempted) return Promise.resolve()
    const id = auth.user.id
    const ticket = request.begin()
    attempted = true
    loading.value = true
    error.value = ''
    pending = api
      .getCapabilities()
      .then((response) => {
        if (request.isCurrent(ticket) && auth.isAuthenticated && auth.user?.id === id)
          capabilities.value = response.data
      })
      .catch((failure) => {
        if (!request.isCurrent(ticket)) return
        capabilities.value = null
        error.value = getApiErrorMessage(failure, '雪球入口能力暂未确认')
      })
      .finally(() => {
        if (!request.isCurrent(ticket)) return
        loading.value = false
        pending = null
      })
    return pending
  }

  const unsubscribe = onLedgerEvent('session-changed', reset)
  onScopeDispose(unsubscribe)
  watch(
    () => [auth.isAuthenticated, auth.user?.id],
    () => {
      reset()
      if (auth.isAuthenticated) void load()
    },
    { immediate: true }
  )

  const showOpinions = computed(
    () => auth.isAdmin || capabilities.value?.opinions.available !== false
  )
  const showSymbolFeed = computed(
    () => auth.isAdmin || capabilities.value?.xueqiu_symbol_feed.available !== false
  )
  return { capabilities, loading, error, showOpinions, showSymbolFeed, load }
})
