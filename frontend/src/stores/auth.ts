import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import api from '../api'
import type { User } from '../types'
import { isApiError } from '../utils/apiErrors'
import { setSecuritySearchCacheScope } from '../utils/securitySearchCache'

// 后端 User schema 为准（PR #172 复审）；localStorage 旧缓存的兼容在
// loadCachedUser 的 JSON 解码边界显式处理，不整体降型。
export type UserInfo = User

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

  const isAuthenticated = computed(() => authenticated.value)
  const isAdmin = computed(() => user.value?.is_admin === true)

  function cacheUser(userInfo: UserInfo) {
    user.value = userInfo
    localStorage.setItem('user', JSON.stringify(userInfo))
    setSecuritySearchCacheScope(userInfo.id)
  }

  function clearSession() {
    user.value = null
    authenticated.value = false
    localStorage.removeItem('user')
    setSecuritySearchCacheScope(null)
  }

  async function login(username: string, password: string) {
    try {
      const response = await api.login(username, password)
      cacheUser(response.data.user)
      authenticated.value = true
      sessionChecked.value = true
      return { success: true }
    } catch (error) {
      clearSession()
      sessionChecked.value = true
      return {
        success: false,
        message:
          (isApiError(error) && error.response?.data?.detail) || '登录失败，请检查用户名和密码'
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

  async function fetchUserInfo(): Promise<UserInfo> {
    const response = await api.getUserInfo()
    cacheUser(response.data)
    authenticated.value = true
    return response.data
  }

  async function checkAuth(): Promise<boolean> {
    try {
      await fetchUserInfo()
      return true
    } catch {
      clearSession()
      return false
    } finally {
      sessionChecked.value = true
    }
  }

  function updateUser(userInfo: Partial<UserInfo>) {
    if (!user.value) return // 未登录没有可局部更新的用户（也让展开保持完整类型）
    cacheUser({ ...user.value, ...userInfo })
  }

  return {
    user,
    sessionChecked,
    isAuthenticated,
    isAdmin,
    login,
    logout,
    fetchUserInfo,
    checkAuth,
    updateUser
  }
})
