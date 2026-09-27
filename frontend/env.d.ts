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
    /**
     * 置为 true 时 401 不触发拦截器的整页跳转（由调用方自己处理）：
     * 路由守卫里的会话探测（/auth/me）与登录请求本身——否则拦截器整页跳转与
     * 守卫的 next('/login') 同时发生，页面刷两次、「请先登录」一闪即逝（#219）
     */
    skipAuthRedirect?: boolean
    /** 请求拦截器写入的元数据，用于连接状态恢复判断 */
    metadata?: { startedAt: number }
  }
}

// 路由 meta 的强类型（router/index.ts 与导航守卫共用）。
declare module 'vue-router' {
  interface RouteMeta {
    requiresAuth?: boolean
    requiresAdmin?: boolean
    /** 页面名：router.afterEach 设为「页面名 · 投资追踪系统」；函数形式可带路由参数 */
    title?: string | ((to: import('vue-router').RouteLocationNormalized) => string)
    /** 导航高亮归属：嵌套/详情路由（如标的档案）点亮其所属菜单项的 path */
    nav?: string
  }
}

export {}
