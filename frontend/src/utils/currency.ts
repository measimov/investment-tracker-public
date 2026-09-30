/**
 * 货币代码列表：汇率管理页的选项源，也是 formatCurrency 币种符号的唯一来源。
 * 日元用 JP¥，避免与人民币 ¥ 混淆。
 */
export interface CurrencyOption {
  code: string
  name: string
  symbol: string
}

export const CURRENCIES: CurrencyOption[] = [
  { code: 'CNY', name: '人民币', symbol: '¥' },
  { code: 'USD', name: '美元', symbol: '$' },
  { code: 'HKD', name: '港币', symbol: 'HK$' },
  { code: 'SGD', name: '新加坡元', symbol: 'S$' },
  { code: 'EUR', name: '欧元', symbol: '€' },
  { code: 'GBP', name: '英镑', symbol: '£' },
  { code: 'JPY', name: '日元', symbol: 'JP¥' }
]

/**
 * 账本（交易/公司行动/现金事件/账户/规则/对账快照）可选的币种——`CURRENCIES` 里实际记账的
 * 那几种。此前三处各写一份（#284）；要加币种在这里加。
 */
export const LEDGER_CURRENCIES = ['CNY', 'HKD', 'USD', 'SGD'] as const

/** 下拉选项：`CNY (人民币)` */
export const LEDGER_CURRENCY_OPTIONS = LEDGER_CURRENCIES.map((code) => ({
  value: code,
  label: `${code} (${CURRENCIES.find((item) => item.code === code)?.name ?? code})`
}))
