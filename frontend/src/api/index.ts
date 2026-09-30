/**
 * API 默认导出（barrel）：按领域拆分的方法表合成一个 `api` 对象，调用方照旧 `api.xxx()`（#284）。
 * axios 实例与拦截器在 client.ts。
 */
import { adminApi } from './admin'
import { authApi } from './auth'
import { importsApi } from './imports'
import { ledgerApi } from './ledger'
import { marketApi } from './market'
import { researchApi } from './research'
import { statisticsApi } from './statistics'

const api = {
  ...authApi,
  ...ledgerApi,
  ...importsApi,
  ...researchApi,
  ...statisticsApi,
  ...marketApi,
  ...adminApi
}

export default api
