import { ref } from 'vue'
import api from '../api'

/**
 * 汇率加载与换算（基准货币 CNY）。
 * 返回的 convertToCNY / convertToUSD 读取最新的 exchangeRates，可直接用于 computed。
 */
export function useExchangeRates() {
  const exchangeRates = ref<Record<string, number>>({})

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
