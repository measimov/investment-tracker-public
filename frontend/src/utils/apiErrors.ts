import type { AxiosError, AxiosRequestConfig, AxiosResponse } from 'axios'

/** normalizeApiError 的产物：带用户可读消息的 Error，保留原始 Axios 错误上下文。 */
export interface NormalizedApiError extends Error {
  userMessage: string
  response?: AxiosResponse
  config?: AxiosRequestConfig
  code?: string
  isAxiosError?: boolean
  originalError: unknown
}

export function isApiError(e: unknown): e is NormalizedApiError {
  return e instanceof Error && typeof (e as NormalizedApiError).userMessage === 'string'
}

interface DetailItem {
  msg?: string
  message?: string
}

export function getApiErrorMessage(error: unknown, fallback = '请求失败，请稍后重试'): string {
  const err = error as Partial<NormalizedApiError> | null | undefined

  if (err?.userMessage) {
    return err.userMessage
  }

  const detail = err?.response?.data?.detail

  if (typeof detail === 'string' && detail.trim()) {
    return detail
  }

  if (Array.isArray(detail) && detail.length > 0) {
    return detail
      .map((item: DetailItem | null | undefined) => item?.msg || item?.message || String(item))
      .filter(Boolean)
      .join('；')
  }

  if (detail && typeof detail === 'object') {
    return detail.message || detail.error || fallback
  }

  if (err?.message) {
    return err.message
  }

  return fallback
}

export function normalizeApiError(error: AxiosError): NormalizedApiError {
  let userMessage: string

  if (error.code === 'ECONNABORTED') {
    userMessage = '请求超时，请稍后重试'
  } else if (!error.response) {
    userMessage = '网络连接失败，请检查网络'
  } else if (error.response.status === 403) {
    userMessage = getApiErrorMessage(error, '没有权限执行此操作')
  } else if (error.response.status === 503) {
    userMessage = getApiErrorMessage(error, '服务暂时不可用，请稍后重试')
  } else if (error.response.status >= 500) {
    userMessage = getApiErrorMessage(error, '服务器错误，请稍后重试')
  } else {
    userMessage = getApiErrorMessage(error, error.message)
  }

  return Object.assign(new Error(userMessage), {
    originalError: error as unknown,
    userMessage,
    response: error.response,
    config: error.config,
    code: error.code,
    isAxiosError: error.isAxiosError
  })
}
