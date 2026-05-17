import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { jwtDecode } from 'jwt-decode'
import api from '../api'

export const useAuthStore = defineStore('auth', () => {
  // Helper function to safely load auth state from localStorage
  function loadFromLocalStorage() {
    try {
      const storedToken = localStorage.getItem('token')
      const storedUser = localStorage.getItem('user')

      return {
        token: storedToken || null,
        user: storedUser ? JSON.parse(storedUser) : null
      }
    } catch (error) {
      console.warn('Failed to load auth state from localStorage:', error)
      // Clear potentially corrupted data
      localStorage.removeItem('token')
      localStorage.removeItem('user')
      return { token: null, user: null }
    }
  }

  // State - Initialize from localStorage
  const stored = loadFromLocalStorage()
  const user = ref(stored.user)    // ✅ 立即恢复user
  const token = ref(stored.token)  // ✅ 立即恢复token

  // Computed
  const isAuthenticated = computed(() => !!token.value)
  const isAdmin = computed(() => user.value?.is_admin === true)

  // Actions
  async function login(username, password) {
    try {
      const response = await api.login(username, password)
      const { access_token } = response.data

      // Save token
      token.value = access_token
      localStorage.setItem('token', access_token)

      // Fetch user info
      await fetchUserInfo()

      return { success: true }
    } catch (error) {
      console.error('Login failed:', error)
      return {
        success: false,
        message: error.response?.data?.detail || '登录失败，请检查用户名和密码'
      }
    }
  }

  function logout() {
    // Clear state
    user.value = null
    token.value = null

    // Clear localStorage
    localStorage.removeItem('token')
    localStorage.removeItem('user')
  }

  async function fetchUserInfo() {
    try {
      const response = await api.getUserInfo()
      user.value = response.data

      // Also save to localStorage for persistence
      localStorage.setItem('user', JSON.stringify(response.data))
    } catch (error) {
      console.error('Failed to fetch user info:', error)
      // If fetching user info fails, logout
      logout()
      throw error
    }
  }

  async function checkAuth() {
    const storedToken = localStorage.getItem('token')
    const storedUser = localStorage.getItem('user')

    if (!storedToken) {
      logout()
      return false
    }

    try {
      // Check if token is expired
      const decoded = jwtDecode(storedToken)
      const currentTime = Date.now() / 1000

      if (decoded.exp < currentTime) {
        // Token expired
        logout()
        return false
      }

      // Token is valid, set it
      token.value = storedToken

      // Load user from localStorage (快速恢复，不等待API)
      if (storedUser) {
        user.value = JSON.parse(storedUser)
      }

      // 在后台异步刷新用户信息（不阻塞，失败也不影响已恢复的状态）
      fetchUserInfo().catch(err => {
        console.warn('Failed to refresh user info (running in background):', err)
        // 不处理错误，保留localStorage的数据
      })

      return true
    } catch (error) {
      console.error('Auth check failed:', error)
      logout()
      return false
    }
  }

  function updateUser(userInfo) {
    user.value = { ...user.value, ...userInfo }
    localStorage.setItem('user', JSON.stringify(user.value))
  }

  return {
    // State
    user,
    token,
    // Computed
    isAuthenticated,
    isAdmin,
    // Actions
    login,
    logout,
    fetchUserInfo,
    checkAuth,
    updateUser
  }
})
