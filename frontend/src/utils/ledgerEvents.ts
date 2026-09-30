/**
 * 账本数据缓存的失效信号（#268）。
 *
 * 持仓/交易 store 按查询参数缓存整页数据，但失效只靠各页面零散调用，而登出/切换用户时
 * 根本没人清：家人共用浏览器时，B 登录后打开持仓页看到的是 A 的持仓。这里集中两类信号：
 *
 * - `ledger-mutated`：账本写端点成功（由 api 拦截器按 isLedgerMutation 统一发出）；
 * - `session-changed`：登出、会话失效、登录成另一个用户（由 auth store 发出）。
 *
 * 独立成无依赖模块，是为了让 api 拦截器与 store 都能引用而不形成循环导入。
 * 另有一个代际计数：请求发起时抓取 `dataEpoch()`，响应回来时代际已变（期间发生过写入
 * 或换了用户）就不得写回缓存——否则 A 的慢响应会在 B 登录后把 A 的数据塞回去。
 */

export type LedgerEvent = 'ledger-mutated' | 'session-changed'

type Listener = () => void

const listeners: Record<LedgerEvent, Set<Listener>> = {
  'ledger-mutated': new Set(),
  'session-changed': new Set()
}
let epoch = 0

export function onLedgerEvent(event: LedgerEvent, listener: Listener): () => void {
  listeners[event].add(listener)
  return () => listeners[event].delete(listener)
}

export function emitLedgerEvent(event: LedgerEvent): void {
  epoch += 1
  listeners[event].forEach((listener) => listener())
}

export function dataEpoch(): number {
  return epoch
}

export function isDataEpochCurrent(captured: number): boolean {
  return captured === epoch
}
