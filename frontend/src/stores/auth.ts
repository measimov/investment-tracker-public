import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import api from '../api'
import { isApiError } from '../utils/apiErrors'

/** 后端 /auth/me 返回的用户信息（前端点名使用的字段）。 */
export interface UserInfo {
  username?: string
  is_admin?: boolean
  [key: string]: unknown
}

export const useAuthStore = defineStore('auth', () => {
  function loadCachedUser(): UserInfo | null {
    try {
      const storedUser = localStorage.getItem('user')
      return storedUser ? JSON.parse(storedUser) : null
    } catch (error) {
      console.warn('Failed to load cached user:', error)
      localStorage.removeItem('user')
      return null
    }
  }

  const user = ref<UserInfo | null>(loadCachedUser())
  const authenticated = ref(false)
  const sessionChecked = ref(false)

  const isAuthenticated = computed(() => authenticated.value)
  const isAdmin = computed(() => user.value?.is_admin === true)

  function cacheUser(userInfo: UserInfo) {
    user.value = userInfo
    localStorage.setItem('user', JSON.stringify(userInfo))
  }

  function clearSession() {
    user.value = null
    authenticated.value = false
    localStorage.removeItem('user')
    localStorage.removeItem('token')
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
