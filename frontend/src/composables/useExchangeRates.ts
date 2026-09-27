import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'

/**
 * 汇率加载与换算（基准货币 CNY）。
 * 返回的 convertToCNY / convertToUSD 读取最新的 exchangeRates，可直接用于 computed。
 *
 * 状态是**模块级单例**（PR #177 复审）：此前每次调用各建一份私有 ref，
 * "A 实例加载、B 实例换算"时 B 永远拿不到汇率，convertToCNY 静默回退原币
 * 数值——统计页拆分后 useDistributionStats 与 DistributionSection 正是踩了
 * 这一脚（USD 100 显示成约 ¥100，分母与占比一起错）。汇率本就是全局数据，
 * 任一调用方 load 后全站共享；load 语义不变（每次调用都重新拉取，汇率页
 * 改完切回来仍能拿到新值）。
 */
const exchangeRates = ref<Record<string, number>>({})
const loadFailed = ref(false)

export function useExchangeRates() {
  async function loadExchangeRates(): Promise<void> {
    try {
      const response = await api.getLatestRates()
      const rates: Record<string, number> = {}
      Object.entries(response.data.rates as Record<string, string | number>).forEach(
        ([currency, rate]) => {
          rates[currency] = parseFloat(String(rate))
        }
      )
      exchangeRates.value = rates
      loadFailed.value = false
    } catch (error) {
      console.error('加载汇率失败', error)
      // 只提示一次：多个页面/组件同时 load 时不刷屏
      if (!loadFailed.value) ElMessage.warning('汇率加载失败，外币金额暂无法折算为人民币')
      loadFailed.value = true
    }
  }

  function hasRate(currency: string | null | undefined): boolean {
    return !currency || currency === 'CNY' || Boolean(exchangeRates.value[currency])
  }

  /**
   * 折人民币；缺汇率返回 null（#219）。此前静默返回原币数值，HK$100 会显示成
   * 「≈¥100」并一起污染合计与占比——调用方要把 null 显示成「—」或剔除并提示。
   */
  function convertToCNY(amount: number, currency: string | null | undefined): number | null {
    if (!currency || currency === 'CNY') return amount
    const rate = exchangeRates.value[currency]
    if (!rate) return null
    return amount * rate
  }

  /** 人民币折美元；缺 USD 汇率返回 null（此前返回 0，合计显示成 $0.00） */
  function convertToUSD(amountCNY: number): number | null {
    const usdRate = exchangeRates.value['USD']
    if (!usdRate) return null
    return amountCNY / usdRate
  }

  return { exchangeRates, loadFailed, loadExchangeRates, hasRate, convertToCNY, convertToUSD }
}
