/**
 * 港股年度/中期报表透视行的前端合并（与后端 `earnings_quality.merge_hk_statement_rows` 同规则）：
 * PDF 抽取行（report_statements）优先，雅虎行只补缺；校验存疑的科目先置空再补；
 * 双方币种已知且一致才逐科目补数；按 (end_date, fp) 对齐，雅虎只有年度行。
 *
 * 纯函数、无组件依赖——规则复刻自后端，改后端规则时同步这里并补 spec。
 */

export type StatementRow = Record<string, unknown>

export type CellSource = 'pdf' | 'yahoo'

export interface HkPivotRow extends StatementRow {
  end_date: string
  fp: 'FY' | 'H1'
  currency?: string | null
  /** 每个科目的取值来源 */
  __source: Record<string, CellSource>
  /** 只有比较列（下一期报告的上期数），没有本期权威行 */
  __comparative: boolean
  /** 校验存疑（validation.status === 'suspect'） */
  __suspect: boolean
  /** 被置空的存疑科目（含随输入失效的派生科目） */
  __suspectFields: string[]
  /** 不通过的检查（severity=error 且 status=suspect），供 tooltip */
  __checks: Array<Record<string, unknown>>
  /** 参与本行的来源：pdf / yahoo */
  __sourceKinds: CellSource[]
  /** 构建层标注（EPS 仙→元、资产小计修复、已重列、雅虎口径不同），供标签与 tooltip */
  __notes: BuildNotes
}

/** 构建层（后端 STATEMENT_BUILD_VERSION）与校验 v4 留在行上的标注 */
export interface BuildNotes {
  /** 每股盈利原文以「仙」列示，已 ÷100 折元；basis = label / yahoo / chain / shares */
  epsCents: { basis: string | null } | null
  /** 映射认错无标签小计后被修复的科目：field → 说明 */
  repaired: Array<{ field: string; text: string }>
  /** 被更晚报告重列过的科目（保留首次披露值，只标注） */
  restated: string[]
  /** 与雅虎口径不同但与更晚报告的比较列一致的科目 */
  yahooDefinitionDiff: string[]
}

/** 参与"清洗"的数值科目（与后端 report_statement_checks.NUMERIC_FIELDS 一致） */
export const HK_NUMERIC_FIELDS = [
  'total_revenue',
  'cost_of_revenue',
  'gross_profit',
  'operating_income',
  'n_income_attr_p',
  'total_profit',
  'income_tax',
  'ebitda',
  'sga_exp',
  'int_exp',
  'basic_eps',
  'diluted_eps',
  'total_assets',
  'total_nca',
  'total_cur_assets',
  'total_cur_liab',
  'total_ncl',
  'accounts_receiv',
  'inventories',
  'fix_assets',
  'money_cap',
  'total_liab',
  'total_hldr_eqy_exc_min_int',
  'total_equity',
  'minority_int',
  'total_debt',
  'n_cashflow_act',
  'capex',
  'depr_fa_coga_dpba',
  'free_cashflow',
  'mezzanine_equity',
  'lt_borr',
  'st_borr',
  'div_paid_owners'
] as const

const META_KEYS = new Set([
  'build_version',
  'eps_unit',
  'repaired_fields',
  'restated_by_kind',
  'end_date',
  'fp',
  'currency',
  'is_comparative',
  'source_period_key',
  'source_report_type',
  'source_end_date',
  'source_url',
  'source_fingerprint',
  'source_pages',
  'source_by_kind',
  'extractor_version',
  'prompt_version',
  'validation',
  'currency_by_kind',
  'unit_by_kind',
  'derived_fields',
  'comparative_evidence'
])

const DEFAULT_DERIVED: Record<string, string[]> = { free_cashflow: ['n_cashflow_act', 'capex'] }

function periodOf(row: StatementRow): { endDate: string; fp: 'FY' | 'H1' } {
  const fp = row.fp === 'H1' ? 'H1' : 'FY'
  return { endDate: String(row.end_date ?? ''), fp }
}

// 分项合计类派生科目（与后端 report_statement_checks.SUM_DERIVED_FIELDS 一致）
const SUM_DERIVED_FIELDS: Record<string, string[]> = {
  total_assets: ['total_nca', 'total_cur_assets'],
  total_liab: ['total_cur_liab', 'total_ncl'],
  total_equity: ['total_hldr_eqy_exc_min_int', 'minority_int']
}

function numeric(value: unknown): number | null {
  if (value == null || value === '') return null
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? n : null
}

/**
 * 后端 rederive_fields 的复刻（就地）：清洗时随输入一起失效的派生科目，在雅虎补回输入后
 * 重新推导——FCF = CFO − |capex|，分项合计按 derived_fields / SUM_DERIVED_FIELDS。只填为空的
 * 派生科目，不覆盖已有值；任一输入来自雅虎则该派生值也标为雅虎来源（上标可见）。
 */
export function rederiveFields(row: HkPivotRow): void {
  for (const [field, inputs] of Object.entries(derivedInputs(row))) {
    if (row[field] != null) continue
    const values = inputs.map((input) => numeric(row[input]))
    if (values.some((v) => v === null)) continue
    const nums = values as number[]
    let derived: number | null = null
    if (field === 'free_cashflow') derived = nums[0] - Math.abs(nums[1])
    else if (field in SUM_DERIVED_FIELDS) derived = nums.reduce((a, b) => a + b, 0)
    if (derived === null) continue
    row[field] = derived
    row.__source[field] = inputs.some((input) => row.__source[input] === 'yahoo') ? 'yahoo' : 'pdf'
  }
}

function derivedInputs(row: StatementRow): Record<string, string[]> {
  const recorded = row.derived_fields
  if (recorded && typeof recorded === 'object' && Object.keys(recorded as object).length > 0) {
    return recorded as Record<string, string[]>
  }
  return DEFAULT_DERIVED
}

/** 后端 scrub_suspect_fields 的复刻：返回被置空的科目集合（不修改入参） */
export function suspectFieldsToScrub(row: StatementRow): string[] {
  const validation = (row.validation || {}) as Record<string, unknown>
  if (validation.status !== 'suspect') return []
  const fields = Array.isArray(validation.suspect_fields)
    ? (validation.suspect_fields as string[])
    : []
  const rowLevel =
    validation.row_level == null ? fields.length === 0 : Boolean(validation.row_level)
  if (rowLevel) return [...HK_NUMERIC_FIELDS]
  const cleared = new Set(fields)
  const derived = derivedInputs(row)
  let changed = true
  while (changed) {
    changed = false
    for (const [field, inputs] of Object.entries(derived)) {
      if (!cleared.has(field) && inputs.some((input) => cleared.has(input))) {
        cleared.add(field)
        changed = true
      }
    }
  }
  return [...cleared]
}

function checksOf(row: StatementRow): Array<Record<string, unknown>> {
  const validation = (row.validation || {}) as Record<string, unknown>
  return Array.isArray(validation.checks)
    ? (validation.checks as Array<Record<string, unknown>>)
    : []
}

function failedChecks(row: StatementRow): Array<Record<string, unknown>> {
  return checksOf(row).filter((check) => check.severity === 'error' && check.status === 'suspect')
}

// 与后端 report_statement_checks.RESTATED_REASONS 一致
const RESTATED_REASONS = new Set(['comparative_restated', 'yahoo_restated'])

function fieldsWithReason(row: StatementRow, reasons: Set<string>): string[] {
  const out: string[] = []
  for (const check of checksOf(row)) {
    if (!reasons.has(String(check.reason || ''))) continue
    for (const field of (check.fields as string[]) || []) {
      if (!out.includes(field)) out.push(field)
    }
  }
  return out
}

/**
 * 构建层标注：EPS 以仙列示已折元（eps_unit）、映射修复（repaired_fields：资产小计 / EPS 附注号）、
 * 已重列与雅虎口径不同（validation.checks[].reason，校验 v4）。都是 info——不置空、不算存疑，只供展示。
 */
export function buildNotes(row: StatementRow): BuildNotes {
  const epsUnit = row.eps_unit as Record<string, unknown> | undefined
  const repairedRaw = (row.repaired_fields || {}) as Record<string, Record<string, unknown>>
  const repaired = Object.entries(repairedRaw).map(([field, item]) => {
    if (item.reason === 'eps_note_number') {
      // 构建 v2：映射指向「每股盈利 13」小标题（13 是附注号）→ 改指其后的基本/摊薄行，或弃用交雅虎补缺
      const to = item.to_row ? `行 ${item.to_row}` : '弃用（由雅虎补缺）'
      return {
        field,
        text: `${fieldLabel(field)}：行 ${item.from_row}（附注号 ${item.note_number}）→ ${to}`
      }
    }
    const from = item.from_row ? `行 ${item.from_row}` : '未映射'
    const to = item.to_row ? `行 ${item.to_row}` : '分项合计推导'
    return { field, text: `${fieldLabel(field)}：${from} → ${to}` }
  })
  return {
    epsCents:
      epsUnit && Number(epsUnit.divisor) === 100
        ? { basis: (epsUnit.basis as string | null) ?? null }
        : null,
    repaired,
    restated: fieldsWithReason(row, RESTATED_REASONS),
    yahooDefinitionDiff: fieldsWithReason(row, new Set(['yahoo_definition_diff']))
  }
}

const EPS_BASIS_TEXT: Record<string, string> = {
  label: '报表行名/表头注明',
  yahoo: '与雅虎同财年 EPS 相差 100 倍',
  chain: '与相邻报告的比较列首尾相接',
  shares: '按归母净利 / EPS 推算的股数与仙报告一致'
}

/** EPS 单元格 tooltip：以仙列示已折元时说明依据 */
export function epsNoteText(notes: BuildNotes): string {
  if (!notes.epsCents) return ''
  const basis = notes.epsCents.basis
    ? EPS_BASIS_TEXT[notes.epsCents.basis] || notes.epsCents.basis
    : ''
  return `原文以「仙」列示，已 ÷100 折为元${basis ? `（依据：${basis}）` : ''}`
}

/** 「已重列 / 已修正 / 口径不同」标签的 tooltip 文案；没有标注返回空串 */
export function buildNotesText(notes: BuildNotes): string {
  const parts: string[] = []
  if (notes.restated.length) {
    parts.push(
      `已被后续报告重列：${notes.restated.map(fieldLabel).join('、')}（显示与分析均保留首次披露值）`
    )
  }
  if (notes.repaired.length) {
    parts.push(`映射已修正：${notes.repaired.map((item) => item.text).join('；')}`)
  }
  if (notes.yahooDefinitionDiff.length) {
    parts.push(
      `与雅虎口径不同：${notes.yahooDefinitionDiff.map(fieldLabel).join('、')}（与后续报告的比较列一致）`
    )
  }
  return parts.join('。')
}

export interface MergeOptions {
  includeInterim?: boolean
}

/**
 * pdfRows = report_statements 数据集（年度 + 中报，含比较期行）；yahooRows = yahoo_fundamentals。
 * 返回按 end_date 倒序（同期年报在中报前）的透视行。
 */
export function mergeHkPivotRows(
  pdfRows: StatementRow[],
  yahooRows: StatementRow[],
  options: MergeOptions = {}
): HkPivotRow[] {
  const includeInterim = options.includeInterim === true
  const byPeriod = new Map<string, HkPivotRow>()

  for (const row of pdfRows || []) {
    const { endDate, fp } = periodOf(row)
    if (fp === 'H1' && !includeInterim) continue
    const scrub = new Set(suspectFieldsToScrub(row))
    const merged: HkPivotRow = {
      ...row,
      end_date: endDate,
      fp,
      __source: {},
      __comparative: row.is_comparative === true,
      __suspect:
        scrub.size > 0 ||
        (row.validation as Record<string, unknown> | undefined)?.status === 'suspect',
      __suspectFields: [...scrub],
      __checks: failedChecks(row),
      __sourceKinds: ['pdf'],
      __notes: buildNotes(row)
    }
    for (const field of Object.keys(row)) {
      if (META_KEYS.has(field)) continue
      if (scrub.has(field)) {
        merged[field] = null
        continue
      }
      if (row[field] != null) merged.__source[field] = 'pdf'
    }
    byPeriod.set(`${endDate}|${fp}`, merged)
  }

  for (const row of yahooRows || []) {
    const { endDate, fp } = periodOf(row)
    if (fp !== 'FY') continue
    const key = `${endDate}|FY`
    const pdf = byPeriod.get(key)
    if (!pdf) {
      const fresh: HkPivotRow = {
        ...row,
        end_date: endDate,
        fp: 'FY',
        __source: {},
        __comparative: false,
        __suspect: false,
        __suspectFields: [],
        __checks: [],
        __sourceKinds: ['yahoo'],
        __notes: buildNotes({})
      }
      for (const field of Object.keys(row)) {
        if (!META_KEYS.has(field) && row[field] != null) fresh.__source[field] = 'yahoo'
      }
      byPeriod.set(key, fresh)
      continue
    }
    // 双方币种都已知且一致才逐科目补数；任一侧未知或不同 → PDF 行原样，不借金额也不贴币种
    if (!pdf.currency || !row.currency || pdf.currency !== row.currency) continue
    let filled = false
    for (const [field, value] of Object.entries(row)) {
      if (META_KEYS.has(field) || value == null) continue
      if (pdf[field] == null) {
        pdf[field] = value
        pdf.__source[field] = 'yahoo'
        filled = true
      }
    }
    // 与后端 merge_hk_statement_rows 同位置：补缺后重推导派生科目，详情页与分析输入口径一致
    rederiveFields(pdf)
    if (filled && !pdf.__sourceKinds.includes('yahoo')) pdf.__sourceKinds.push('yahoo')
  }

  return [...byPeriod.values()].sort((a, b) => {
    const byDate = b.end_date.localeCompare(a.end_date)
    if (byDate !== 0) return byDate
    return a.fp === b.fp ? 0 : a.fp === 'FY' ? -1 : 1
  })
}

/** 来源标签：PDF / 雅虎 / PDF+雅虎；比较列单独说明（雅虎补缺后「比较列」标记不丢） */
export function sourceLabel(row: HkPivotRow): string {
  const kinds = row.__sourceKinds
  if (kinds.length === 1 && kinds[0] === 'yahoo') return '雅虎'
  const pdf = row.__comparative ? 'PDF 比较列' : 'PDF'
  return kinds.includes('yahoo') ? `${pdf}+雅虎` : pdf
}

/**
 * 科目中文名：覆盖 HK_NUMERIC_FIELDS 全部 34 个（后端 report_statement_prompts.STATEMENT_FIELDS
 * 的 32 个 + 派生的 free_cashflow + 按行名取值的 mezzanine_equity）。存疑 tooltip、校验说明里的
 * 英文字段名都经它转中文。
 */
export const HK_FIELD_LABELS: Record<(typeof HK_NUMERIC_FIELDS)[number], string> = {
  total_revenue: '营业收入',
  cost_of_revenue: '营业成本',
  gross_profit: '毛利',
  operating_income: '经营利润',
  n_income_attr_p: '归母净利润',
  total_profit: '税前利润',
  income_tax: '所得税',
  ebitda: 'EBITDA',
  sga_exp: '销售及管理费用',
  int_exp: '利息费用',
  basic_eps: '基本每股收益',
  diluted_eps: '摊薄每股收益',
  total_assets: '总资产',
  total_nca: '非流动资产',
  total_cur_assets: '流动资产',
  total_cur_liab: '流动负债',
  total_ncl: '非流动负债',
  accounts_receiv: '应收账款',
  inventories: '存货',
  fix_assets: '固定资产',
  money_cap: '现金及等价物',
  total_liab: '总负债',
  total_hldr_eqy_exc_min_int: '归母权益',
  total_equity: '权益总额',
  minority_int: '少数股东权益',
  total_debt: '有息借款',
  n_cashflow_act: '经营现金流',
  capex: '资本开支',
  depr_fa_coga_dpba: '折旧及摊销',
  free_cashflow: '自由现金流',
  mezzanine_equity: '夹层权益',
  lt_borr: '非流动借款',
  st_borr: '流动借款',
  div_paid_owners: '已付股东股息'
}

export function fieldLabel(field: string): string {
  return (HK_FIELD_LABELS as Record<string, string>)[field] || field
}

// 长字段名在前，避免 total_assets 先于 total_assets_xxx 被替换（当前无前缀冲突，防御性）
const FIELD_NAME_RE = new RegExp(
  `\\b(${[...HK_NUMERIC_FIELDS].sort((a, b) => b.length - a.length).join('|')})\\b`,
  'g'
)

/** 把校验说明（如「total_revenue 与雅虎相差 3.2%」）里的英文字段名换成中文 */
export function localizeFieldNames(text: string): string {
  return text.replace(FIELD_NAME_RE, (name) => fieldLabel(name))
}

/**
 * 单元格是否「存疑已置空」：该科目被清洗且未被雅虎补回。与「缺数据」区分展示。
 */
export function isScrubbedCell(row: HkPivotRow, field: string): boolean {
  return row.__suspectFields.includes(field) && row[field] == null
}

/**
 * 存疑 tooltip 文案：置空科目（中文名；整行置空时不逐个列 30 个科目）+ 不通过的检查说明。
 */
export function suspectTooltipText(row: HkPivotRow): string {
  const parts: string[] = []
  const fields = row.__suspectFields
  if (fields.length >= HK_NUMERIC_FIELDS.length) {
    parts.push('整行校验不通过，全部科目已置空')
  } else if (fields.length) {
    parts.push(`已置空：${fields.map(fieldLabel).join('、')}`)
  }
  const reasons = row.__checks
    .map((check) => localizeFieldNames(String(check.detail || check.id || '')))
    .filter(Boolean)
  if (reasons.length) parts.push(reasons.join('；'))
  return parts.join('。') || '校验存疑'
}

/**
 * 会计期键 → 人话：「20251231|FY」→「2025 年报」，「20250630|H1」→「2025 中报」；
 * 非自然年财年（期末不是 12 月年报 / 6 月中报）带上月份：「20230630|FY」→「2023-06 年报」。
 */
export function formatStatementPeriodKey(key: string): string {
  const [endDate, fp] = String(key || '').split('|')
  if (!/^\d{8}$/.test(endDate || '')) return key || ''
  const year = endDate.slice(0, 4)
  const month = endDate.slice(4, 6)
  const interim = fp === 'H1'
  const natural = interim ? month === '06' : month === '12'
  return `${natural ? year : `${year}-${month}`} ${interim ? '中报' : '年报'}`
}

export interface PivotCurrencySummary {
  /** 多数币种（行数最多者；并列取先出现的） */
  majority: string
  /** 全部已知币种行一致且无未知币种行 */
  uniform: boolean
}

/**
 * 透视表币种：标题只在全部行一致时写币种，否则提示看币种列；与多数不一致（含未知）的行高亮。
 */
export function summarizePivotCurrency(rows: StatementRow[]): PivotCurrencySummary {
  const counts = new Map<string, number>()
  let unknown = 0
  for (const row of rows) {
    const currency = row.currency ? String(row.currency) : ''
    if (!currency) {
      unknown += 1
      continue
    }
    counts.set(currency, (counts.get(currency) || 0) + 1)
  }
  let majority = ''
  let best = 0
  for (const [currency, count] of counts) {
    if (count > best) {
      majority = currency
      best = count
    }
  }
  return { majority, uniform: counts.size === 1 && unknown === 0 }
}

export function isCurrencyOutlier(row: StatementRow, summary: PivotCurrencySummary): boolean {
  if (summary.uniform) return false
  return !row.currency || String(row.currency) !== summary.majority
}
