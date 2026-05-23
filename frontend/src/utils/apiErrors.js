export function getApiErrorMessage(error, fallback = '请求失败，请稍后重试') {
  if (error?.userMessage) {
    return error.userMessage
  }

  const detail = error?.response?.data?.detail

  if (typeof detail === 'string' && detail.trim()) {
    return detail
  }

  if (Array.isArray(detail) && detail.length > 0) {
    return detail
      .map((item) => item?.msg || item?.message || String(item))
      .filter(Boolean)
      .join('；')
  }

  if (detail && typeof detail === 'object') {
    return detail.message || detail.error || fallback
  }

  if (error?.message) {
    return error.message
  }

  return fallback
}

export function normalizeApiError(error) {
  let userMessage

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
    originalError: error,
    userMessage,
    response: error.response,
    config: error.config,
    code: error.code,
    isAxiosError: error.isAxiosError
  })
}
