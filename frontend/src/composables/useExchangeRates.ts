import { ref } from 'vue'
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
    } catch (error) {
      console.error('加载汇率失败', error)
    }
  }

  function convertToCNY(amount: number, currency: string | null | undefined): number {
    if (!currency || currency === 'CNY') return amount
    const rate = exchangeRates.value[currency]
    if (!rate) return amount
    return amount * rate
  }

  function convertToUSD(amountCNY: number): number {
    const usdRate = exchangeRates.value['USD']
    if (!usdRate || usdRate === 0) return 0
    return amountCNY / usdRate
  }

  return { exchangeRates, loadExchangeRates, convertToCNY, convertToUSD }
}
