/**
 * 持仓数据 feature（issue #140：持仓页的核心数据面）。持仓列表、账户/市场
 * 过滤、关键词搜索与标签筛选（#235，口径见 filters.ts）、深链定位、券商账户目录、行内价格编辑与持久化、一键刷新股价、汇总口径
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
import { profitColor, todayLocalISODate, toNumber } from '@/utils/helpers'
import type { BrokerAccount } from '@/types'
import {
  UNASSIGNED_ACCOUNT,
  accountLabel as accountLabelOf,
  type UnassignedAccount
} from '@/utils/labels'
import { showApiError } from '@/utils/showApiError'
import {
  buildMarketSubtotals,
  describePrice,
  sortNullsLast,
  type PriceInfo,
  type SortOrder
} from './display'
import {
  buildTagFilterOptions,
  EMPTY_TAG_SOURCE,
  matchesKeyword,
  matchesTagFilter,
  symbolEquals,
  type HoldingTagSource
} from './filters'

/**
 * 表格的一行：合并视图下是同一标的跨账户的汇总（accounts 里是各账户的原始持仓行），
 * 按账户视图下就是单个账户的持仓（accounts 只有它自己）。数值统一成 number。
 */
export interface HoldingRow {
  key: string
  symbol: string
  name: string | null | undefined
  market: string
  currency: string
  quantity: number
  totalCost: number
  avgCost: number
  unknownCost: number
  priceUpdatedAt: string | null | undefined
  /**
   * 现价这一组（写库时刻/行情日/来源，#217）：合并视图下取写库最晚的账户行整组，
   * 不拆开拼（价格本就是证券级，各账户行通常一致）
   */
  pricedAt: string | null | undefined
  priceAsOf: string | null | undefined
  priceSource: string | null | undefined
  /** 按账户视图：该行所属账户；合并视图：undefined */
  accountId?: number | null
  accounts: Holding[]
}

export type HoldingsViewMode = 'merged' | 'account'
/** 可排序列：市值与浮动盈亏都按折人民币金额排，缺价/缺汇率的行沉底 */
export type HoldingsSortProp = 'marketValue' | 'profit'
export const DEFAULT_SORT = {
  prop: 'marketValue' as HoldingsSortProp,
  order: 'descending' as const
}
const VIEW_MODE_KEY = 'holdings.viewMode'

function readViewMode(): HoldingsViewMode {
  try {
    return window.localStorage.getItem(VIEW_MODE_KEY) === 'account' ? 'account' : 'merged'
  } catch {
    return 'merged'
  }
}

function latest(a: string | null | undefined, b: string | null | undefined) {
  if (!a) return b
  if (!b) return a
  return a > b ? a : b
}

/** 深链定位的目标（?symbol=&market=）；market 缺省 = 任意市场 */
export interface HoldingFocus {
  symbol: string
  market: string | null
}

export function useHoldingsTable({
  isUnmounted,
  tagSourceOf = () => EMPTY_TAG_SOURCE
}: {
  isUnmounted: () => boolean
  /** 标签筛选的数据来源（角标 feature 提供）；缺省视为无标签 */
  tagSourceOf?: (row: { symbol: string; market: string }) => HoldingTagSource
}) {
  const holdingsStore = useHoldingsStore()
  const { convertToCNY, convertToUSD, hasRate } = useExchangeRates()
  const { refreshPrices: runPriceRefresh, notifyRefreshResult } = useRefreshPrices(isUnmounted)

  const state = reactive({
    holdings: [] as Holding[],
    loading: false,
    refreshing: false,
    selectedMarket: '',
    selectedAccount: '' as '' | UnassignedAccount | number,
    brokerAccounts: [] as BrokerAccount[],
    currentPrices: {} as Record<string, number | null>,
    // 现价默认只读；同一时刻最多一只标的处于编辑态（键 = symbol:market）
    // 正在编辑现价的**行**（row.key）。按账户视图里同一标的有多行，共用 symbol:market
    // 价格键；编辑态若也按价格键，各行一起挂输入框抢焦点（PR #226 评审 P2）
    editingRowKey: null as string | null,
    priceDraft: null as number | null,
    sort: { ...DEFAULT_SORT } as { prop: HoldingsSortProp | null; order: SortOrder },
    // 合并（按标的汇总，默认）/ 按账户：账户细节多数时候用不上，放到展开行与切换视图里
    viewMode: readViewMode() as HoldingsViewMode,
    // 搜索与标签筛选只作用于表格行，不影响汇总卡（汇总是账户/市场口径的全貌）
    keyword: '',
    selectedTags: [] as string[],
    // 深链定位（仪表盘/交易记录点代码跳来）：高亮并滚动到这一只
    focus: null as HoldingFocus | null
  })

  function setViewMode(mode: HoldingsViewMode) {
    state.viewMode = mode
    try {
      window.localStorage.setItem(VIEW_MODE_KEY, mode)
    } catch {
      /* 隐私模式等：只影响记忆 */
    }
  }

  const visibleHoldings = computed(() => {
    if (state.selectedAccount === '' || state.selectedAccount === undefined) return state.holdings
    if (state.selectedAccount === UNASSIGNED_ACCOUNT) {
      return state.holdings.filter((h) => h.broker_account_id === null)
    }
    return state.holdings.filter((h) => h.broker_account_id === state.selectedAccount)
  })

  // 已删除账户显示统一文案（此前显示成「账户#12」）
  const accountLabel = (accountId: number | null | undefined) =>
    accountLabelOf(state.brokerAccounts, accountId)

  async function loadBrokerAccounts() {
    try {
      const response = await api.getBrokerAccounts({ limit: 1000 })
      state.brokerAccounts = response.data
    } catch (error) {
      showApiError(error, '加载券商账户失败')
    }
  }

  // 价格键与统计页一致：symbol:market（同代码跨市场不串价）
  function priceKey(row: { symbol: string; market: string }) {
    return `${row.symbol}:${row.market}`
  }

  function toRow(h: Holding): HoldingRow {
    const quantity = toNumber(h.quantity)
    const totalCost = toNumber(h.total_cost)
    return {
      key: `${h.symbol}:${h.market}:${h.broker_account_id ?? 'null'}`,
      symbol: h.symbol,
      name: h.name,
      market: h.market,
      currency: h.currency,
      quantity,
      totalCost,
      avgCost: quantity ? totalCost / quantity : toNumber(h.avg_cost),
      unknownCost: toNumber(h.unknown_cost_quantity ?? 0),
      priceUpdatedAt: h.price_updated_at ?? h.updated_at,
      pricedAt: h.price_updated_at,
      priceAsOf: h.price_as_of,
      priceSource: h.price_source,
      accountId: h.broker_account_id ?? null,
      accounts: [h]
    }
  }

  // 同一标的（symbol+market）跨账户合并：数量/成本相加，均价 = 总成本 / 总数量
  const mergedRows = computed<HoldingRow[]>(() => {
    const groups = new Map<string, HoldingRow>()
    for (const h of visibleHoldings.value) {
      const key = `${h.symbol}:${h.market}`
      const row = groups.get(key)
      if (!row) {
        groups.set(key, { ...toRow(h), key, accountId: undefined })
        continue
      }
      row.quantity += toNumber(h.quantity)
      row.totalCost += toNumber(h.total_cost)
      row.unknownCost += toNumber(h.unknown_cost_quantity ?? 0)
      row.avgCost = row.quantity ? row.totalCost / row.quantity : row.avgCost
      row.priceUpdatedAt = latest(row.priceUpdatedAt, h.price_updated_at ?? h.updated_at)
      if (h.price_updated_at && latest(row.pricedAt, h.price_updated_at) === h.price_updated_at) {
        row.pricedAt = h.price_updated_at
        row.priceAsOf = h.price_as_of
        row.priceSource = h.price_source
      }
      row.name = row.name || h.name
      row.accounts.push(h)
    }
    return [...groups.values()]
  })

  function priceOf(row: { symbol: string; market: string }) {
    return state.currentPrices[`${row.symbol}:${row.market}`] ?? null
  }

  function marketValueOf(row: HoldingRow): number | null {
    const price = priceOf(row)
    return price ? price * row.quantity : null
  }

  function marketValueCNYOf(row: HoldingRow): number | null {
    const value = marketValueOf(row)
    return value === null ? null : convertToCNY(value, row.currency)
  }

  function profitOf(row: HoldingRow): number | null {
    const value = marketValueOf(row)
    return value === null ? null : value - row.totalCost
  }

  /** 浮动盈亏折人民币（成本与市值同按今日汇率，不含汇兑损益）；缺汇率为 null */
  function profitCNYOf(row: HoldingRow): number | null {
    const profit = profitOf(row)
    return profit === null ? null : convertToCNY(profit, row.currency)
  }

  function priceInfoOf(row: HoldingRow): PriceInfo | null {
    return describePrice(
      {
        price: priceOf(row),
        priceAsOf: row.priceAsOf,
        priceUpdatedAt: row.pricedAt ?? row.priceUpdatedAt,
        priceSource: row.priceSource
      },
      todayLocalISODate()
    )
  }

  function profitRateOf(row: HoldingRow): number | null {
    const profit = profitOf(row)
    if (profit === null || !row.totalCost) return null
    return (profit / row.totalCost) * 100
  }

  // 排序在这里做（el-table 用 sortable="custom"）：el-table 的 sort-method 降序时整体
  // 反转，缺值会跑到最上面；这里缺价/缺汇率的行无论升降序都沉底。默认按市值降序。
  const baseRows = computed<HoldingRow[]>(() =>
    state.viewMode === 'account' ? visibleHoldings.value.map(toRow) : mergedRows.value
  )

  // 两种视图同一口径：关键词与标签都按标的（symbol:market）判断，与行是否按账户拆开无关
  const rows = computed<HoldingRow[]>(() => {
    const filtered = baseRows.value.filter(
      (row) =>
        matchesKeyword(row, state.keyword) &&
        (!state.selectedTags.length || matchesTagFilter(tagSourceOf(row), state.selectedTags))
    )
    const { prop, order } = state.sort.prop && state.sort.order ? state.sort : DEFAULT_SORT
    const valueOf = prop === 'profit' ? profitCNYOf : marketValueCNYOf
    return sortNullsLast(filtered, valueOf, order ?? 'descending')
  })

  /** 筛选前（账户/市场口径下）的行数：「筛选出 x / y」 */
  const totalRowCount = computed(() => baseRows.value.length)
  const isFiltered = computed(() => !!state.keyword.trim() || state.selectedTags.length > 0)

  // 选项按标的去重后统计（按账户视图同一标的多行只计一只），只列当前持仓里出现过的标签
  const tagOptions = computed(() => {
    const seen = new Map<string, { symbol: string; market: string }>()
    for (const h of visibleHoldings.value) seen.set(priceKey(h), h)
    return buildTagFilterOptions([...seen.values()].map((row) => tagSourceOf(row)))
  })

  function clearFilters() {
    state.keyword = ''
    state.selectedTags = []
  }

  function isFocused(row: { symbol: string; market: string }): boolean {
    const focus = state.focus
    if (!focus) return false
    return symbolEquals(row.symbol, focus.symbol) && (!focus.market || row.market === focus.market)
  }

  /** 深链目标是否在已加载的持仓里（不看账户过滤）；false = 已清仓或从未持有 */
  const focusHeld = computed(() => !!state.focus && state.holdings.some(isFocused))
  /** 深链目标是否在当前账户过滤下可见 */
  const focusVisible = computed(() => !!state.focus && visibleHoldings.value.some(isFocused))

  function setSort(sort: { prop: string | null; order: SortOrder }) {
    const prop = sort.prop === 'profit' || sort.prop === 'marketValue' ? sort.prop : null
    state.sort = { prop, order: prop ? sort.order : null }
  }

  // 汇总口径：无可用价格（或缺汇率无法折人民币）的持仓行在成本与市值两侧同时剔除
  // （口径自洽），单独计数提示，而不是"计成本不计市值"把总盈亏虚减。
  const pricedHoldings = computed(() =>
    visibleHoldings.value.filter(
      (h) => (state.currentPrices[priceKey(h)] ?? 0) > 0 && hasRate(h.currency)
    )
  )

  // 只数按标的（symbol:market）去重：同一标的多账户行只算一只
  const securityCount = computed(() => new Set(visibleHoldings.value.map(priceKey)).size)
  const pricedSecurityCount = computed(() => new Set(pricedHoldings.value.map(priceKey)).size)
  const unpricedCount = computed(() => securityCount.value - pricedSecurityCount.value)

  const marketSubtotals = computed(() => {
    const priced = new Set(pricedHoldings.value)
    return buildMarketSubtotals(
      visibleHoldings.value.map((h) => {
        const isPriced = priced.has(h)
        const value = (state.currentPrices[priceKey(h)] ?? 0) * toNumber(h.quantity)
        return {
          market: h.market,
          key: priceKey(h),
          priced: isPriced,
          valueCNY: isPriced ? (convertToCNY(value, h.currency) ?? 0) : 0,
          costCNY: isPriced ? (convertToCNY(toNumber(h.total_cost), h.currency) ?? 0) : 0
        }
      })
    )
  })

  const totalCostCNY = computed(() => {
    return pricedHoldings.value.reduce((sum, h) => {
      return sum + (convertToCNY(toNumber(h.total_cost), h.currency) ?? 0)
    }, 0)
  })

  const totalCostUSD = computed(() => convertToUSD(totalCostCNY.value))

  const totalMarketValueCNY = computed(() => {
    return pricedHoldings.value.reduce((sum, h) => {
      const marketValue = (state.currentPrices[priceKey(h)] ?? 0) * toNumber(h.quantity)
      return sum + (convertToCNY(marketValue, h.currency) ?? 0)
    }, 0)
  })

  const totalMarketValueUSD = computed(() => convertToUSD(totalMarketValueCNY.value))

  const totalProfit = computed(() => totalMarketValueCNY.value - totalCostCNY.value)

  const totalProfitUSD = computed(() => convertToUSD(totalProfit.value))

  const totalProfitRate = computed<number | null>(() => {
    if (totalCostCNY.value === 0) return null
    return (totalProfit.value / totalCostCNY.value) * 100
  })

  function currentParams(): Record<string, unknown> {
    const params: Record<string, unknown> = {}
    if (state.selectedMarket) params.market = state.selectedMarket
    return params
  }

  // silent：自动重读（useAutoReload）用——不转圈、不弹错，失败向上抛由调用方静默
  async function loadHoldings(options: { force?: boolean; silent?: boolean } = {}) {
    if (!options.silent) state.loading = true
    try {
      state.holdings = await holdingsStore.fetchHoldings(currentParams(), {
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
      if (options.silent) throw error
      showApiError(error, '加载持仓数据失败')
    } finally {
      if (!options.silent) state.loading = false
    }
  }

  // 价格按标的（symbol:market）共享。后端 PUT /holdings/{id}/price 本就按 user+symbol+market
  // 更新该标的在所有账户的持仓，所以只发**一次**请求（取任一账户行即可）。逐账户循环提交会在
  // 连续改价时让旧批次的后续请求把新价格覆盖回去（PR #216 评审 P2）
  async function savePriceToDatabase(row: HoldingRow) {
    const price = state.currentPrices[`${row.symbol}:${row.market}`]
    const representative = row.accounts[0]
    if (!price || price <= 0 || !representative) return
    try {
      await holdingsStore.updateHoldingPrice(
        representative.id,
        price,
        `${row.symbol}:${row.market}`
      )
      // store 已按序号守卫用服务端整行回填缓存（含 price_source=manual、行情日清空）；
      // 从缓存取回让「手工」标立刻出现。只换行数据，不重置 currentPrices——
      // 其他标的可能还有在飞的改价。
      if (!isUnmounted()) {
        state.holdings = await holdingsStore.fetchHoldings(currentParams())
      }
    } catch (error) {
      showApiError(error, { prefix: `保存 ${row.symbol} 价格失败` })
    }
  }

  // ---- 现价行内编辑：点击数值/铅笔进入，回车或失焦保存，Esc 取消 ----
  function startPriceEdit(row: HoldingRow) {
    state.editingRowKey = row.key
    state.priceDraft = state.currentPrices[priceKey(row)] ?? null
  }

  function isEditingPrice(row: HoldingRow) {
    return state.editingRowKey === row.key
  }

  function cancelPriceEdit() {
    state.editingRowKey = null
    state.priceDraft = null
  }

  async function commitPriceEdit(row: HoldingRow) {
    const key = priceKey(row)
    // 回车后紧跟的失焦、Esc 后的失焦、别的行的失焦都会进来：不是正在编辑的这一行就什么都不做
    if (state.editingRowKey !== row.key) return
    const draft = state.priceDraft
    cancelPriceEdit()
    if (draft === null || !(draft > 0) || draft === state.currentPrices[key]) return
    state.currentPrices[key] = draft
    await savePriceToDatabase(row)
  }

  function weightOf(row: HoldingRow): number | null {
    const value = marketValueCNYOf(row)
    if (value === null || !totalMarketValueCNY.value) return null
    return (value / totalMarketValueCNY.value) * 100
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
      showApiError(error, '刷新股价失败')
      console.error('Refresh error:', error)
    } finally {
      if (!isUnmounted()) state.refreshing = false
    }
  }

  function getProfitColor(row: HoldingRow) {
    return profitColor(profitOf(row) ?? 0)
  }

  return reactive({
    state,
    visibleHoldings,
    unpricedCount,
    securityCount,
    pricedSecurityCount,
    marketSubtotals,
    totalCostCNY,
    totalCostUSD,
    totalMarketValueCNY,
    totalMarketValueUSD,
    totalProfit,
    totalProfitUSD,
    totalProfitRate,
    rows,
    totalRowCount,
    isFiltered,
    tagOptions,
    clearFilters,
    isFocused,
    focusHeld,
    focusVisible,
    accountLabel,
    loadBrokerAccounts,
    loadHoldings,
    priceKey,
    priceOf,
    marketValueOf,
    marketValueCNYOf,
    profitOf,
    profitCNYOf,
    profitRateOf,
    priceInfoOf,
    weightOf,
    setViewMode,
    setSort,
    savePriceToDatabase,
    startPriceEdit,
    isEditingPrice,
    cancelPriceEdit,
    commitPriceEdit,
    refreshPrices,
    getProfitColor
  })
}

export type HoldingsTableFeature = ReturnType<typeof useHoldingsTable>
