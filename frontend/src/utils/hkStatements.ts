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
  'free_cashflow'
] as const

const META_KEYS = new Set([
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

function failedChecks(row: StatementRow): Array<Record<string, unknown>> {
  const validation = (row.validation || {}) as Record<string, unknown>
  const checks = Array.isArray(validation.checks)
    ? (validation.checks as Array<Record<string, unknown>>)
    : []
  return checks.filter((check) => check.severity === 'error' && check.status === 'suspect')
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
      __sourceKinds: ['pdf']
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
        __sourceKinds: ['yahoo']
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

/** 来源标签：PDF / 雅虎 / PDF+雅虎；比较列单独说明 */
export function sourceLabel(row: HkPivotRow): string {
  const kinds = row.__sourceKinds
  const base =
    kinds.length === 2
      ? 'PDF+雅虎'
      : kinds[0] === 'yahoo'
        ? '雅虎'
        : row.__comparative
          ? 'PDF 比较列'
          : 'PDF'
  return base
}
