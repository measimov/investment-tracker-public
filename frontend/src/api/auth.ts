/** 登录、会话探测、改密码。 */
import { apiClient } from './client'
import type { LoginResponse, User } from '@/types'

export const authApi = {
  // Authentication
  login(username: string, password: string) {
    return apiClient.post<LoginResponse>(
      '/auth/login',
      { username, password },
      { skipAuthRedirect: true }
    )
  },
  logout() {
    return apiClient.post('/auth/logout')
  },
  // 会话探测：401 = 未登录，交给路由守卫统一跳转与提示
  getUserInfo() {
    // 会话探测用短超时：后端挂起（而非拒绝连接）时，「不可用」状态下每次导航都要等探测结束
    return apiClient.get<User>('/auth/me', { skipAuthRedirect: true, timeout: 10000 })
  },
  // 修改自己的密码：成功后后端吊销该用户全部会话（含当前这个）
  changePassword(oldPassword: string, newPassword: string) {
    return apiClient.put<{ message: string }>('/auth/me/password', {
      old_password: oldPassword,
      new_password: newPassword
    })
  }
}
