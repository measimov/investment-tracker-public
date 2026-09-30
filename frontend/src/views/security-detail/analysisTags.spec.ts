import { describe, expect, it } from 'vitest'
import {
  NEGATIVE_ANALYSIS_TAGS,
  POSITIVE_ANALYSIS_TAGS,
  analysisTagTone,
  analysisTagType,
  outputAdjustmentText,
  riskAdjustmentText,
  riskLabel,
  riskTagType
} from './analysisTags'

// 与后端 security_analysis_prompts.ALLOWED_TAGS 一致（改白名单时同步）
const ALLOWED_TAGS = [
  '高股息',
  '分红连续',
  '分红中断',
  '业绩增长',
  '业绩下滑',
  '业绩预警',
  '高质押',
  '大股东减持',
  '大股东增持',
  '解禁临近',
  '审计非标',
  '估值偏高',
  '估值偏低',
  '数据不足',
  '利润质量存疑',
  '现金流背离',
  '依赖非经常损益',
  '安全边际充足',
  '安全边际不足',
  '财务强度高',
  '高杠杆脆弱',
  '净现金充裕',
  '尾部风险暴露'
]

describe('analysisTagTone', () => {
  it('正负集合不越出白名单、互不重叠；白名单里只有「数据不足」是中性', () => {
    for (const tag of [...POSITIVE_ANALYSIS_TAGS, ...NEGATIVE_ANALYSIS_TAGS]) {
      expect(ALLOWED_TAGS).toContain(tag)
      expect(POSITIVE_ANALYSIS_TAGS.has(tag) && NEGATIVE_ANALYSIS_TAGS.has(tag)).toBe(false)
    }
    const neutral = ALLOWED_TAGS.filter((tag) => analysisTagTone(tag) === 'neutral')
    expect(neutral).toEqual(['数据不足'])
  })

  it('褒贬映射到 el-tag type', () => {
    expect(analysisTagType('净现金充裕')).toBe('success')
    expect(analysisTagType('审计非标')).toBe('danger')
    expect(analysisTagType('数据不足')).toBe('info')
    expect(analysisTagType('白名单外')).toBe('info')
  })
})

describe('riskTagType', () => {
  it('未知等级兜底 info 而不是绿色', () => {
    expect(riskTagType('high')).toBe('danger')
    expect(riskTagType('medium')).toBe('warning')
    expect(riskTagType('low')).toBe('success')
    expect(riskTagType('')).toBe('info')
    expect(riskTagType(null)).toBe('info')
    expect(riskLabel('medium')).toBe('中')
    expect(riskLabel(undefined)).toBe('未知')
  })
})

describe('riskAdjustmentText', () => {
  it('有上调记录时给出一句话提示（含原因）', () => {
    expect(riskAdjustmentText({ from: 'low', to: 'medium', reason: '港股数据边界' })).toBe(
      '模型给出「低」，按市场下限上调为「中」：港股数据边界'
    )
  })

  it('无原因时省略冒号部分', () => {
    expect(riskAdjustmentText({ from: 'low', to: 'medium' })).toBe(
      '模型给出「低」，按市场下限上调为「中」'
    )
  })

  it('无记录或字段不全返回 null（不显示提示）', () => {
    expect(riskAdjustmentText(null)).toBeNull()
    expect(riskAdjustmentText(undefined)).toBeNull()
    expect(riskAdjustmentText({ from: '', to: 'medium' })).toBeNull()
  })
})

describe('outputAdjustmentText', () => {
  it('无记录返回 null', () => {
    expect(outputAdjustmentText(null)).toBeNull()
    expect(outputAdjustmentText([])).toBeNull()
    expect(outputAdjustmentText([{ type: 'extra_sections', sections: ['附录'] }])).toBeNull()
  })
  it('合成丢弃/截断/归一/补免责声明', () => {
    expect(
      outputAdjustmentText([
        { type: 'tag_normalized', from: '依赖非经常性损益', to: '依赖非经常损益' },
        { type: 'tag_dropped', tag: '高质押', reason: 'market_banned' },
        { type: 'tags_truncated', dropped: ['估值偏高'] },
        { type: 'disclaimer_appended' }
      ])
    ).toBe(
      '解析时已按规则调整：丢弃不合规标签：高质押；标签超过 4 个，去掉：估值偏高；' +
        '标签归一：「依赖非经常性损益」→「依赖非经常损益」；补上免责声明'
    )
  })
  it('仅空白差异的归一不提示', () => {
    expect(
      outputAdjustmentText([{ type: 'tag_normalized', from: ' 高股息 ', to: '高股息' }])
    ).toBeNull()
  })
})
