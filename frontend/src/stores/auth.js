import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import api from '../api'

export const useAuthStore = defineStore('auth', () => {
  function loadCachedUser() {
    try {
      const storedUser = localStorage.getItem('user')
      return storedUser ? JSON.parse(storedUser) : null
    } catch (error) {
      console.warn('Failed to load cached user:', error)
      localStorage.removeItem('user')
      return null
    }
  }

  const user = ref(loadCachedUser())
  const authenticated = ref(false)
  const sessionChecked = ref(false)

  const isAuthenticated = computed(() => authenticated.value)
  const isAdmin = computed(() => user.value?.is_admin === true)

  function cacheUser(userInfo) {
    user.value = userInfo
    localStorage.setItem('user', JSON.stringify(userInfo))
  }

  function clearSession() {
    user.value = null
    authenticated.value = false
    localStorage.removeItem('user')
    localStorage.removeItem('token')
  }

  async function login(username, password) {
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
        message: error.response?.data?.detail || '登录失败，请检查用户名和密码'
      }
    }
  }

  async function logout() {
    try {
      await api.logout()
    } catch (error) {
      if (error.response?.status !== 401) {
        console.warn('Remote logout failed:', error)
      }
    } finally {
      clearSession()
      sessionChecked.value = true
    }
  }

  async function fetchUserInfo() {
    const response = await api.getUserInfo()
    cacheUser(response.data)
    authenticated.value = true
    return response.data
  }

  async function checkAuth() {
    try {
      await fetchUserInfo()
      return true
    } catch (error) {
      clearSession()
      return false
    } finally {
      sessionChecked.value = true
    }
  }

  function updateUser(userInfo) {
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
