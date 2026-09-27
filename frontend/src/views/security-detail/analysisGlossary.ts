/**
 * 格雷厄姆准则的中文叫法 + AI 分析正文的「字段名 → 中文」展示期替换（纯函数，有 spec）。
 *
 * 准则名/判定词与后端 graham_screen.GRAHAM_CRITERIA_NAMES_ZH / GRAHAM_VERDICT_LABELS_ZH
 * 一致（改一边同步另一边）：准则卡与分析输入（name_zh / verdict_zh）用同一套叫法。
 *
 * humanizeAnalysisMarkdown 处理的是存量报告：2026-09 之前生成的分析常把输入字段名与英文
 * 判定词原样写进正文（`current_ratio 2.24 → pass`、`graham_screen.status=ok`、
 * `passed 7 / failed 0 / indeterminate 0`）。只在渲染前替换、库内原文不动；替换保守——
 * 只认格雷厄姆相关的一组标识符，英文判定词只在中文语境里换（两侧不是英文单词），
 * 代码片段、链接与 URL 原样保留。之后仍须经 renderMarkdown（marked + DOMPurify）。
 */

export const GRAHAM_CRITERION_NAMES: Record<string, string> = {
  current_ratio: '流动比率',
  lt_debt_vs_net_current_assets: '长期债务',
  earnings_stability: '盈利稳定性',
  dividend_record: '分红记录',
  earnings_growth: '盈利增长',
  pe: '市盈率',
  pb_or_product: '市净率'
}

// 准则卡上名称后面的阈值说明
const GRAHAM_CRITERION_RULES: Record<string, string> = {
  current_ratio: ' ≥ 2',
  lt_debt_vs_net_current_assets: ' ≤ 净流动资产',
  earnings_stability: '（十年为正）',
  dividend_record: '（连续分红）',
  earnings_growth: ' ≥ 1/3',
  pe: ' ≤ 15',
  pb_or_product: ' ≤ 1.5（或 PE×PB ≤ 22.5）'
}

export const GRAHAM_VERDICT_LABELS: Record<string, string> = {
  pass: '达标',
  fail: '不达标',
  indeterminate: '不可判定'
}

/** 准则卡标签：中文名 + 阈值（未知准则原样返回 key） */
export function grahamCriterionLabel(criterion: string): string {
  const name = GRAHAM_CRITERION_NAMES[criterion]
  return name ? name + (GRAHAM_CRITERION_RULES[criterion] || '') : criterion
}

// 脆弱性信号：与准则卡下方的脆弱性行同一叫法
const FRAGILITY_NAMES: Record<string, string> = {
  net_cash_to_market_cap: '净现金/市值',
  net_debt_to_assets: '净债务/总资产',
  debt_to_assets: '总负债率',
  interest_coverage: '利息覆盖倍数'
}

const IDENTIFIER_NAMES: Record<string, string> = {
  ...GRAHAM_CRITERION_NAMES,
  ...FRAGILITY_NAMES,
  lt_debt: '长期债务',
  basic_eps: '基本每股盈利',
  as_of_year: '锚定年度'
}
// 带下划线的标识符不会是普通英文单词，可直接按词界替换（pe 例外，见下）
const UNDERSCORE_IDENTIFIERS = Object.keys(IDENTIFIER_NAMES)
  .filter((key) => key.includes('_'))
  .sort((a, b) => b.length - a.length)

// 「中文名 + 字段名」重复书写（「流动比率 current_ratio」「市净率（pb_or_product）」）时只保留
// 中文——**删除字段名必须有重复的正面证据**：紧挨着的中文恰好是该字段自己的中文名（或下列
// 同义全称，正则片段）。PR #240 评审 P2：此前括号前只要是任意中文就删，「关键指标（basic_eps）
// 为 2.3 元」会丢掉唯一的指标名。不满足时字段名就地译成中文，不删。
const IDENTIFIER_ALIASES: Record<string, string[]> = {
  lt_debt_vs_net_current_assets: [
    '长期债务\\s*(?:vs\\.?|≤|<=|/|对|与|和)?\\s*净流动资产',
    '长期债务'
  ],
  earnings_stability: ['盈利稳定性', '盈利稳定'],
  dividend_record: ['连续分红记录', '分红记录'],
  pb_or_product: ['市净率\\s*/\\s*乘积条款', '市净率'],
  basic_eps: ['基本每股盈利', '基本每股收益'],
  debt_to_assets: ['总负债率', '资产负债率']
}

function aliasPatterns(key: string): string[] {
  return IDENTIFIER_ALIASES[key] ?? [escapeRegExp(IDENTIFIER_NAMES[key])]
}

// 以准则/信号名开头的中文：判定词后面接它时保留空格
const NAME_PREFIXES = [
  ...Object.values(GRAHAM_CRITERION_NAMES),
  ...Object.values(FRAGILITY_NAMES),
  '盈利稳定',
  '有息负债',
  '长期'
]
const VERDICT_WORDS = ['达标', '不达标', '不可判定', '未达标']

// 替换结果的两端标记：清理时去掉它与相邻中文之间的空格（「5 项 pass」→「5 项达标」）
const OPEN = '\u0001'
const CLOSE = '\u0002'
const zh = (text: string) => `${OPEN}${text}${CLOSE}`

const CJK = '\\u3000-\\u303f\\u4e00-\\u9fff\\uff00-\\uffef'

// 不参与替换的片段：围栏代码块、行内代码、Markdown 链接/图片、HTML 标签/自动链接、裸 URL
const PROTECTED =
  /(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|!?\[[^\]\n]*\]\([^)\n]*\)|<[^>\n]+>|(?:https?:\/\/|www\.)[^\s<>()（）]+)/g

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

// 判定词本身互为「中性邻居」：「作出 pass / fail / indeterminate 的」里 fail 两侧都是判定词
const NEUTRAL_WORDS = new Set(['pass', 'passed', 'fail', 'failed', 'indeterminate', 'verdict'])

type Side = 'good' | 'letter' | 'other' | 'edge'

// 从 index 起向 step 方向找最近的有意义字符：跳过空白、Markdown 强调符、斜杠分隔符、判定词
// 与全大写缩写
function sideOf(text: string, index: number, step: 1 | -1): Side {
  let i = index
  for (;;) {
    while (i >= 0 && i < text.length && /[ \t*/]/.test(text[i])) i += step
    if (i < 0 || i >= text.length || text[i] === '\n') return 'edge'
    const ch = text[i]
    if (/[A-Za-z]/.test(ch)) {
      let j = i
      while (j >= 0 && j < text.length && /[A-Za-z]/.test(text[j])) j += step
      const word = step === 1 ? text.slice(i, j) : text.slice(j + 1, i + 1)
      // 全大写缩写（PE、PB、TTM）是中文财经文本的常规写法，不构成英文语境
      if (!NEUTRAL_WORDS.has(word) && !/^[A-Z]{2,}$/.test(word)) return 'letter'
      i = j
      continue
    }
    if (/[0-9]/.test(ch) || ch.charCodeAt(0) > 0x7f) return 'good'
    return 'other'
  }
}

// 英文词只在中文语境里替换：紧邻不是字母/数字/连字符/撇号（排除 compass、pass-through），
// 两侧最近的有意义字符都不是英文单词（排除英文句子），且至少一侧是中文/全角符号/数字
function inChineseContext(text: string, start: number, end: number): boolean {
  const before = text[start - 1] ?? ''
  const after = text[end] ?? ''
  if (/[A-Za-z0-9_'\-.]/.test(before)) return false
  if (/[A-Za-z0-9_'-]/.test(after)) return false
  if (after === '.' && /[A-Za-z0-9]/.test(text[end + 1] ?? '')) return false
  const left = sideOf(text, start - 1, -1)
  const right = sideOf(text, end, 1)
  if (left === 'letter' || right === 'letter') return false
  return left === 'good' || right === 'good'
}

const CONTEXT_WORDS: Array<[RegExp, string, boolean]> = [
  // [模式, 替换, 是否中文结果（参与空格清理）]
  [/\bnot pass\b/g, '未达标', true],
  [/\bpass(?:ed)?\b/g, '达标', true],
  [/\bfail(?:ed)?\b/g, '不达标', true],
  [/\bindeterminate\b/g, '不可判定', true],
  [/\bverdicts?\b/g, '判定', true],
  [/\bbasis\b/g, '依据', true],
  [/\bsupplement\b/g, '补充口径', true],
  [/\bfragility\b/g, '脆弱性', true],
  [/\bcriteria\b/g, '准则', true],
  [/\bgraham\b/g, '格雷厄姆', true],
  // pe/pb 只统一大小写，不换成中文
  [/\bpe\b/g, 'PE', false],
  [/\bpb\b/g, 'PB', false]
]

function humanizePlain(input: string): string {
  let text = input

  // 1. 状态与取值
  text = text.replace(/graham_screen\.status\s*=\s*["“]?ok["”]?/g, zh('格雷厄姆准则'))
  text = text.replace(
    /graham_screen\.status\s*=\s*["“]?no_data["”]?/g,
    zh('格雷厄姆准则（无数据）')
  )
  text = text.replace(
    /\b(passed|failed|indeterminate)\s*=\s*(\d+)/g,
    (_m, word: string, count: string) =>
      `${zh(word === 'passed' ? '达标' : word === 'failed' ? '不达标' : '不可判定')} ${count}`
  )
  text = text.replace(
    /\bas_of_year\s*=\s*(\d{4})/g,
    (_m, year: string) => `${zh('锚定年度')} ${year}`
  )
  text = text.replace(/\bprice_stale\s*=\s*false\b/g, zh('价格未陈旧'))
  text = text.replace(/\bprice_stale\s*=\s*true\b/g, zh('价格陈旧'))
  text = text.replace(
    /\bprice_age_days\s*=\s*(\d+)/g,
    (_m, days: string) => `${zh('价格滞后')} ${days} ${zh('天')}`
  )
  text = text.replace(/\bvaluation_method\s*=\s*estimated\b/g, zh('估算口径'))
  text = text.replace(/\bvaluation_method\s*=\s*snapshot\b/g, zh('快照口径'))

  // 2. JSON 路径
  text = text.replace(/(?:graham_screen\.)?criteri(?:a|on)(?:\[\])?\.basis\b/g, zh('依据'))
  text = text.replace(/(?:graham_screen\.)?criteri(?:a|on)(?:\[\])?\.supplement\b/g, zh('补充口径'))
  text = text.replace(/(?:graham_screen\.)?fragility\.([a-z_]+)/g, (m, key: string) =>
    FRAGILITY_NAMES[key] ? zh(FRAGILITY_NAMES[key]) : m
  )
  text = text.replace(/graham_screen\.fragility\b/g, zh('脆弱性信号'))
  text = text.replace(/graham_screen\.criteria(?:\[\])?/g, zh('格雷厄姆准则'))
  text = text.replace(/\bgraham_screen\b/g, zh('格雷厄姆准则'))

  // 3. 重复书写去重：紧挨着的中文恰好是该字段自己的中文名时，才删掉后面的字段名
  //    （「市净率（pb_or_product）」「流动比率 current_ratio」→ 只留中文）；其余交给第 4 步就地翻译
  for (const key of [...UNDERSCORE_IDENTIFIERS, 'pe']) {
    const aliasAlt = aliasPatterns(key).join('|')
    const id = escapeRegExp(key)
    text = text.replace(
      new RegExp(
        `(${aliasAlt})(?:[ \\t]*[（(][ \\t]*${id}[ \\t]*[）)]|[ \\t]*${id}(?![\\w]))`,
        'g'
      ),
      '$1'
    )
  }

  // 4. 带下划线的标识符
  for (const key of UNDERSCORE_IDENTIFIERS) {
    text = text.replace(
      new RegExp(`(^|[^\\w.])${escapeRegExp(key)}(?![\\w])`, 'g'),
      (_m, prev: string) => `${prev}${zh(IDENTIFIER_NAMES[key])}`
    )
  }

  // 5. 英文判定词等：仅中文语境
  for (const [pattern, replacement, chinese] of CONTEXT_WORDS) {
    text = text.replace(pattern, (match: string, offset: number, whole: string) =>
      inChineseContext(whole, offset, offset + match.length)
        ? chinese
          ? zh(replacement)
          : replacement
        : match
    )
  }

  // 6. 替换结果与相邻中文之间的空格去掉（「5 项 pass」→「5 项达标」），但判定词后面紧跟
  //    准则名时保留空格（「- pass 盈利稳定性」→「- 达标 盈利稳定性」而不是粘成一串），再去掉标记
  text = text
    .replace(new RegExp(`([${CJK}])[ \\t]+${OPEN}`, 'g'), `$1${OPEN}`)
    .replace(
      new RegExp(`${CLOSE}[ \\t]+(?=[${CJK}]|${OPEN})`, 'g'),
      (match, offset: number, whole: string) => {
        const opened = whole.lastIndexOf(OPEN, offset)
        const replaced = opened >= 0 ? whole.slice(opened + 1, offset) : ''
        const rest = whole.slice(offset + match.length).replace(OPEN, '')
        return VERDICT_WORDS.includes(replaced) &&
          NAME_PREFIXES.some((name) => rest.startsWith(name))
          ? `${CLOSE} `
          : CLOSE
      }
    )
  return text.replace(new RegExp(`[${OPEN}${CLOSE}]`, 'g'), '')
}

/** AI 分析正文展示前的字段名/英文判定词中文化（存量报告兼容；库内原文不变） */
export function humanizeAnalysisMarkdown(markdown: string | null | undefined): string {
  if (!markdown) return ''
  let out = ''
  let last = 0
  for (const match of markdown.matchAll(PROTECTED)) {
    const index = match.index ?? 0
    out += humanizePlain(markdown.slice(last, index)) + match[0]
    last = index + match[0].length
  }
  return out + humanizePlain(markdown.slice(last))
}
