import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { ElMessage } from 'element-plus'
import Dashboard from '../views/Dashboard.vue'
import Transactions from '../views/Transactions.vue'
import Holdings from '../views/Holdings.vue'
import Statistics from '../views/Statistics.vue'
import CorporateActions from '../views/CorporateActions.vue'
import ExchangeRates from '../views/ExchangeRates.vue'
import Login from '../views/Login.vue'
import UserManagement from '../views/admin/UserManagement.vue'
import AllHoldings from '../views/admin/AllHoldings.vue'

const routes = [
  {
    path: '/login',
    name: 'Login',
    component: Login,
    meta: { requiresAuth: false }
  },
  {
    path: '/',
    name: 'Dashboard',
    component: Dashboard,
    meta: { requiresAuth: true }
  },
  {
    path: '/transactions',
    name: 'Transactions',
    component: Transactions,
    meta: { requiresAuth: true }
  },
  {
    path: '/holdings',
    name: 'Holdings',
    component: Holdings,
    meta: { requiresAuth: true }
  },
  {
    path: '/corporate-actions',
    name: 'CorporateActions',
    component: CorporateActions,
    meta: { requiresAuth: true }
  },
  {
    path: '/statistics',
    name: 'Statistics',
    component: Statistics,
    meta: { requiresAuth: true }
  },
  {
    path: '/exchange-rates',
    name: 'ExchangeRates',
    component: ExchangeRates,
    meta: { requiresAuth: true }
  },
  {
    path: '/admin/users',
    name: 'UserManagement',
    component: UserManagement,
    meta: { requiresAuth: true, requiresAdmin: true }
  },
  {
    path: '/admin/holdings',
    name: 'AllHoldings',
    component: AllHoldings,
    meta: { requiresAuth: true, requiresAdmin: true }
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

// Global navigation guard
router.beforeEach(async (to, from, next) => {
  const authStore = useAuthStore()

  // 如果有token但未认证，尝试恢复状态（防御性检查）
  if (!authStore.isAuthenticated && localStorage.getItem('token')) {
    await authStore.checkAuth()
  }

  // Check if route requires authentication
  if (to.meta.requiresAuth !== false) {
    // Check if user is authenticated
    if (!authStore.isAuthenticated) {
      // Try to restore auth from localStorage
      const isAuthenticated = await authStore.checkAuth()

      if (!isAuthenticated) {
        // Not authenticated, redirect to login
        ElMessage.warning('请先登录')
        next({
          path: '/login',
          query: { redirect: to.fullPath }
        })
        return
      }
    }

    // Check if route requires admin access
    if (to.meta.requiresAdmin && !authStore.isAdmin) {
      ElMessage.error('您没有访问该页面的权限')
      next('/')
      return
    }
  } else {
    // Route doesn't require auth (e.g., login page)
    // If user is already authenticated, redirect to home
    if (to.path === '/login' && authStore.isAuthenticated) {
      next('/')
      return
    }
  }

  next()
})

export default router
