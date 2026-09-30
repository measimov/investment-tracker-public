import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import api from '../api'
import type { User } from '../types'
import { getApiErrorMessage, isApiError } from '../utils/apiErrors'
import { emitLedgerEvent } from '../utils/ledgerEvents'
import { setSecuritySearchCacheScope } from '../utils/securitySearchCache'

// 后端 User schema 为准（PR #172 复审）；localStorage 旧缓存的兼容在
// loadCachedUser 的 JSON 解码边界显式处理，不整体降型。
export type UserInfo = User

/**
 * 会话探测结果：只有 401/403 才是「未登录」；网络错误与 5xx 是「服务暂不可用」——
 * 此前一律当未登录，后端重启期间用户会被踢到登录页并提示「请先登录」（#286）。
 */
export type AuthCheckResult = 'ok' | 'unauthenticated' | 'unavailable'

export type LoginResult =
  | { success: true }
  | { success: false; message: string; globallyNotified: boolean }

export const useAuthStore = defineStore('auth', () => {
  function loadCachedUser(): UserInfo | null {
    try {
      const storedUser = localStorage.getItem('user')
      if (!storedUser) return null
      // JSON 解码边界：旧版缓存可能缺新必填字段，缺核心键的直接作废
      // 让登录流程重新写入（而不是把整个类型放宽成 optional）
      const parsed = JSON.parse(storedUser) as Partial<UserInfo>
      if (typeof parsed?.id !== 'number' || typeof parsed?.username !== 'string') {
        localStorage.removeItem('user')
        return null
      }
      return parsed as UserInfo
    } catch (error) {
      console.warn('Failed to load cached user:', error)
      localStorage.removeItem('user')
      return null
    }
  }

  const user = ref<UserInfo | null>(loadCachedUser())
  // 标的检索缓存按认证身份命名空间：登录/恢复/登出都要同步，否则 A 登出 B 登录后
  // （只 router.push，不重载页面）B 会拿到 A 的持仓/自选候选
  setSecuritySearchCacheScope(user.value?.id ?? null)
  const authenticated = ref(false)
  const sessionChecked = ref(false)
  const lastAuthCheck = ref<AuthCheckResult | null>(null)

  const isAuthenticated = computed(() => authenticated.value)
  const isAdmin = computed(() => user.value?.is_admin === true)

  function cacheUser(userInfo: UserInfo) {
    const previousId = user.value?.id ?? null
    user.value = userInfo
    localStorage.setItem('user', JSON.stringify(userInfo))
    setSecuritySearchCacheScope(userInfo.id)
    // 换了用户（A 登出后 B 登录不重载页面）：A 的持仓/交易缓存必须作废（#268）
    if (previousId !== userInfo.id) emitLedgerEvent('session-changed')
  }

  function clearSession() {
    user.value = null
    authenticated.value = false
    // 否则一次「不可用」之后登录再登出，守卫仍读到旧的 'unavailable'，「请先登录」被吞掉
    lastAuthCheck.value = 'unauthenticated'
    localStorage.removeItem('user')
    setSecuritySearchCacheScope(null)
    emitLedgerEvent('session-changed')
  }

  async function login(username: string, password: string): Promise<LoginResult> {
    try {
      const response = await api.login(username, password)
      cacheUser(response.data.user)
      authenticated.value = true
      sessionChecked.value = true
      lastAuthCheck.value = 'ok'
      return { success: true }
    } catch (error) {
      clearSession()
      sessionChecked.value = true
      // getApiErrorMessage 而不是直接取 detail：422 的 detail 是数组，
      // 直接塞进提示会显示成 [object Object]
      return {
        success: false,
        message: getApiErrorMessage(error, '登录失败，请检查用户名和密码'),
        // 拦截器已弹过全局通知（5xx/断网）：登录页不再重复显示同一条错误
        globallyNotified: isApiError(error) && error.globallyNotified === true
      }
    }
  }

  async function logout() {
    try {
      await api.logout()
    } catch (error) {
      if (!isApiError(error) || error.response?.status !== 401) {
        console.warn('Remote logout failed:', error)
      }
    } finally {
      clearSession()
      sessionChecked.value = true
    }
  }

  /**
   * 只清本地会话、不调后端登出：改密码成功后服务端已吊销全部会话，
   * 此时再 POST /auth/logout 会吃 401 并触发拦截器整页跳转。
   */
  function endLocalSession() {
    clearSession()
    sessionChecked.value = true
  }

  async function fetchUserInfo(): Promise<UserInfo> {
    const response = await api.getUserInfo()
    cacheUser(response.data)
    authenticated.value = true
    return response.data
  }

  async function checkAuth(): Promise<AuthCheckResult> {
    let result: AuthCheckResult
    try {
      await fetchUserInfo()
      result = 'ok'
    } catch (error) {
      const status = isApiError(error) ? error.response?.status : undefined
      // 「不可用」只认没有响应（断网/超时）、5xx 与 408/429；其余 4xx 是服务端的明确回答——
      // 例如停用账号的 /auth/me 返回 400，按不可用处理会放进外壳后每个请求都 400 且无提示
      const unavailable = status === undefined || status >= 500 || status === 408 || status === 429
      if (!unavailable) {
        clearSession()
        result = 'unauthenticated'
      } else {
        // 服务暂不可用：不清会话。有缓存用户就照常放行（请求失败由连接横幅与全局通知提示，
        // 后端恢复后自然可用；会话若真已失效，下一次请求的 401 会走拦截器跳登录）；
        // 下次导航重新探测
        authenticated.value = user.value !== null
        result = 'unavailable'
      }
    }
    lastAuthCheck.value = result
    sessionChecked.value = result !== 'unavailable'
    return result
  }

  function updateUser(userInfo: Partial<UserInfo>) {
    if (!user.value) return // 未登录没有可局部更新的用户（也让展开保持完整类型）
    cacheUser({ ...user.value, ...userInfo })
  }

  return {
    user,
    sessionChecked,
    lastAuthCheck,
    isAuthenticated,
    isAdmin,
    login,
    logout,
    endLocalSession,
    fetchUserInfo,
    checkAuth,
    updateUser
  }
})
