/** 管理：用户、系统告警、事件提醒、全员持仓。 */
import { apiClient } from './client'
import type {
  AdminHolding,
  AlertList,
  NotificationEventList,
  NotifyResult,
  User,
  UserCreate,
  UserUpdate
} from '@/types'

export const adminApi = {
  // User Management (Admin)
  getUsers() {
    return apiClient.get<User[]>('/users')
  },
  createUser(userData: UserCreate) {
    return apiClient.post<User>('/users', userData)
  },
  updateUser(userId: number | string, userData: UserUpdate) {
    return apiClient.put<User>(`/users/${userId}`, userData)
  },
  deleteUser(userId: number | string) {
    return apiClient.delete<void>(`/users/${userId}`)
  },
  resetUserPassword(userId: number | string, newPassword: string) {
    return apiClient.put(`/users/${userId}/password`, {
      new_password: newPassword
    })
  },

  // 系统告警（管理员）：推送渠道只返回脱敏地址
  getSystemAlerts() {
    return apiClient.get<AlertList>('/notifications/alerts')
  },
  runAlertChecks() {
    return apiClient.post<AlertList>('/notifications/check')
  },
  sendTestNotification() {
    return apiClient.post<NotifyResult>('/notifications/test')
  },
  // 最近的事件提醒（新分红建议、除净日临近、价格异动）
  getNotificationEvents(limit = 50) {
    return apiClient.get<NotificationEventList>('/notifications/events', { params: { limit } })
  },

  // Admin Holdings
  getAllHoldingsAdmin() {
    return apiClient.get<AdminHolding[]>('/holdings/admin/all')
  },
  getUserHoldingsAdmin(userId: number | string) {
    return apiClient.get<AdminHolding[]>(`/holdings/admin/users/${userId}`)
  }
}
