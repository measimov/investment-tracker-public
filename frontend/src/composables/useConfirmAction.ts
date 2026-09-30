import { ElMessage, ElMessageBox } from 'element-plus'
import { showApiError } from '@/utils/showApiError'

/**
 * 危险操作的确认 + 执行 + 提示 + 重载，全站一份（#284）。
 *
 * 此前各页手写 `ElMessageBox.confirm(...).then(...)`，多处没有 `.catch`：点「取消」控制台报
 * `Uncaught (in promise) cancel`；account-data 自己有一份 `makeRemover` 只在那里用。
 * `confirmAction` 把取消/关闭变成返回 false，不再以 rejection 的形式漏出去。
 */
export async function confirmAction(options: {
  title: string
  message: string
  confirmText?: string
}): Promise<boolean> {
  try {
    await ElMessageBox.confirm(options.message, options.title, {
      type: 'warning',
      confirmButtonText: options.confirmText ?? '确定',
      cancelButtonText: '取消'
    })
    return true
  } catch {
    return false // 取消或关闭
  }
}

/** 行操作工厂：确认 → 请求 → 成功提示 → 重载；message 可以是按行生成的函数。 */
export function makeConfirmedAction<T>(options: {
  title: string
  message: string | ((row: T) => string)
  confirmText?: string
  request: (row: T) => Promise<unknown>
  successMessage: string
  failureMessage: string
  reload: () => unknown
}) {
  return async (row: T) => {
    const message = typeof options.message === 'function' ? options.message(row) : options.message
    if (
      !(await confirmAction({ title: options.title, message, confirmText: options.confirmText }))
    ) {
      return
    }
    try {
      await options.request(row)
      ElMessage.success(options.successMessage)
      await options.reload()
    } catch (error) {
      showApiError(error, options.failureMessage)
    }
  }
}
