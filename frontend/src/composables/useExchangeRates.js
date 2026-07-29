import { ref } from 'vue'
import api from '../api'

/**
 * 汇率加载与换算（基准货币 CNY）。
 * 返回的 convertToCNY / convertToUSD 读取最新的 exchangeRates，可直接用于 computed。
 */
export function useExchangeRates() {
  const exchangeRates = ref({})

  async function loadExchangeRates() {
    try {
      const response = await api.getLatestRates()
      const rates = {}
      Object.entries(response.data.rates).forEach(([currency, rate]) => {
        rates[currency] = parseFloat(rate)
      })
      exchangeRates.value = rates
    } catch (error) {
      console.error('加载汇率失败', error)
    }
  }

  function convertToCNY(amount, currency) {
    if (!currency || currency === 'CNY') return amount
    const rate = exchangeRates.value[currency]
    if (!rate) return amount
    return amount * rate
  }

  function convertToUSD(amountCNY) {
    const usdRate = exchangeRates.value['USD']
    if (!usdRate || usdRate === 0) return 0
    return amountCNY / usdRate
  }

  return { exchangeRates, loadExchangeRates, convertToCNY, convertToUSD }
}
