/**
 * 跨页面共用的枚举文案（#219/#220）：交易类型、公司行动类型、现金事件类型与
 * 「没有归属账户」的叫法此前各页各写一份，已经漂移（待分配/未分配/未关联账户/
 * 未指定…；「股票股息/红股」vs「股票股息」）。选项列表一律从这里的映射生成。
 */

export type TagKind = 'success' | 'warning' | 'info' | 'primary' | 'danger'

/** 没有归属账户的持仓/交易/行动（后端 broker_account_id IS NULL）——与后端文案一致 */
export const UNASSIGNED_ACCOUNT_LABEL = '未指定账户'
// 账户筛选/选择里「未指定账户」的哨兵值（唯一定义，#284：此前交易页用 'UNASSIGNED'、
// 公司行动/持仓/转仓用 'unassigned'）。发给后端时换成 unassigned_account=true 或 null
export const UNASSIGNED_ACCOUNT = 'unassigned' as const
export type UnassignedAccount = typeof UNASSIGNED_ACCOUNT

interface AccountLike {
  id?: number | null
  account_name?: string | null
  broker?: string | null
  account_number_masked?: string | null
}

// 账户号展示脱敏（#286：此前 IBKR 尾号打了码、账户名里却是完整账号；招商的股东代码完全不打码）。
// 只改展示，库里的值不动（导入器按原值匹配账户）。判据：≥6 位字母数字、其中 ≥5 位数字的串
const ID_TOKEN = /[A-Za-z0-9]{6,}/g

function maskToken(token: string): string {
  if ((token.match(/\d/g) || []).length < 5) return token
  if (token.includes('*')) return token
  const head = /^[A-Za-z]/.test(token) ? token[0] : ''
  return `${head}***${token.slice(-4)}`
}

/** 自由文本（账户名）里夹带的账号打码：`IBKR U12345678` → `IBKR U***5678` */
export function maskInlineIds(text: string | null | undefined): string {
  return (text || '').replace(ID_TOKEN, maskToken)
}

/** 账号字段：多个号码（招商一个资金账户挂多个股东代码）只显示第一个 + 数量 */
export function maskAccountNumber(text: string | null | undefined): string {
  const tokens = (text || '').split(/\s+/).filter(Boolean)
  if (!tokens.length) return ''
  const first = maskToken(tokens[0])
  return tokens.length > 1 ? `${first} 等 ${tokens.length} 个` : first
}

/** 账户简称：名称；没有名称退回「券商 尾号」，再退回「未命名账户」。表格单元格用它（#284/#286：
 *  此前四份实现输出各不相同，交易表的「名称 · 券商 · 尾号」把账户列撑成两三行）。 */
export function accountShortName(account: AccountLike | null | undefined): string {
  return (
    maskInlineIds(account?.account_name) ||
    [account?.broker, maskAccountNumber(account?.account_number_masked)]
      .filter(Boolean)
      .join(' ') ||
    '未命名账户'
  )
}

/** 账户全称「名称 · 券商 · 尾号」：下拉选项里用，同名账户靠券商与尾号区分。 */
export function accountOptionLabel(account: AccountLike): string {
  return (
    [
      maskInlineIds(account.account_name),
      account.broker,
      maskAccountNumber(account.account_number_masked)
    ]
      .filter(Boolean)
      .join(' · ') || '未命名账户'
  )
}

export type AccountListStatus = 'loading' | 'ready' | 'error'

/** 只有列表加载成功后，缺失的 id 才能显示为已删除。 */
export function accountLabel(
  accounts: readonly AccountLike[],
  id: unknown,
  { full = false, status = 'ready' }: { full?: boolean; status?: AccountListStatus } = {}
): string {
  if (id === null || id === undefined || id === '') return UNASSIGNED_ACCOUNT_LABEL
  const account = accounts.find((item) => String(item.id) === String(id))
  if (!account) {
    if (status === 'loading') return '账户加载中'
    if (status === 'error') return '账户暂不可用'
    return DELETED_ACCOUNT_LABEL
  }
  return full ? accountOptionLabel(account) : accountShortName(account)
}
/** 引用了已删除的券商账户 */
export const DELETED_ACCOUNT_LABEL = '已删除账户'

// 证券转仓（TRANSFER_OUT/IN）与现金侧的资金划转是两回事，文案必须能区分
export const TRANSACTION_TYPE_LABELS: Record<string, string> = {
  BUY: '买入',
  SELL: '卖出',
  TRANSFER_OUT: '转仓转出',
  TRANSFER_IN: '转仓转入'
}

export const TRANSACTION_TYPE_TAGS: Record<string, TagKind> = {
  BUY: 'info',
  SELL: 'info',
  TRANSFER_OUT: 'warning',
  TRANSFER_IN: 'info'
}

export function transactionTypeLabel(type: string): string {
  return TRANSACTION_TYPE_LABELS[type] || type
}

export function transactionTypeTag(type: string): TagKind {
  return TRANSACTION_TYPE_TAGS[type] || 'info'
}

export const ACTION_TYPE_LABELS: Record<string, string> = {
  CASH_DIVIDEND: '现金股息',
  STOCK_DIVIDEND: '股票股息',
  RIGHTS_ISSUE: '配股',
  STOCK_SPLIT: '拆股',
  REVERSE_SPLIT: '合股',
  BONUS_ISSUE: '送股',
  OPENING_POSITION: '期初建仓/转托管转入'
}

export const ACTION_TYPE_TAGS: Record<string, TagKind> = {
  CASH_DIVIDEND: 'success',
  STOCK_DIVIDEND: 'warning',
  RIGHTS_ISSUE: 'info',
  STOCK_SPLIT: 'primary',
  REVERSE_SPLIT: 'primary',
  BONUS_ISSUE: 'warning',
  OPENING_POSITION: 'info'
}

export function actionTypeLabel(type: string): string {
  return ACTION_TYPE_LABELS[type] || type
}

/** 未知类型返回 undefined = el-tag 默认样式 */
export function actionTypeTag(type: string): TagKind | undefined {
  return ACTION_TYPE_TAGS[type]
}

export const CASH_EVENT_TYPE_LABELS: Record<string, string> = {
  DEPOSIT: '入金',
  WITHDRAWAL: '出金',
  INTEREST: '利息',
  FEE: '费用',
  TAX: '税费',
  TRANSFER_IN: '资金转入',
  TRANSFER_OUT: '资金转出',
  FX_IN: '换汇转入',
  FX_OUT: '换汇转出',
  OTHER: '其他'
}

export function cashEventTypeLabel(type: string | null | undefined): string {
  if (!type) return '—'
  return CASH_EVENT_TYPE_LABELS[type] || type
}

// 标的事件（security_events 全局表）：持仓页角标与事件筛选共用
export const SECURITY_EVENT_TYPE_LABELS: Record<string, string> = {
  EARNINGS_DISCLOSURE: '财报披露',
  DIVIDEND_PLAN: '分红预案',
  SHARE_UNLOCK: '限售解禁'
}

export function securityEventTypeLabel(type: string): string {
  return SECURITY_EVENT_TYPE_LABELS[type] || type
}

/** 映射 → el-option 列表（保持声明顺序）；exclude 用于 CMB 规则这类子集 */
export function optionsOf(
  labels: Record<string, string>,
  exclude: readonly string[] = []
): { label: string; value: string }[] {
  return Object.entries(labels)
    .filter(([value]) => !exclude.includes(value))
    .map(([value, label]) => ({ label, value }))
}
