import { describe, expect, it } from 'vitest'
import {
  GRAHAM_CRITERION_NAMES,
  GRAHAM_VERDICT_LABELS,
  grahamCriterionLabel,
  humanizeAnalysisMarkdown as h
} from './analysisGlossary'

describe('格雷厄姆准则中文名', () => {
  it('与后端 graham_screen.GRAHAM_CRITERIA_NAMES_ZH / GRAHAM_VERDICT_LABELS_ZH 一致', () => {
    // 改一边必须同步另一边：准则卡与 AI 正文用同一套叫法
    expect(GRAHAM_CRITERION_NAMES).toEqual({
      current_ratio: '流动比率',
      lt_debt_vs_net_current_assets: '长期债务',
      earnings_stability: '盈利稳定性',
      dividend_record: '分红记录',
      earnings_growth: '盈利增长',
      pe: '市盈率',
      pb_or_product: '市净率'
    })
    expect(GRAHAM_VERDICT_LABELS).toEqual({
      pass: '达标',
      fail: '不达标',
      indeterminate: '不可判定'
    })
  })

  it('准则卡标签 = 中文名 + 阈值；未知准则原样', () => {
    expect(grahamCriterionLabel('current_ratio')).toBe('流动比率 ≥ 2')
    expect(grahamCriterionLabel('lt_debt_vs_net_current_assets')).toBe('长期债务 ≤ 净流动资产')
    expect(grahamCriterionLabel('pb_or_product')).toBe('市净率 ≤ 1.5（或 PE×PB ≤ 22.5）')
    expect(grahamCriterionLabel('earnings_stability')).toBe('盈利稳定性（十年为正）')
    expect(grahamCriterionLabel('new_rule')).toBe('new_rule')
  })
})

describe('humanizeAnalysisMarkdown：生产报告实例（2026-09-27）', () => {
  it('逐项判定行', () => {
    expect(
      h(
        '逐项 verdict（graham_screen.status=ok，锚定 2025 年）：current_ratio 2.24 → pass；' +
          '长期债务 1.98 亿 ≤ 净流动资产 233.2 亿 → pass；合计 passed 7 / failed 0 / indeterminate 0。'
      )
    ).toBe(
      '逐项判定（格雷厄姆准则，锚定 2025 年）：流动比率 2.24 → 达标；' +
        '长期债务 1.98 亿 ≤ 净流动资产 233.2 亿 → 达标；合计达标 7 / 不达标 0 / 不可判定 0。'
    )
  })

  it('解读句里的 pass 项 / 无 fail', () => {
    expect(h('pass 项分别对应短期偿付能力；无 fail、无 indeterminate，覆盖无缺口')).toBe(
      '达标项分别对应短期偿付能力；无不达标、无不可判定，覆盖无缺口'
    )
  })

  it('graham dividend_record = N', () => {
    // graham 前缀是译而不是删（删除类规则只用于有重复证据的场合）
    expect(h('连续 11 年（graham dividend_record = 11）')).toBe(
      '连续 11 年（格雷厄姆分红记录 = 11）'
    )
  })

  it('数据质量提示里的 basic_eps / supplement', () => {
    expect(
      h(
        '**数据质量提示（不改写 verdict）**：earnings_growth 所引用的 2016 年 basic_eps 为 13.0 ，' +
          'supplement 中的年报静态 PE 14.67'
      )
    ).toBe(
      '**数据质量提示（不改写判定）**：盈利增长所引用的 2016 年基本每股盈利为 13.0 ，' +
        '补充口径中的年报静态 PE 14.67'
    )
  })

  it('与 graham_screen 的 dividend_record pass 一致', () => {
    expect(h('与 graham_screen 的 dividend_record pass 一致')).toBe(
      '与格雷厄姆准则的分红记录达标一致'
    )
  })

  it('列表写法：粗体｜分隔、冒号、箭头、括号', () => {
    expect(h('- **pass｜earnings_stability**：2016→2025')).toBe('- **达标｜盈利稳定性**：2016→2025')
    expect(h('- pb_or_product：fail —— PB 2.09 > 1.5')).toBe('- 市净率：不达标 —— PB 2.09 > 1.5')
    expect(h('- **pe → indeterminate**、**pb_or_product → indeterminate**')).toBe(
      '- **PE → 不可判定**、**市净率 → 不可判定**'
    )
    expect(h('- **分红记录（fail）**：2019–2025')).toBe('- **分红记录（不达标）**：2019–2025')
    expect(h('综合：5 项 pass、0 项 fail、2 项 indeterminate。')).toBe(
      '综合：5 项达标、0 项不达标、2 项不可判定。'
    )
    expect(h('（as_of_year=2025，passed=4 / failed=3 / indeterminate=0）')).toBe(
      '（锚定年度 2025，达标 4 / 不达标 3 / 不可判定 0）'
    )
    expect(h('这是本次唯一被 not pass 的财务强度项')).toBe('这是本次唯一被未达标的财务强度项')
    expect(h('作出 pass / fail / indeterminate 的判断')).toBe('作出达标 / 不达标 / 不可判定的判断')
  })

  it('中文名后面的字段名注释只留中文', () => {
    expect(h('- 流动比率 current_ratio：**fail**')).toBe('- 流动比率：**不达标**')
    expect(h('- 分红记录（dividend_record）：pass')).toBe('- 分红记录：达标')
    expect(h('- 市盈率（pe）：fail。')).toBe('- 市盈率：不达标。')
    expect(h('长期债务 vs 净流动资产（lt_debt_vs_net_current_assets）')).toBe(
      '长期债务 vs 净流动资产'
    )
  })

  it('PR #240 评审 P2：前面的中文不是该字段自己的名字时，字段名就地翻译、不删', () => {
    expect(h('关键指标（basic_eps）为 2.3 元。')).toBe('关键指标（基本每股盈利）为 2.3 元。')
    expect(h('唯一不达标的项目（current_ratio）需要关注。')).toBe(
      '唯一不达标的项目（流动比率）需要关注。'
    )
    expect(h('估值端（pb_or_product）偏贵')).toBe('估值端（市净率）偏贵')
    expect(h('杠杆（debt_to_assets）0.62')).toBe('杠杆（总负债率）0.62')
    expect(h('估值（pe）偏低')).toBe('估值（PE）偏低')
    // 只是别名的一部分不算重复：「净流动资产」≠ 长期债务准则
    expect(h('低于净流动资产 lt_debt_vs_net_current_assets 判定')).toBe(
      '低于净流动资产长期债务判定'
    )
    expect(h('连续分红 dividend_record 为 11 年')).toBe('连续分红分红记录为 11 年')
    expect(h('利润（earnings_growth）')).toBe('利润（盈利增长）')
  })

  it('真正的重复书写仍然去重', () => {
    expect(h('流动比率（current_ratio）')).toBe('流动比率')
    expect(h('基本每股收益（basic_eps）为 2.3 元')).toBe('基本每股收益为 2.3 元')
    expect(h('长期债务/净流动资产（lt_debt_vs_net_current_assets）')).toBe('长期债务/净流动资产')
    expect(h('市净率/乘积条款（pb_or_product）：不达标')).toBe('市净率/乘积条款：不达标')
    expect(h('- 盈利稳定性 earnings_stability：**pass**')).toBe('- 盈利稳定性：**达标**')
    expect(h('净债务/总资产 net_debt_to_assets -0.07')).toBe('净债务/总资产 -0.07')
  })

  it('判定词后接准则名时保留空格，不粘成一串', () => {
    expect(h('- pass 盈利稳定性：2016→2025')).toBe('- 达标 盈利稳定性：2016→2025')
    expect(h('- fail 流动比率：2025 年 1.74')).toBe('- 不达标 流动比率：2025 年 1.74')
    expect(h('- pass PE：PE(TTM) 6.90')).toBe('- 达标 PE：PE(TTM) 6.90')
  })

  it('JSON 路径与脆弱性字段', () => {
    expect(h('价格与汇率见 criteria[].basis。')).toBe('价格与汇率见依据。')
    expect(h('以下仅解读 graham_screen.criteria 已给出的 verdict')).toBe(
      '以下仅解读格雷厄姆准则已给出的判定'
    )
    expect(h('**下行保护**（fragility 信号）：net_debt_to_assets -0.0659')).toBe(
      '**下行保护**（脆弱性信号）：净债务/总资产 -0.0659'
    )
    expect(h('价格年龄 2 天，price_stale=false）')).toBe('价格年龄 2 天，价格未陈旧）')
  })
})

describe('humanizeAnalysisMarkdown：不该动的', () => {
  it('英文单词与英文句子', () => {
    const cases = [
      'Management uses a compass metaphor.',
      '成本 pass-through 机制较完善',
      '管理层称 "we did not pass the audit" 属于引述',
      'The deal failed to close.',
      'passing grade',
      '采用 pass through 条款',
      'Bypass 策略'
    ]
    for (const text of cases) expect(h(text)).toBe(text)
  })

  it('代码片段、链接、URL 原样保留', () => {
    const cases = [
      '`graham_screen.status` 为 `no_data`，未提供 `criteria` 逐项',
      '见 [pass 率](https://example.com/pass?current_ratio=2)',
      '来源 https://example.com/fail/current_ratio 原文',
      '<https://example.com/graham_screen>',
      '```\ncurrent_ratio: pass\n```'
    ]
    for (const text of cases) expect(h(text)).toBe(text)
  })

  it('不把 PE/PB 换成中文，只统一小写独立缩写', () => {
    expect(h('PE 7.5、PB 1.2')).toBe('PE 7.5、PB 1.2')
    expect(h('- pe 10.40：TTM')).toBe('- PE 10.40：TTM')
    expect(h('pe_ttm 字段')).toBe('pe_ttm 字段')
    expect(h('open pe ratio')).toBe('open pe ratio')
  })

  it('无关的下划线字段与空值', () => {
    expect(h('earnings_quality 的 cfo_ni_ratio')).toBe('earnings_quality 的 cfo_ni_ratio')
    expect(h('')).toBe('')
    expect(h(null)).toBe('')
    expect(h(undefined)).toBe('')
  })

  it('幂等：已是中文的正文不变', () => {
    const text =
      '## 格雷厄姆准则解读\n\n- 流动比率：达标。\n\n达标 5 项、不达标 1 项、不可判定 1 项'
    expect(h(text)).toBe(text)
    expect(h(h('- current_ratio：pass'))).toBe(h('- current_ratio：pass'))
  })
})
