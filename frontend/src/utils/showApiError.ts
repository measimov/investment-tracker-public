import { ElMessage } from 'element-plus'
import { getApiErrorMessage, type NormalizedApiError } from './apiErrors'

/**
 * view 层的错误提示入口：拦截器已为这个错误弹过全局通知（403/5xx/断网）时不再
 * 重复弹 ElMessage（#219）。独立成文件而不放进 apiErrors.ts：后者是无 UI 依赖的
 * 纯函数、有单测。
 */
export function showApiError(error: unknown, fallback?: string): void {
  if ((error as Partial<NormalizedApiError> | null)?.globallyNotified) return
  ElMessage.error(getApiErrorMessage(error, fallback))
}
