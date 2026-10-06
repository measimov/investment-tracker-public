import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useXueqiuCapabilitiesStore } from '../stores/xueqiuCapabilities'
import { ElMessage } from 'element-plus'
import { MARKETS } from '../utils/securities'

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
const SystemAlerts = () => import('../views/admin/SystemAlerts.vue')

// meta.title：标签页标题的页面名（与导航文案一致）；meta.nav：不在导航里的
// 页面点亮哪个菜单项
const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'Login',
    component: Login,
    meta: { requiresAuth: false, title: '登录' }
  },
  {
    path: '/',
    name: 'Dashboard',
    component: Dashboard,
    meta: { requiresAuth: true, title: '仪表盘' }
  },
  {
    path: '/transactions',
    name: 'Transactions',
    component: Transactions,
    meta: { requiresAuth: true, title: '交易记录' }
  },
  {
    path: '/account-data',
    name: 'AccountData',
    component: AccountData,
    meta: { requiresAuth: true, title: '账户数据' }
  },
  {
    path: '/holdings',
    name: 'Holdings',
    component: Holdings,
    meta: { requiresAuth: true, title: '当前持仓' }
  },
  {
    path: '/watchlist',
    name: 'Watchlist',
    component: () => import('../views/Watchlist.vue'),
    meta: { requiresAuth: true, title: '观察清单' }
  },
  {
    path: '/opinions',
    name: 'Opinions',
    component: () => import('../views/Opinions.vue'),
    meta: { requiresAuth: true, title: '雪球观点' }
  },
  {
    path: '/securities/:market/:symbol',
    name: 'SecurityDetail',
    component: () => import('../views/SecurityDetail.vue'),
    // 市场不认识（参数颠倒 /securities/00700/港股 之类）→ NotFound，而不是渲染一个标题为
    // 「港股 00700」、又提示「该市场暂不支持」的自相矛盾页面（#286）
    beforeEnter: (to) =>
      (MARKETS as readonly string[]).includes(String(to.params.market))
        ? true
        : { name: 'NotFound', params: { pathMatch: to.path.slice(1).split('/') } },
    meta: {
      requiresAuth: true,
      // 详情页不在导航里：点亮「当前持仓」；标题带上代码，多开几个标的时可区分
      nav: '/holdings',
      title: (to) => `${String(to.params.symbol ?? '')} · 标的档案`
    }
  },
  {
    path: '/corporate-actions',
    name: 'CorporateActions',
    component: CorporateActions,
    meta: { requiresAuth: true, title: '公司行动' }
  },
  {
    path: '/reports',
    name: 'Reports',
    component: () => import('../views/Reports.vue'),
    meta: { requiresAuth: true, title: 'AI 复盘' }
  },
  {
    path: '/statistics',
    name: 'Statistics',
    component: Statistics,
    meta: { requiresAuth: true, title: '统计分析' }
  },
  {
    path: '/exchange-rates',
    name: 'ExchangeRates',
    component: ExchangeRates,
    meta: { requiresAuth: true, title: '汇率管理' }
  },
  {
    path: '/admin/users',
    name: 'UserManagement',
    component: UserManagement,
    meta: { requiresAuth: true, requiresAdmin: true, title: '用户管理' }
  },
  {
    path: '/admin/holdings',
    name: 'AllHoldings',
    component: AllHoldings,
    meta: { requiresAuth: true, requiresAdmin: true, title: '查看所有持仓' }
  },
  {
    path: '/admin/alerts',
    name: 'SystemAlerts',
    component: SystemAlerts,
    meta: { requiresAuth: true, requiresAdmin: true, title: '系统告警' }
  },
  {
    // 兜底 404，必须放在最后。不要求登录：输错地址应该看到「页面不存在」，
    // 而不是被踢去登录页、登录回跳后再落到一个空白页
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: () => import('../views/NotFound.vue'),
    meta: { requiresAuth: false, title: '页面不存在' }
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

// Global navigation guard
// 未登录的跳转只在这里发生：会话探测（/auth/me）带 skipAuthRedirect，
// 拦截器不再同时整页跳转（此前两处一起跳，页面刷两次、提示一闪即逝，#219）
router.beforeEach(async (to, from, next) => {
  const authStore = useAuthStore()

  if (!authStore.sessionChecked) {
    await authStore.checkAuth()
  }

  // Check if route requires authentication
  if (to.meta.requiresAuth !== false) {
    if (!authStore.isAuthenticated) {
      // 服务不可用时拦截器已弹过全局通知并亮起连接横幅，不再误导成「请先登录」
      if (authStore.lastAuthCheck !== 'unavailable') ElMessage.warning('请先登录')
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
    if (to.name === 'Opinions' && !authStore.isAdmin) {
      const capabilities = useXueqiuCapabilitiesStore()
      await capabilities.load()
      if (!capabilities.showOpinions) {
        next('/')
        return
      }
    }
  } else {
    // Route doesn't require auth (e.g., login page)
    // If user is already authenticated, redirect to home
    if (to.path === '/login' && authStore.isAuthenticated) {
      // 已登录还带着 redirect（如另一个标签页刚续上会话）：直接兑现回跳
      const target = to.query.redirect
      if (typeof target === 'string' && target.startsWith('/') && !target.startsWith('//')) {
        next(target)
      } else {
        next('/')
      }
      return
    }
  }

  next()
})

const APP_TITLE = '投资追踪系统'

// 标签页标题「页面名 · 投资追踪系统」：此前所有页都叫「投资追踪系统」，多开标签无法区分
router.afterEach((to) => {
  const title = typeof to.meta.title === 'function' ? to.meta.title(to) : to.meta.title
  document.title = title ? `${title} · ${APP_TITLE}` : APP_TITLE
})

export default router
