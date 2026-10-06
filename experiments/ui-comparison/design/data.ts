// Fictional holdings and quotes for visual review only. No live market data.
export type Market = '港股' | 'A股' | '美股'
export interface Lot {
  account: string
  quantity: number
  average: number
}
export interface Position {
  id: string
  name: string
  symbol: string
  market: Market
  industry: string
  currency: 'CNY' | 'HKD' | 'USD'
  price: number | null
  date: string
  tone: string
  manual?: boolean
  lots: Lot[]
}
export const accounts = [
  { label: '全部账户', value: 'all' },
  { label: '香港投资账户', value: '香港投资账户' },
  { label: '环球投资账户', value: '环球投资账户' },
  { label: '境内证券账户', value: '境内证券账户' }
]
export const rates = { CNY: 1, HKD: 0.91, USD: 7.12 }
export const positions: Position[] = [
  {
    id: '700',
    name: '腾讯控股',
    symbol: '00700',
    market: '港股',
    industry: '互联网',
    currency: 'HKD',
    price: 586,
    date: '2026-09-30',
    tone: 'blue',
    lots: [
      { account: '香港投资账户', quantity: 800, average: 412.5 },
      { account: '环球投资账户', quantity: 200, average: 460 }
    ]
  },
  {
    id: '900',
    name: '长江电力',
    symbol: '600900',
    market: 'A股',
    industry: '公用事业',
    currency: 'CNY',
    price: 29.86,
    date: '2026-09-30',
    tone: 'sage',
    lots: [{ account: '境内证券账户', quantity: 12000, average: 25.48 }]
  },
  {
    id: 'aapl',
    name: '苹果公司',
    symbol: 'AAPL',
    market: '美股',
    industry: '科技',
    currency: 'USD',
    price: 231.4,
    date: '2026-09-30',
    tone: 'ink',
    lots: [{ account: '环球投资账户', quantity: 150, average: 198.6 }]
  },
  {
    id: '941',
    name: '中国移动',
    symbol: '00941',
    market: '港股',
    industry: '电信服务',
    currency: 'HKD',
    price: 79.8,
    date: '2026-09-30',
    tone: 'blue',
    lots: [{ account: '香港投资账户', quantity: 4000, average: 68.25 }]
  },
  {
    id: '9988',
    name: '阿里巴巴－W',
    symbol: '09988',
    market: '港股',
    industry: '互联网',
    currency: 'HKD',
    price: 143.2,
    date: '2026-09-30',
    tone: 'clay',
    lots: [{ account: '香港投资账户', quantity: 1500, average: 152.6 }]
  },
  {
    id: 'voo',
    name: '标普 500 ETF',
    symbol: 'VOO',
    market: '美股',
    industry: '指数基金',
    currency: 'USD',
    price: 558.2,
    date: '2026-09-30',
    tone: 'wine',
    lots: [{ account: '环球投资账户', quantity: 60, average: 558.2 }]
  },
  {
    id: 'etf',
    name: '红利低波 ETF',
    symbol: '512890',
    market: 'A股',
    industry: '指数基金',
    currency: 'CNY',
    price: 1.409,
    date: '2026-09-30',
    tone: 'ochre',
    lots: [{ account: '境内证券账户', quantity: 80000, average: 1.265 }]
  },
  {
    id: 'low',
    name: '示例精密制造',
    symbol: 'DEMO1',
    market: '港股',
    industry: '工业',
    currency: 'HKD',
    price: 0.085,
    date: '2026-09-16',
    tone: 'sage',
    manual: true,
    lots: [{ account: '香港投资账户', quantity: 20000, average: 0.112 }]
  },
  {
    id: 'missing',
    name: '示例海外消费',
    symbol: 'DEMO2',
    market: '美股',
    industry: '消费',
    currency: 'USD',
    price: null,
    date: '',
    tone: 'ink',
    lots: [{ account: '环球投资账户', quantity: 200, average: 32.8 }]
  }
]
export function estimate(position: Position, account: string) {
  const lots = position.lots.filter((lot) => account === 'all' || lot.account === account)
  const quantity = lots.reduce((sum, lot) => sum + lot.quantity, 0)
  const cost = lots.reduce((sum, lot) => sum + lot.quantity * lot.average, 0)
  const value = position.price === null ? null : quantity * position.price
  const profit = value === null ? null : value - cost
  return {
    ...position,
    lots,
    quantity,
    cost,
    average: quantity ? cost / quantity : 0,
    value,
    cny: value === null ? null : value * rates[position.currency],
    costCny: cost * rates[position.currency],
    profit,
    profitCny: profit === null ? null : profit * rates[position.currency],
    rate: profit === null ? null : (profit / cost) * 100
  }
}
export type Row = ReturnType<typeof estimate>
