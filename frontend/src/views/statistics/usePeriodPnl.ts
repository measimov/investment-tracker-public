import { getCurrentScope, onScopeDispose, reactive } from 'vue'
import api from '@/api'
import type { PeriodPnlResponse } from '@/types'
import { getApiErrorMessage } from '@/utils/apiErrors'

/** 手工试价与摘要一起刷新；失败时不沿用另一组价格的期间损益。 */
export function usePeriodPnl() {
  let requestSequence = 0
  if (getCurrentScope()) onScopeDispose(() => ++requestSequence)
  const state = reactive({ data: null as PeriodPnlResponse | null, error: '' })

  async function load(prices: Record<string, number> | null = null) {
    const sequence = ++requestSequence
    state.data = null
    state.error = ''
    try {
      const response = await api.getPeriodPnl(prices)
      if (sequence !== requestSequence) return false
      if (!response.data?.periods?.mtd || !response.data?.periods?.ytd) {
        throw new Error('返回的期间损益不完整')
      }
      state.data = response.data
      return true
    } catch (error) {
      if (sequence !== requestSequence) return false
      state.error = getApiErrorMessage(error, '加载期间损益失败')
      return false
    }
  }

  return reactive({ state, load })
}
