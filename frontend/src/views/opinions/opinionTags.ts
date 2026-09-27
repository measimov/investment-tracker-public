/**
 * 雪球观点标签的展示样式（#221）。后端白名单见 `opinion_summary_prompts.ALLOWED_OPINION_TAGS`
 * （整体立场 / 近期变化 / 语境三层）。
 *
 * 配色原则：**不用红/绿表达多空**——全站是绿涨红跌（profitColor），同页「风险 高」也是红，
 * 此前「红 = 看多、绿 = 看空」与之正好相反。多空改用 primary / warning + ↑ / ↓ 前缀；
 * 变化类标签**先判断**（此前「近期转多/转空」被立场分支先截走，变化高亮只剩「新增关注」），
 * 用实心（dark）样式突出，是「最近发生了什么」的信号。
 */

export type OpinionTagType = 'primary' | 'warning' | 'info'
export type OpinionTagEffect = 'dark' | 'light' | 'plain'

export interface OpinionTagStyle {
  type: OpinionTagType
  effect: OpinionTagEffect
  /** 展示文案（带方向前缀） */
  label: string
  /** 近期变化类（角标/高亮用） */
  change: boolean
}

// 近期变化类标签：角标与高亮共用（与后端 ALLOWED_OPINION_TAGS 的变化层一致）
export const OPINION_CHANGE_TAGS: ReadonlySet<string> = new Set([
  '近期转多',
  '近期转空',
  '新增关注'
])

const BULLISH = new Set(['一致看多', '偏多'])
const BEARISH = new Set(['一致看空', '偏空'])

export function opinionTagStyle(tag: string): OpinionTagStyle {
  if (tag === '近期转多')
    return { type: 'primary', effect: 'dark', label: `↑ ${tag}`, change: true }
  if (tag === '近期转空')
    return { type: 'warning', effect: 'dark', label: `↓ ${tag}`, change: true }
  if (tag === '新增关注') return { type: 'info', effect: 'dark', label: `+ ${tag}`, change: true }
  if (BULLISH.has(tag))
    return { type: 'primary', effect: 'light', label: `↑ ${tag}`, change: false }
  if (BEARISH.has(tag))
    return { type: 'warning', effect: 'light', label: `↓ ${tag}`, change: false }
  return { type: 'info', effect: 'light', label: tag, change: false }
}

/** el-tag 的 type（持仓页角标等只需要颜色的地方） */
export function opinionTagType(tag: string): OpinionTagType {
  return opinionTagStyle(tag).type
}
