import { describe, expect, it } from 'vitest'
import {
  UNPRICED_POSITIONS_WARNING,
  buildSummaryWarnings,
  parseRateWarningCurrencies
} from './warnings'

const BACKEND_RATE_WARNING = (currencies: string) =>
  `缺少 ${currencies} 对 CNY 的汇率，这些币种的金额未计入 CNY 汇总（不会按原值混入）。`

describe('parseRateWarningCurrencies', () => {
  it('解析后端缺汇率提示里的币种', () => {
    expect(parseRateWarningCurrencies(BACKEND_RATE_WARNING('HKD/USD'))).toEqual(['HKD', 'USD'])
    expect(parseRateWarningCurrencies('检测到历史超卖')).toBeNull()
  })
})

describe('buildSummaryWarnings', () => {
  it('持仓表现的数据质量警告也要进顶部（此前只取已实现）', () => {
    const warnings = buildSummaryWarnings({
      realizedWarnings: [],
      currentWarnings: ['检测到历史超卖：600000'],
      missingRateCurrencies: []
    })
    expect(warnings).toEqual(['检测到历史超卖：600000'])
  })

  it('已实现与持仓表现逐字相同的 FIFO 警告只出一次', () => {
    const warnings = buildSummaryWarnings({
      realizedWarnings: ['FIFO 超卖'],
      currentWarnings: ['FIFO 超卖']
    })
    expect(warnings).toEqual(['FIFO 超卖'])
  })

  it('缺汇率：各块币种与后端提示里的币种并成一条', () => {
    const warnings = buildSummaryWarnings({
      realizedWarnings: [BACKEND_RATE_WARNING('THB')],
      currentWarnings: [BACKEND_RATE_WARNING('SGD')],
      missingRateCurrencies: ['THB', 'HKD']
    })
    const rateWarnings = warnings.filter((text) => text.includes('对 CNY 的汇率'))
    expect(rateWarnings).toHaveLength(1)
    expect(rateWarnings[0]).toContain('HKD/SGD/THB')
    expect(rateWarnings[0]).toContain('未计入 CNY 汇总')
  })

  it('仅列表路径命中缺汇率时也提示；无缺汇率不误报', () => {
    expect(buildSummaryWarnings({ missingRateCurrencies: ['THB'] })[0]).toContain('THB')
    expect(buildSummaryWarnings({ missingRateCurrencies: [] })).toEqual([])
  })

  it('有具体缺价清单时去掉持仓表现那句泛泛的缺价提示', () => {
    const warnings = buildSummaryWarnings({
      currentWarnings: [UNPRICED_POSITIONS_WARNING],
      priceFreshness: { 'NOPX:A股': { source: 'missing', stale: true } }
    })
    // 缺价清单由 PriceIssuesAlert 展示（#286），这里只去掉重复的泛泛提示
    expect(warnings).toEqual([])

    // 没有清单（试算路径）时保留泛泛那条，不能把缺价信号吞掉
    expect(buildSummaryWarnings({ currentWarnings: [UNPRICED_POSITIONS_WARNING] })).toEqual([
      UNPRICED_POSITIONS_WARNING
    ])
  })
})
