import { ElMessage } from 'element-plus'
import { getApiErrorMessage, type NormalizedApiError } from './apiErrors'

export interface ShowApiErrorOptions {
  /** 后端没有给出原因时的兜底文案 */
  fallback?: string
  /** 拼在原因前的上下文，如「保存失败」→「保存失败：原因」 */
  prefix?: string
}

/**
 * view 层的错误提示入口：拦截器已为这个错误弹过全局通知（403/5xx/断网）时不再
 * 重复弹 ElMessage（#219）。独立成文件而不放进 apiErrors.ts：后者是无 UI 依赖的
 * 纯函数、有单测。第二个参数给字符串即 fallback；要保留「xxx失败：原因」的上下文用
 * `{ prefix }`——此前各处手写 `ElMessage.error('xxx：' + getApiErrorMessage(e))`，
 * 绕开了全局通知去重，后端一挂同一个错误弹两次（#284）。
 */
export function showApiError(error: unknown, options?: string | ShowApiErrorOptions): void {
  if ((error as Partial<NormalizedApiError> | null)?.globallyNotified) return
  const { fallback, prefix } = typeof options === 'string' ? { fallback: options } : options || {}
  const message = getApiErrorMessage(error, fallback)
  ElMessage.error(prefix ? `${prefix}：${message}` : message)
}
