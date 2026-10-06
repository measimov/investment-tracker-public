import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import {
  formatCurrency,
  formatPrice,
  formatQuantity,
  formatPercent,
  formatDate
} from '@/utils/helpers'
export { formatCurrency, formatPrice, formatQuantity, formatPercent, formatDate }

export interface SampleRow {
  id: number
  symbol: string
  name: string
  account: 'demo-a' | 'demo-b'
  currency: 'CNY' | 'HKD' | 'USD'
  quantity: number
  cost: number
  price: number | null
  value: number | null
  valueCNY: number | null
  profit: number | null
  rate: number | null
  date: string
  note: string
}
export const accounts = [
  { label: '全部账户', value: 'all' },
  { label: '演示账户 A', value: 'demo-a' },
  { label: '演示账户 B', value: 'demo-b' }
]
export const sampleCounts = [50, 200].map((value) => ({ label: `${value} 条样本`, value }))
export const accountName = (value: string) =>
  accounts.find((a) => a.value === value)?.label || value

export function makeRows(count: number): SampleRow[] {
  return Array.from({ length: count }, (_, index) => {
    const id = index + 1
    const currency = (['HKD', 'CNY', 'USD'] as const)[index % 3]
    const price =
      index === 0
        ? 0.085
        : index === 1
          ? 1.409
          : index === 2
            ? null
            : index === 3
              ? 0
              : Number((18 + ((index * 13) % 1600) / 10).toFixed(3))
    const quantity = index === 0 ? 12500 : index === 4 ? 250000 : 100 + index * 50
    const value = price === null ? null : Number((price * quantity).toFixed(2))
    const cost = Number(((price || 25) * (index % 2 ? 1.08 : 0.92)).toFixed(4))
    const profit = value === null ? null : Number((value - cost * quantity).toFixed(2))
    return {
      id,
      symbol: String(id).padStart(5, '0'),
      name:
        index % 7 === 0
          ? '示例国际生物科技与医疗创新控股有限公司'
          : `示例${['科技', '银行', '能源', '消费'][index % 4]} ${id}`,
      account: index % 2 ? 'demo-b' : 'demo-a',
      currency,
      quantity,
      cost,
      price,
      value,
      valueCNY:
        value === null || index === 8
          ? null
          : Number(
              (value * (currency === 'USD' ? 7.12 : currency === 'HKD' ? 0.91 : 1)).toFixed(2)
            ),
      profit,
      rate: profit === null ? null : Number(((profit / (cost * quantity)) * 100).toFixed(2)),
      date: `2026-09-${String((index % 28) + 1).padStart(2, '0')}`,
      note:
        index % 7 === 0
          ? '长名称与多行提示样本；已公告股息仍待核对，不代表已到账。'
          : index === 2
            ? '行情缺失，市值与收益保持未知。'
            : index === 8
              ? '折算汇率未知，人民币金额保持未知。'
              : ''
    }
  })
}

export interface TradeDraft {
  symbol: string
  account: string
  date: string
  quantity: number | null
  price: number | null
}
export function emptyTrade(): TradeDraft {
  return { symbol: '00001', account: 'demo-a', date: '2026-09-30', quantity: null, price: null }
}
export function validateTrade(draft: TradeDraft): Record<string, string> {
  const errors: Record<string, string> = {}
  if (!draft.symbol.trim()) errors.symbol = '请输入标的代码'
  if (!['demo-a', 'demo-b'].includes(draft.account)) errors.account = '请选择账户'
  if (!/^\d{4}-\d{2}-\d{2}$/.test(draft.date)) errors.date = '请选择交易日期'
  if (draft.quantity === null || !Number.isFinite(draft.quantity) || draft.quantity <= 0)
    errors.quantity = '数量必须大于 0'
  if (draft.price === null || !Number.isFinite(draft.price) || draft.price <= 0)
    errors.price = '价格必须大于 0'
  return errors
}

export function useComparison(variant: string) {
  const initialCount = Number(new URLSearchParams(location.search).get('rows')) === 200 ? 200 : 50
  const count = ref(initialCount)
  const query = reactive({ keyword: '', account: 'all', start: '', end: '' })
  const sort = ref<'original' | 'asc' | 'desc'>('original')
  const allRows = computed(() => makeRows(count.value))
  const rows = computed(() => {
    const filtered = allRows.value.filter(
      (row) =>
        (query.account === 'all' || row.account === query.account) &&
        (!query.keyword || `${row.name} ${row.symbol}`.includes(query.keyword)) &&
        (!query.start || row.date >= query.start) &&
        (!query.end || row.date <= query.end)
    )
    if (sort.value === 'original') return filtered
    return filtered.sort((a, b) => {
      if (a.profit === null) return b.profit === null ? a.id - b.id : 1
      if (b.profit === null) return -1
      return (a.profit - b.profit) * (sort.value === 'asc' ? 1 : -1) || a.id - b.id
    })
  })
  const open = ref(false)
  const draft = reactive(emptyTrade())
  const errors = ref<Record<string, string>>({})
  const saved = ref<TradeDraft | null>(null)
  function reset() {
    Object.assign(query, { keyword: '', account: 'all', start: '', end: '' })
    sort.value = 'original'
  }
  function openTrade(row?: SampleRow) {
    Object.assign(
      draft,
      emptyTrade(),
      row ? { symbol: row.symbol, account: row.account, price: row.price } : {}
    )
    errors.value = {}
    open.value = true
  }
  function submit() {
    errors.value = validateTrade(draft)
    if (Object.keys(errors.value).length) return false
    saved.value = { ...draft }
    open.value = false
    return true
  }
  function toggleSort() {
    sort.value = sort.value === 'desc' ? 'asc' : 'desc'
  }
  const settle = async () => {
    await nextTick()
    await new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r())))
  }
  onMounted(() => {
    const api = {
      variant,
      snapshot: () => ({
        count: rows.value.length,
        ids: rows.value.map((r) => r.id),
        query: { ...query },
        sort: sort.value,
        open: open.value,
        errors: errors.value,
        saved: saved.value
      }),
      async setCount(value: number) {
        count.value = value === 200 ? 200 : 50
        reset()
        await settle()
      },
      async filter(value: Partial<typeof query>) {
        Object.assign(query, value)
        await settle()
      },
      async reset() {
        reset()
        await settle()
      },
      async sort() {
        toggleSort()
        await settle()
      },
      async openTrade() {
        openTrade()
        await settle()
      },
      async closeTrade() {
        open.value = false
        await settle()
      }
    }
    ;(window as any).__uiBench = api
    settle().then(() => {
      performance.mark('comparison-ready')
      document.documentElement.dataset.ready = 'true'
    })
  })
  return {
    variant,
    count,
    query,
    sort,
    rows,
    open,
    draft,
    errors,
    saved,
    reset,
    openTrade,
    submit,
    toggleSort
  }
}
export type ComparisonModel = ReturnType<typeof useComparison>
