import { reactive } from 'vue'
import api from '@/api'
import type { BrokerAccount } from '@/types'
import { accountLabel, type AccountListStatus } from '@/utils/labels'
import { showApiError } from '@/utils/showApiError'

/** 页面自己的账户列表；共用请求约定，不缓存或跨页面共享状态。 */
export function useBrokerAccounts() {
  const state = reactive({
    accounts: [] as BrokerAccount[],
    status: 'loading' as AccountListStatus
  })

  async function load() {
    state.status = 'loading'
    try {
      state.accounts = (await api.getBrokerAccounts({ limit: 1000 })).data
      state.status = 'ready'
    } catch (error) {
      state.status = 'error'
      showApiError(error, '加载券商账户失败')
    }
  }

  const label = (id: unknown) => accountLabel(state.accounts, id, { status: state.status })
  return { state, load, label }
}
