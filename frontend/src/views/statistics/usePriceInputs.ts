/**
 * 价格 what-if feature（issue #140：五件事之"价格输入弹窗"）：
 * 持仓价格行装载、当前价映射与批量保存。计算编排留在页面层
 * （calculate 同时驱动摘要与 analytics 两个 feature）。
 */

import { reactive } from 'vue'
import { ElMessage } from 'element-plus'
import { useHoldingsStore } from '@/stores/holdings'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { toNumber } from '@/utils/helpers'
import type { PriceInputRow } from './types'

export function usePriceInputs() {
  const holdingsStore = useHoldingsStore()
  const state = reactive({
    rows: [] as PriceInputRow[],
    dialogVisible: false,
    saving: false
  })

  async function loadHoldingsForPrice(options: { force?: boolean } = {}) {
    try {
      const holdings = await holdingsStore.fetchHoldings(
        {},
        {
          force: options?.force === true
        }
      )
      state.rows = holdings.map((h) => ({
        symbol: h.symbol,
        name: h.name,
        market: h.market,
        avg_cost: toNumber(h.avg_cost),
        // Use the database price if available; missing prices should stay empty.
        current_price:
          h.current_price && toNumber(h.current_price) > 0 ? toNumber(h.current_price) : null,
        quantity: toNumber(h.quantity)
      }))
    } catch (error) {
      ElMessage.error(getApiErrorMessage(error, '加载持仓失败'))
    }
  }

  function getCurrentPrices() {
    const prices: Record<string, number> = {}
    state.rows.forEach((item) => {
      if (item.current_price && item.current_price > 0) {
        prices[item.symbol] = item.current_price
      }
    })
    return prices
  }

  async function savePrices() {
    state.saving = true
    try {
      // Build updates array with symbol, market, and price
      const updates: Array<{ symbol: string; market: string; price: number }> = []
      state.rows.forEach((item) => {
        if (item.current_price && item.current_price > 0) {
          updates.push({
            symbol: item.symbol,
            market: item.market,
            price: item.current_price
          })
        }
      })

      if (updates.length === 0) {
        ElMessage.warning('没有可保存的价格')
        return
      }

      const response = await holdingsStore.batchUpdatePrices(updates)
      const result = response.data
      ElMessage.success(`成功保存 ${result.success_count} 个价格`)
      await loadHoldingsForPrice({ force: true })

      if (result.failed_count > 0) {
        console.error('保存失败的项:', result.failed_list)
      }
    } catch (error) {
      ElMessage.error('保存失败：' + getApiErrorMessage(error))
    } finally {
      state.saving = false
    }
  }

  return reactive({ state, loadHoldingsForPrice, getCurrentPrices, savePrices })
}

export type PriceInputsFeature = ReturnType<typeof usePriceInputs>
