/**
 * 持仓数据 feature（issue #140：持仓页的核心数据面）。持仓列表、账户/市场
 * 过滤、券商账户目录、行内价格编辑与持久化、一键刷新股价、汇总口径
 * （成本/市值/盈亏，CNY+USD 双币）全在这里；HoldingsTable/HoldingsSummary
 * 只做展示与交互绑定。
 *
 * 返回 reactive 包：作为单个 prop 传给子组件后模板可直接读写
 * （selectedAccount/selectedMarket/currentPrices 由壳层与表格双向绑定）。
 */

import { computed, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import { useHoldingsStore, type Holding } from '@/stores/holdings'
import { useExchangeRates } from '@/composables/useExchangeRates'
import { useRefreshPrices } from '@/composables/useRefreshPrices'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { profitColor, toNumber } from '@/utils/helpers'
import type { BrokerAccount } from '@/types'

export function useHoldingsTable({ isUnmounted }: { isUnmounted: () => boolean }) {
  const holdingsStore = useHoldingsStore()
  const { convertToCNY, convertToUSD } = useExchangeRates()
  const { refreshPrices: runPriceRefresh, notifyRefreshResult } = useRefreshPrices(isUnmounted)

  const state = reactive({
    holdings: [] as Holding[],
    loading: false,
    refreshing: false,
    selectedMarket: '',
    selectedAccount: '' as '' | 'unassigned' | number,
    brokerAccounts: [] as BrokerAccount[],
    currentPrices: {} as Record<string, number | null>
  })

  const visibleHoldings = computed(() => {
    if (state.selectedAccount === '' || state.selectedAccount === undefined) return state.holdings
    if (state.selectedAccount === 'unassigned') {
      return state.holdings.filter((h) => h.broker_account_id === null)
    }
    return state.holdings.filter((h) => h.broker_account_id === state.selectedAccount)
  })

  function accountLabel(accountId: number | null | undefined) {
    if (accountId === null || accountId === undefined) return '未指定'
    const account = state.brokerAccounts.find((item) => item.id === accountId)
    return account ? account.account_name : `账户#${accountId}`
  }

  async function loadBrokerAccounts() {
    try {
      const response = await api.getBrokerAccounts({ limit: 1000 })
      state.brokerAccounts = response.data
    } catch (error) {
      ElMessage.error(getApiErrorMessage(error, '加载券商账户失败'))
    }
  }

  // 价格键与统计页一致：symbol:market（同代码跨市场不串价）
  function priceKey(row: Holding) {
    return `${row.symbol}:${row.market}`
  }

  // 汇总口径：无可用价格的持仓行在成本与市值两侧同时剔除（口径自洽），
  // 单独计数提示，而不是"计成本不计市值"把总盈亏虚减。
  const pricedHoldings = computed(() =>
    visibleHoldings.value.filter((h) => (state.currentPrices[priceKey(h)] ?? 0) > 0)
  )

  const unpricedCount = computed(() => visibleHoldings.value.length - pricedHoldings.value.length)

  const totalCostCNY = computed(() => {
    return pricedHoldings.value.reduce((sum, h) => {
      const costCNY = convertToCNY(toNumber(h.total_cost), h.currency)
      return sum + costCNY
    }, 0)
  })

  const totalCostUSD = computed(() => convertToUSD(totalCostCNY.value))

  const totalMarketValueCNY = computed(() => {
    return pricedHoldings.value.reduce((sum, h) => {
      const marketValue = (state.currentPrices[priceKey(h)] ?? 0) * toNumber(h.quantity)
      const marketValueCNY = convertToCNY(marketValue, h.currency)
      return sum + marketValueCNY
    }, 0)
  })

  const totalMarketValueUSD = computed(() => convertToUSD(totalMarketValueCNY.value))

  const totalProfit = computed(() => totalMarketValueCNY.value - totalCostCNY.value)

  const totalProfitUSD = computed(() => convertToUSD(totalProfit.value))

  const totalProfitRate = computed(() => {
    if (totalCostCNY.value === 0) return 0
    return (totalProfit.value / totalCostCNY.value) * 100
  })

  async function loadHoldings(options: { force?: boolean } = {}) {
    state.loading = true
    try {
      const params: Record<string, unknown> = {}
      if (state.selectedMarket) params.market = state.selectedMarket

      state.holdings = await holdingsStore.fetchHoldings(params, {
        force: options?.force === true
      })

      // Initialize current prices from persisted market prices only.
      state.holdings.forEach((h) => {
        if (h.current_price && toNumber(h.current_price) > 0) {
          state.currentPrices[priceKey(h)] = toNumber(h.current_price)
        } else {
          state.currentPrices[priceKey(h)] = null
        }
      })
    } catch (error) {
      ElMessage.error(getApiErrorMessage(error, '加载持仓数据失败'))
    } finally {
      state.loading = false
    }
  }

  async function savePriceToDatabase(row: Holding) {
    try {
      const price = state.currentPrices[priceKey(row)]
      if (!price || price <= 0) return

      await holdingsStore.updateHoldingPrice(row.id, price)
      // Silent success - don't show message for every input
    } catch (error) {
      ElMessage.error(`保存${row.symbol}价格失败: ${getApiErrorMessage(error)}`)
    }
  }

  async function refreshPrices() {
    state.refreshing = true

    const loadingMsg = ElMessage({
      message: '股价刷新已提交，正在后台处理...',
      type: 'info',
      duration: 0,
      showClose: true
    })

    try {
      // 轮询与消息拼装在 useRefreshPrices 收敛一处
      const result = await runPriceRefresh()
      loadingMsg.close()
      if (!result || isUnmounted()) return

      await loadHoldings({ force: true })
      notifyRefreshResult(result)
    } catch (error) {
      loadingMsg.close()
      if (isUnmounted()) return
      ElMessage.error(getApiErrorMessage(error, '刷新股价失败'))
      console.error('Refresh error:', error)
    } finally {
      if (!isUnmounted()) state.refreshing = false
    }
  }

  function calculateProfitAmount(row: Holding) {
    const currentPrice = state.currentPrices[priceKey(row)]
    if (!currentPrice) return 0
    const marketValue = currentPrice * toNumber(row.quantity)
    return marketValue - toNumber(row.total_cost)
  }

  function calculateProfitRate(row: Holding) {
    const profitAmount = calculateProfitAmount(row)
    const totalCost = toNumber(row.total_cost)
    if (totalCost === 0) return 0
    return (profitAmount / totalCost) * 100
  }

  function getProfitColor(row: Holding) {
    return profitColor(calculateProfitAmount(row))
  }

  return reactive({
    state,
    visibleHoldings,
    unpricedCount,
    totalCostCNY,
    totalCostUSD,
    totalMarketValueCNY,
    totalMarketValueUSD,
    totalProfit,
    totalProfitUSD,
    totalProfitRate,
    accountLabel,
    loadBrokerAccounts,
    loadHoldings,
    priceKey,
    savePriceToDatabase,
    refreshPrices,
    calculateProfitAmount,
    calculateProfitRate,
    getProfitColor
  })
}

export type HoldingsTableFeature = ReturnType<typeof useHoldingsTable>
