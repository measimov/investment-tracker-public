/**
 * 价格 what-if feature（issue #140：五件事之"价格输入弹窗"）：
 * 持仓价格行装载、当前价映射与批量保存。计算编排留在页面层
 * （calculate 同时驱动摘要与 analytics 两个 feature）。
 *
 * 行按 `symbol:market` 合并各账户持仓、价格映射的键也是 `symbol:market`
 * （#218：此前按裸代码写键，多账户行/同码跨市场互相覆盖）。纯函数在 priceRows.ts。
 */

import { reactive } from 'vue'
import { ElMessage } from 'element-plus'
import { useHoldingsStore } from '@/stores/holdings'
import type { PriceInputRow } from './types'
import { showApiError } from '@/utils/showApiError'
import { collectPrices, fillMissingPrices, mergePriceRows } from './priceRows'

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
      state.rows = mergePriceRows(holdings)
    } catch (error) {
      showApiError(error, '加载持仓失败')
    }
  }

  /** 空价行用服务端估值价（含历史收盘兜底）补齐，再打开弹窗 */
  function openDialog(serverPrices: Record<string, number>) {
    state.rows = fillMissingPrices(state.rows, serverPrices)
    state.dialogVisible = true
  }

  function getCurrentPrices() {
    return collectPrices(state.rows)
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
      if (result.failed_count > 0)
        ElMessage.warning(
          `已保存 ${result.success_count} 个价格，${result.failed_count} 个失败：${(
            result.failed_list ?? []
          )
            .map((item: { symbol: string }) => item.symbol)
            .join('、')}`
        )
      else ElMessage.success(`成功保存 ${result.success_count} 个价格`)
      await loadHoldingsForPrice({ force: true })
    } catch (error) {
      showApiError(error, { prefix: '保存失败' })
    } finally {
      state.saving = false
    }
  }

  return reactive({ state, loadHoldingsForPrice, openDialog, getCurrentPrices, savePrices })
}

export type PriceInputsFeature = ReturnType<typeof usePriceInputs>
