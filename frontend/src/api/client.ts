/**
 * axios 客户端：凭据、CSRF 头、401 跳转、全局错误通知去重与账本变更事件（#284 由 api/index.ts 拆出）。
 * 领域方法在同目录各模块，index.ts 汇总为默认导出 `api`。
 */
import axios, { type AxiosError, type AxiosRequestConfig } from 'axios'
import type { BrokerImportResult } from '@/types'
import { ElNotification } from 'element-plus'
import { useAppStatusStore } from '../stores/appStatus'
import { getApiErrorMessage, normalizeApiError, type NormalizedApiError } from '../utils/apiErrors'
import { emitLedgerEvent } from '../utils/ledgerEvents'
import { invalidateSecuritySearchCache, isLedgerMutation } from '../utils/securitySearchCache'

const CSRF_COOKIE_NAME = 'investment_csrf'
const SAFE_METHODS = new Set(['get', 'head', 'options'])

function getCookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`
  const item = document.cookie.split('; ').find((value) => value.startsWith(prefix))
  return item ? decodeURIComponent(item.slice(prefix.length)) : null
}

let lastGlobalErrorKey = ''
let lastGlobalErrorAt = 0

// 滑动会话续期：有 API 活动时每隔一段时间静默轮换会话 Cookie，
// 活跃用户不会被 30 分钟令牌过期打断；闲置用户按原有效期自然登出。
const SESSION_RENEW_INTERVAL_MS = 10 * 60 * 1000
let lastSessionRenewAt = 0

function maybeRenewSession(config: AxiosRequestConfig | undefined): void {
  const url = config?.url || ''
  if (url.startsWith('/auth/')) {
    // 登录/续期本身就是最新会话，登出后无会话可续
    if (url === '/auth/login' || url === '/auth/refresh') {
      lastSessionRenewAt = Date.now()
    }
    return
  }
  if (!localStorage.getItem('user')) return
  if (Date.now() - lastSessionRenewAt < SESSION_RENEW_INTERVAL_MS) return
  // 先记时间戳：失败也等满间隔再试，避免后端异常时逐请求重试
  lastSessionRenewAt = Date.now()
  apiClient.post('/auth/refresh', null, { skipGlobalErrorNotification: true }).catch(() => {
    // 静默失败：会话真过期时常规 401 流程会接管
  })
}

function getStatusStore(): ReturnType<typeof useAppStatusStore> | null {
  try {
    return useAppStatusStore()
  } catch (error) {
    if (import.meta.env.DEV) {
      console.warn('Failed to access app status store:', error)
    }
    return null
  }
}

// 502/504 = 反向代理找不到后端（容器重启/部署窗口），与 503 同属「服务不可用」
function isUnavailableStatus(status: number | undefined): boolean {
  return status === 502 || status === 503 || status === 504
}

function notifyGlobalError(error: NormalizedApiError): void {
  if (error.config?.skipGlobalErrorNotification) return

  const status = error.response?.status
  const shouldNotify =
    !error.response ||
    error.code === 'ECONNABORTED' ||
    status === 403 ||
    status === 500 ||
    isUnavailableStatus(status)

  if (!shouldNotify) return
  // 本次（或 5 秒内同键的上一次）已经弹过通知：view 层 showApiError 据此跳过，
  // 同一个错误不再「右上角通知 + 顶部消息」各弹一次
  error.globallyNotified = true

  const message = getApiErrorMessage(error)
  const key = `${status || error.code || 'network'}:${message}`
  const now = Date.now()
  if (key === lastGlobalErrorKey && now - lastGlobalErrorAt < 5000) return

  lastGlobalErrorKey = key
  lastGlobalErrorAt = now

  ElNotification.error({
    title: isUnavailableStatus(status) ? '服务不可用' : '请求失败',
    message,
    duration: 4500
  })
}

export const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_URL || '/api',
  timeout: 120000, // Increased to 120 seconds for batch operations
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json'
  }
})

// Add request interceptor for authentication and logging
apiClient.interceptors.request.use(
  (config) => {
    config.metadata = {
      ...config.metadata,
      startedAt: Date.now()
    }

    const method = config.method?.toLowerCase() || 'get'
    if (!SAFE_METHODS.has(method)) {
      const csrfToken = getCookie(CSRF_COOKIE_NAME)
      if (csrfToken) {
        config.headers['X-CSRF-Token'] = csrfToken
      }
    }

    if (import.meta.env.DEV && (config.url?.includes('refresh') || config.url?.includes('batch'))) {
      console.log(`[API Request] ${config.method?.toUpperCase()} ${config.url}`)
    }
    return config
  },
  (error) => {
    return Promise.reject(error)
  }
)

// Add response interceptor for better error handling and authentication
apiClient.interceptors.response.use(
  (response) => {
    const statusStore = getStatusStore()
    if (statusStore?.shouldClearForRequest(response.config?.metadata?.startedAt)) {
      statusStore.clear()
    }
    maybeRenewSession(response.config)
    // 账本写端点（交易/自选/导入/账户/持仓）成功即让标的检索缓存失效：
    // 集中在这里，不靠各页面零散调用（评审 P2：删自选/编辑名称/删交易/导入都曾漏掉）
    if (isLedgerMutation(response.config?.method, response.config?.url)) {
      invalidateSecuritySearchCache()
      // 持仓/交易 store 的缓存同源失效（#268）：补录成本、接受分红建议等都曾漏掉
      emitLedgerEvent('ledger-mutated')
    }
    return response
  },
  (error: AxiosError) => {
    const normalizedError = normalizeApiError(error)
    const statusStore = getStatusStore()

    // Handle 401 Unauthorized - clear auth and redirect to login.
    // skipAuthRedirect 的请求（守卫里的会话探测、登录本身）由调用方处理：
    // 路由守卫会 next('/login') 并提示，这里再整页跳转就刷两次（#219）
    if (normalizedError.response?.status === 401 && !normalizedError.config?.skipAuthRedirect) {
      // Clear authentication
      localStorage.removeItem('user')

      // Redirect to login page if not already there；带上当前地址，
      // 登录成功后由 Login.vue 读取 ?redirect= 回跳（会话过期不再丢页面）
      if (window.location.pathname !== '/login') {
        const redirect = encodeURIComponent(
          window.location.pathname + window.location.search + window.location.hash
        )
        window.location.href = `/login?redirect=${redirect}`
      }
    }

    if (!normalizedError.response || normalizedError.code === 'ECONNABORTED') {
      statusStore?.markConnectionLost(normalizedError.userMessage)
    } else if (isUnavailableStatus(normalizedError.response.status)) {
      statusStore?.markMaintenance(normalizedError.userMessage)
    }

    notifyGlobalError(normalizedError)
    return Promise.reject(normalizedError)
  }
)

export type QueryParams = Record<string, unknown>
type UploadFields = Record<string, string | number | null | undefined>

export function uploadFile<T = BrokerImportResult>(
  endpoint: string,
  file: File | Blob,
  fields: UploadFields = {}
) {
  const formData = new FormData()
  formData.append('file', file)
  Object.entries(fields).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== '') {
      formData.append(key, String(value))
    }
  })
  return apiClient.post<T>(endpoint, formData, {
    headers: { 'Content-Type': 'multipart/form-data' }
  })
}
