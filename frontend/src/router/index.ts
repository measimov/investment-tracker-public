import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { ElMessage } from 'element-plus'

// Route-level code splitting: each view is loaded on demand so the initial
// bundle stays small (the Statistics view with its charts is the heaviest).
const Dashboard = () => import('../views/Dashboard.vue')
const Transactions = () => import('../views/Transactions.vue')
const Holdings = () => import('../views/Holdings.vue')
const Statistics = () => import('../views/Statistics.vue')
const CorporateActions = () => import('../views/CorporateActions.vue')
const ExchangeRates = () => import('../views/ExchangeRates.vue')
const AccountData = () => import('../views/AccountData.vue')
const Login = () => import('../views/Login.vue')
const UserManagement = () => import('../views/admin/UserManagement.vue')
const AllHoldings = () => import('../views/admin/AllHoldings.vue')

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
    path: '/account-data',
    name: 'AccountData',
    component: AccountData,
    meta: { requiresAuth: true }
  },
  {
    path: '/holdings',
    name: 'Holdings',
    component: Holdings,
    meta: { requiresAuth: true }
  },
  {
    path: '/securities/:market/:symbol',
    name: 'SecurityDetail',
    component: () => import('../views/SecurityDetail.vue'),
    meta: { requiresAuth: true }
  },
  {
    path: '/corporate-actions',
    name: 'CorporateActions',
    component: CorporateActions,
    meta: { requiresAuth: true }
  },
  {
    path: '/reports',
    name: 'Reports',
    component: () => import('../views/Reports.vue'),
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

  if (!authStore.sessionChecked) {
    await authStore.checkAuth()
  }

  // Check if route requires authentication
  if (to.meta.requiresAuth !== false) {
    if (!authStore.isAuthenticated) {
      ElMessage.warning('请先登录')
      next({
        path: '/login',
        query: { redirect: to.fullPath }
      })
      return
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
