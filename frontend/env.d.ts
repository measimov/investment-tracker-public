/// <reference types="vite/client" />

declare global {
  interface ImportMetaEnv {
    readonly VITE_API_URL?: string
  }

  interface ImportMeta {
    readonly env: ImportMetaEnv
  }
}

// 项目内部对 axios 请求配置的扩展字段（拦截器约定）。
declare module 'axios' {
  export interface AxiosRequestConfig {
    /** 置为 true 时跳过全局错误通知（由响应拦截器读取） */
    skipGlobalErrorNotification?: boolean
    /** 请求拦截器写入的元数据，用于连接状态恢复判断 */
    metadata?: { startedAt: number }
  }
}

// 路由 meta 的强类型（router/index.ts 与导航守卫共用）。
declare module 'vue-router' {
  interface RouteMeta {
    requiresAuth?: boolean
    requiresAdmin?: boolean
  }
}

export {}
