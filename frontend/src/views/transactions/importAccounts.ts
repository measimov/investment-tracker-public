/**
 * 券商导入「导入到」下拉的候选账户（#220）。
 *
 * 按账户的券商名匹配关键字；此前只认「招商 / IBKR / 东方」三个子串，账户券商名写成
 * 「招行 / 盈透 / 东财」或账户已停用时下拉直接为空且不解释。这里放宽关键字，并在
 * 候选为空时给出原因，让界面能提示去账户数据页处理。
 */

import type { BrokerAccount } from '@/types'

export type BrokerImportMode = 'cmb' | 'ibkr' | 'eastmoney'

export const BROKER_IMPORT_KEYWORDS: Record<BrokerImportMode, string[]> = {
  cmb: ['招商', '招行', 'CMB', 'CHINA MERCHANTS'],
  ibkr: ['IBKR', 'INTERACTIVE', '盈透'],
  eastmoney: ['东方财富', '东财', 'EASTMONEY', '东方']
}

export const BROKER_IMPORT_NAMES: Record<BrokerImportMode, string> = {
  cmb: '招商证券',
  ibkr: 'IBKR',
  eastmoney: '东方财富'
}

export interface ImportAccountChoice {
  options: BrokerAccount[]
  /** 候选为空的原因；有候选时为 null */
  emptyReason: string | null
}

function matchesBroker(account: BrokerAccount, keywords: string[]): boolean {
  const broker = String(account.broker || '').toUpperCase()
  return keywords.some((keyword) => broker.includes(keyword.toUpperCase()))
}

export function importAccountChoice(
  accounts: BrokerAccount[],
  mode: string
): ImportAccountChoice | null {
  const keywords = BROKER_IMPORT_KEYWORDS[mode as BrokerImportMode]
  if (!keywords) return null
  const matched = accounts.filter((account) => matchesBroker(account, keywords))
  const options = matched.filter((account) => account.is_active !== false)
  if (options.length) return { options, emptyReason: null }
  const name = BROKER_IMPORT_NAMES[mode as BrokerImportMode]
  const emptyReason = matched.length
    ? `券商为「${name}」的账户都已停用，停用账户不能导入；请先在账户数据页启用`
    : `还没有券商为「${name}」的账户（按账户的券商名匹配）；请先在账户数据页新建账户`
  return { options, emptyReason }
}
