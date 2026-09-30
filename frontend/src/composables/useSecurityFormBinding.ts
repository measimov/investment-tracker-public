import type { SecurityResolveResponse, SecuritySearchItem } from '@/types'
import {
  followMarketCurrency,
  freeTextFormPatch,
  resolvedFormPatch,
  securityFormPatch,
  type SecurityFormFields
} from '@/utils/securities'

/**
 * 标的输入框（SecuritySelect）与市场/币种下拉的联动，交易表单与公司行动表单共用（#284：
 * 此前两处逐字重复 currencyAuto + 五个处理函数）。三种补丁语义见 utils/securities.ts。
 *
 * currencyAuto：币种是否仍是自动推导值（选候选 / 换市场 / 解析回填）。用户手选过一次
 * 币种就不再跟随市场；只挂在两个 el-select 的 @change（用户动作）上，编辑回填/重置这类
 * 程序赋值不触发——打开表单时由调用方 `setCurrencyAuto(新建 ? true : false)`。
 */
export function useSecurityFormBinding(form: SecurityFormFields) {
  let currencyAuto = true

  return {
    setCurrencyAuto(value: boolean) {
      currencyAuto = value
    },
    onSymbolSelected(item: SecuritySearchItem) {
      Object.assign(form, securityFormPatch(item))
      currencyAuto = true
    },
    onMarketChange(market: string) {
      // 自动态下推不出也要清空（评审 P1：默认 CNY 配加密货币会把 BTC 当 CNY 入账）
      form.currency = followMarketCurrency(form.currency, market, form.symbol, currencyAuto)
    },
    onCurrencyChange() {
      currencyAuto = false
    },
    onSymbolFreeText(payload: { symbol: string; lastPicked: SecuritySearchItem | null }) {
      Object.assign(form, freeTextFormPatch(form, payload))
    },
    onSymbolResolved(result: SecurityResolveResponse) {
      Object.assign(form, resolvedFormPatch(form, result))
    }
  }
}
