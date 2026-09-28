import { describe, expect, it } from 'vitest'
import {
  buildTagFilterOptions,
  EMPTY_TAG_SOURCE,
  matchesKeyword,
  matchesTagFilter,
  symbolEquals,
  tagFilterValue,
  type HoldingTagSource
} from './filters'

function source(overrides: Partial<HoldingTagSource> = {}): HoldingTagSource {
  return { ...EMPTY_TAG_SOURCE, ...overrides }
}

describe('matchesKeyword', () => {
  const tencent = { symbol: '00700', name: '腾讯控股' }
  const apple = { symbol: 'AAPL', name: 'Apple Inc.' }

  it('空关键词不过滤', () => {
    expect(matchesKeyword(tencent, '')).toBe(true)
    expect(matchesKeyword(tencent, '   ')).toBe(true)
    expect(matchesKeyword(tencent, null)).toBe(true)
  })

  it('代码不区分大小写的子串', () => {
    expect(matchesKeyword(apple, 'aap')).toBe(true)
    expect(matchesKeyword(apple, ' AAPL ')).toBe(true)
    expect(matchesKeyword(apple, 'msft')).toBe(false)
  })

  it('港股容忍省略或多写前导零', () => {
    expect(matchesKeyword(tencent, '700')).toBe(true)
    expect(matchesKeyword(tencent, '0700')).toBe(true)
    expect(matchesKeyword(tencent, '000700')).toBe(true)
    expect(matchesKeyword(tencent, '701')).toBe(false)
  })

  it('名称子串（英文不区分大小写）', () => {
    expect(matchesKeyword(tencent, '腾讯')).toBe(true)
    expect(matchesKeyword(apple, 'apple')).toBe(true)
    expect(matchesKeyword({ symbol: 'X', name: null }, '腾讯')).toBe(false)
  })
})

describe('symbolEquals', () => {
  it('深链定位：同一只才算，数字代码忽略前导零', () => {
    expect(symbolEquals('00700', '700')).toBe(true)
    expect(symbolEquals('aapl', 'AAPL')).toBe(true)
    expect(symbolEquals('00700', '07000')).toBe(false)
    expect(symbolEquals('600036', '6000360')).toBe(false)
    // 非纯数字不做前导零折叠
    expect(symbolEquals('0A', 'A')).toBe(false)
  })
})

describe('matchesTagFilter', () => {
  const dividendLowRisk = source({
    aiTags: ['高股息', '估值偏低'],
    riskLevel: 'low',
    opinionTags: ['偏多'],
    eventTypes: ['DIVIDEND_PLAN']
  })
  const growthHighRisk = source({ aiTags: ['业绩增长'], riskLevel: 'high' })

  it('未选标签 = 不过滤（含无分析的标的）', () => {
    expect(matchesTagFilter(source(), [])).toBe(true)
  })

  it('组内 OR', () => {
    const selected = [tagFilterValue('ai', '高股息'), tagFilterValue('ai', '业绩增长')]
    expect(matchesTagFilter(dividendLowRisk, selected)).toBe(true)
    expect(matchesTagFilter(growthHighRisk, selected)).toBe(true)
    expect(matchesTagFilter(source(), selected)).toBe(false)
  })

  it('组间 AND', () => {
    const selected = [
      tagFilterValue('ai', '高股息'),
      tagFilterValue('ai', '业绩增长'),
      tagFilterValue('risk', 'high')
    ]
    expect(matchesTagFilter(dividendLowRisk, selected)).toBe(false)
    expect(matchesTagFilter(growthHighRisk, selected)).toBe(true)
  })

  it('AI 标签用全部标签而不是表格里显示的第一个', () => {
    expect(matchesTagFilter(dividendLowRisk, [tagFilterValue('ai', '估值偏低')])).toBe(true)
  })

  it('观点与事件组', () => {
    expect(
      matchesTagFilter(dividendLowRisk, [
        tagFilterValue('opinion', '偏多'),
        tagFilterValue('event', 'DIVIDEND_PLAN')
      ])
    ).toBe(true)
    expect(matchesTagFilter(dividendLowRisk, [tagFilterValue('event', 'SHARE_UNLOCK')])).toBe(false)
  })

  it('同名标签按组区分，不跨组命中', () => {
    const odd = source({ opinionTags: ['高股息'] })
    expect(matchesTagFilter(odd, [tagFilterValue('ai', '高股息')])).toBe(false)
  })

  it('无法识别的选项值忽略', () => {
    expect(matchesTagFilter(source(), ['bogus', 'nope:x'])).toBe(true)
  })
})

describe('buildTagFilterOptions', () => {
  it('只列当前持仓出现过的标签，空组不出现，计数按标的', () => {
    const groups = buildTagFilterOptions([
      source({ aiTags: ['高股息', '业绩下滑'], riskLevel: 'low' }),
      source({ aiTags: ['高股息', '数据不足'], riskLevel: 'high' }),
      source({ eventTypes: ['EARNINGS_DISCLOSURE', 'EARNINGS_DISCLOSURE'] })
    ])
    expect(groups.map((group) => group.label)).toEqual(['AI 标签', '风险等级', '近期事件'])

    const ai = groups[0].options
    // 负面在前、正面其次、中性（数据不足）最后
    expect(ai.map((option) => [option.label, option.count, option.tone])).toEqual([
      ['业绩下滑', 1, 'negative'],
      ['高股息', 2, 'positive'],
      ['数据不足', 1, 'neutral']
    ])
    expect(ai[1].value).toBe('ai:高股息')

    // 风险按 高 → 中 → 低
    expect(groups[1].options.map((option) => [option.label, option.value])).toEqual([
      ['高风险', 'risk:high'],
      ['低风险', 'risk:low']
    ])

    // 同一标的同类事件多条只计一次；事件类型转中文
    expect(groups[2].options).toEqual([
      expect.objectContaining({ label: '财报披露', value: 'event:EARNINGS_DISCLOSURE', count: 1 })
    ])
  })

  it('没有任何标签时返回空列表', () => {
    expect(buildTagFilterOptions([source(), source()])).toEqual([])
  })
})
