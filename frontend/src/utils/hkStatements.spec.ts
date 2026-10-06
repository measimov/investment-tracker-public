import { describe, expect, it } from 'vitest'
import {
  HK_FIELD_LABELS,
  HK_NUMERIC_FIELDS,
  buildNotes,
  buildNotesText,
  currencySwitchText,
  epsNoteText,
  formatStatementPeriodKey,
  isCurrencyOutlier,
  isScrubbedCell,
  localizeFieldNames,
  mergeHkPivotRows,
  sourceLabel,
  summarizePivotCurrency,
  suspectFieldsToScrub,
  suspectTooltipText
} from './hkStatements'

const pdf2025 = {
  end_date: '20251231',
  fp: 'FY',
  currency: 'CNY',
  is_comparative: false,
  total_revenue: 100,
  cost_of_revenue: 60,
  n_cashflow_act: 30,
  capex: -10,
  free_cashflow: 20,
  derived_fields: { free_cashflow: ['n_cashflow_act', 'capex'] },
  validation: { status: 'ok', suspect_fields: [], row_level: false, checks: [] }
}

describe('mergeHkPivotRows', () => {
  it('本身存疑的派生科目不因雅虎补缺而复活（#343-3，与后端 rederive_fields 同口径）', () => {
    const row = {
      end_date: '20251231',
      fp: 'FY',
      currency: 'CNY',
      is_comparative: false,
      total_hldr_eqy_exc_min_int: 800,
      minority_int: 50,
      total_equity: 850,
      total_liab: 100,
      derived_fields: { total_equity: ['total_hldr_eqy_exc_min_int', 'minority_int'] },
      validation: {
        status: 'suspect',
        row_level: false,
        suspect_fields: ['total_liab', 'total_equity'],
        checks: []
      }
    }
    const withYahoo = mergeHkPivotRows(
      [row],
      [{ end_date: '20251231', fp: 'FY', currency: 'CNY', total_liab: 120 }]
    )
    const withoutYahoo = mergeHkPivotRows([row], [])
    expect(withYahoo[0].total_equity).toBeNull()
    expect(withoutYahoo[0].total_equity).toBeNull()
    expect(withYahoo[0].total_liab).toBe(120)
  })

  it('PDF 行优先，雅虎只补缺并标注来源', () => {
    const rows = mergeHkPivotRows(
      [pdf2025],
      [{ end_date: '20251231', fp: 'FY', currency: 'CNY', total_revenue: 999, total_assets: 500 }]
    )
    expect(rows).toHaveLength(1)
    expect(rows[0].total_revenue).toBe(100)
    expect(rows[0].total_assets).toBe(500)
    expect(rows[0].__source.total_revenue).toBe('pdf')
    expect(rows[0].__source.total_assets).toBe('yahoo')
    expect(rows[0].__sourceKinds).toEqual(['pdf', 'yahoo'])
    expect(sourceLabel(rows[0])).toBe('PDF+雅虎')
  })

  it('币种未知或不同时不补数，也不贴币种', () => {
    const rows = mergeHkPivotRows(
      [{ ...pdf2025, currency: null }],
      [{ end_date: '20251231', fp: 'FY', currency: 'CNY', total_assets: 500 }]
    )
    expect(rows[0].total_assets).toBeUndefined()
    expect(rows[0].currency).toBeNull()
    const hkd = mergeHkPivotRows(
      [pdf2025],
      [{ end_date: '20251231', fp: 'FY', currency: 'HKD', total_assets: 500 }]
    )
    expect(hkd[0].total_assets).toBeUndefined()
    expect(sourceLabel(hkd[0])).toBe('PDF')
  })

  it('存疑科目先置空再由雅虎补，派生科目随输入失效', () => {
    const suspect = {
      ...pdf2025,
      validation: {
        status: 'suspect',
        row_level: false,
        suspect_fields: ['n_cashflow_act'],
        checks: [
          {
            id: 'yahoo_n_cashflow_act',
            severity: 'error',
            status: 'suspect',
            detail: 'CFO 差 40%'
          },
          { id: 'x', severity: 'info', status: 'suspect', detail: '不进 tooltip' }
        ]
      }
    }
    expect(suspectFieldsToScrub(suspect).sort()).toEqual(['free_cashflow', 'n_cashflow_act'])
    const rows = mergeHkPivotRows(
      [suspect],
      [{ end_date: '20251231', fp: 'FY', currency: 'CNY', n_cashflow_act: 50 }]
    )
    expect(rows[0].__suspect).toBe(true)
    expect(rows[0].n_cashflow_act).toBe(50)
    expect(rows[0].__source.n_cashflow_act).toBe('yahoo')
    expect(rows[0].free_cashflow).toBe(40) // 雅虎补回 CFO 后重算：50 − |−10|
    expect(rows[0].__checks.map((c) => c.detail)).toEqual(['CFO 差 40%'])
    expect(rows[0].total_revenue).toBe(100)
  })

  it('雅虎补回被清空的输入后重算派生科目（评审 P2：total_assets = 60 + 50 = 110）', () => {
    const suspect = {
      end_date: '20251231',
      fp: 'FY',
      currency: 'CNY',
      total_nca: 60,
      total_cur_assets: 999,
      total_assets: 1059,
      derived_fields: { total_assets: ['total_nca', 'total_cur_assets'] },
      validation: {
        status: 'suspect',
        row_level: false,
        suspect_fields: ['total_cur_assets'],
        checks: []
      }
    }
    const rows = mergeHkPivotRows(
      [suspect],
      [{ end_date: '20251231', fp: 'FY', currency: 'CNY', total_cur_assets: 50 }]
    )
    expect(rows[0].total_cur_assets).toBe(50)
    expect(rows[0].total_assets).toBe(110)
    expect(rows[0].__source.total_assets).toBe('yahoo')
    // 雅虎没有该输入：派生值保持空，不复活旧值
    const none = mergeHkPivotRows(
      [suspect],
      [{ end_date: '20251231', fp: 'FY', currency: 'CNY', total_revenue: 1 }]
    )
    expect(none[0].total_assets).toBeNull()
  })

  it('FCF 在雅虎补回 CFO 后按 CFO − |capex| 重算', () => {
    const suspect = {
      ...pdf2025,
      validation: {
        status: 'suspect',
        row_level: false,
        suspect_fields: ['n_cashflow_act'],
        checks: []
      }
    }
    const rows = mergeHkPivotRows(
      [suspect],
      [{ end_date: '20251231', fp: 'FY', currency: 'CNY', n_cashflow_act: 50 }]
    )
    expect(rows[0].free_cashflow).toBe(40)
  })

  it('整行不可信（row_level）全部数值置空；旧版无 row_level 且无字段视为整行', () => {
    const rowLevel = {
      ...pdf2025,
      validation: { status: 'suspect', row_level: true, suspect_fields: [] }
    }
    expect(mergeHkPivotRows([rowLevel], [])[0].total_revenue).toBeNull()
    const legacy = { ...pdf2025, validation: { status: 'suspect', suspect_fields: [] } }
    expect(mergeHkPivotRows([legacy], [])[0].cost_of_revenue).toBeNull()
  })

  it('中报默认不展示，开关打开后同期年报排在中报前；雅虎只有年度行', () => {
    const h1 = { ...pdf2025, end_date: '20250630', fp: 'H1' }
    const cmp2024 = { ...pdf2025, end_date: '20241231', is_comparative: true }
    expect(mergeHkPivotRows([pdf2025, h1, cmp2024], []).map((r) => r.end_date)).toEqual([
      '20251231',
      '20241231'
    ])
    const withInterim = mergeHkPivotRows([h1, pdf2025, cmp2024], [], { includeInterim: true })
    expect(withInterim.map((r) => `${r.end_date}|${r.fp}`)).toEqual([
      '20251231|FY',
      '20250630|H1',
      '20241231|FY'
    ])
    expect(withInterim[2].__comparative).toBe(true)
    expect(sourceLabel(withInterim[2])).toBe('PDF 比较列')
    const yahooOnly = mergeHkPivotRows(
      [],
      [{ end_date: '20231231', fp: 'FY', currency: 'CNY', total_revenue: 1 }]
    )
    expect(sourceLabel(yahooOnly[0])).toBe('雅虎')
    expect(yahooOnly[0].__source.total_revenue).toBe('yahoo')
  })
})

describe('展示辅助（#221）', () => {
  it('比较列被雅虎补缺后仍标「PDF 比较列+雅虎」', () => {
    const cmp = { ...pdf2025, end_date: '20241231', is_comparative: true, total_assets: null }
    const rows = mergeHkPivotRows(
      [cmp],
      [{ end_date: '20241231', fp: 'FY', currency: 'CNY', total_assets: 500 }]
    )
    expect(sourceLabel(rows[0])).toBe('PDF 比较列+雅虎')
  })

  it('科目中文名覆盖全部数值科目', () => {
    for (const field of HK_NUMERIC_FIELDS) {
      expect(HK_FIELD_LABELS[field], field).toMatch(/[\u4e00-\u9fa5]|EBITDA/)
    }
    expect(localizeFieldNames('total_revenue 与雅虎相差 3.2%')).toBe('营业收入 与雅虎相差 3.2%')
    // 检查 id 里的字段名前缀不被误替换
    expect(localizeFieldNames('total_assets_positive')).toBe('total_assets_positive')
  })

  it('存疑已置空的单元格与缺数据区分；tooltip 用中文科目名', () => {
    const suspect = {
      ...pdf2025,
      validation: {
        status: 'suspect',
        row_level: false,
        suspect_fields: ['total_revenue'],
        checks: [
          {
            id: 'yahoo_total_revenue',
            severity: 'error',
            status: 'suspect',
            detail: 'total_revenue 与雅虎相差 12.0%'
          }
        ]
      }
    }
    const [row] = mergeHkPivotRows([suspect], [])
    expect(isScrubbedCell(row, 'total_revenue')).toBe(true)
    expect(isScrubbedCell(row, 'total_assets')).toBe(false) // 本来就缺 = 缺数据
    expect(suspectTooltipText(row)).toBe('已置空：营业收入。营业收入 与雅虎相差 12.0%')
    // 雅虎补回后不再是「已置空」
    const [refilled] = mergeHkPivotRows(
      [suspect],
      [{ end_date: '20251231', fp: 'FY', currency: 'CNY', total_revenue: 90 }]
    )
    expect(isScrubbedCell(refilled, 'total_revenue')).toBe(false)
    const [whole] = mergeHkPivotRows(
      [{ ...pdf2025, validation: { status: 'suspect', row_level: true, suspect_fields: [] } }],
      []
    )
    expect(suspectTooltipText(whole)).toBe('整行校验不通过，全部科目已置空')
  })

  it('会计期键转人话', () => {
    expect(formatStatementPeriodKey('20251231|FY')).toBe('2025 年报')
    expect(formatStatementPeriodKey('20250630|H1')).toBe('2025 中报')
    expect(formatStatementPeriodKey('20230630|FY')).toBe('2023-06 年报')
    expect(formatStatementPeriodKey('20251231|H1')).toBe('2025-12 中报')
    expect(formatStatementPeriodKey('garbage')).toBe('garbage')
  })

  it('币种一致才写进标题；与多数不一致（含未知）的行高亮', () => {
    const uniform = summarizePivotCurrency([{ currency: 'HKD' }, { currency: 'HKD' }])
    expect(uniform).toEqual({ majority: 'HKD', uniform: true })
    expect(isCurrencyOutlier({ currency: 'HKD' }, uniform)).toBe(false)
    const mixed = summarizePivotCurrency([
      { currency: 'CNY' },
      { currency: 'CNY' },
      { currency: 'HKD' },
      { currency: null }
    ])
    expect(mixed).toEqual({ majority: 'CNY', uniform: false })
    expect(isCurrencyOutlier({ currency: 'CNY' }, mixed)).toBe(false)
    expect(isCurrencyOutlier({ currency: 'HKD' }, mixed)).toBe(true)
    expect(isCurrencyOutlier({ currency: null }, mixed)).toBe(true)
    expect(summarizePivotCurrency([{ currency: 'CNY' }, {}]).uniform).toBe(false)
  })
})

describe('构建层标注（PR-A：EPS 折元 / 小计修复 / 已重列）', () => {
  const annotated = {
    ...pdf2025,
    basic_eps: 0.46,
    build_version: 1,
    eps_unit: { source_unit: 'cents', divisor: 100, basis: 'chain' },
    repaired_fields: {
      total_cur_assets: { from_row: 'r21', to_row: 'r23' },
      total_assets: { from_row: null, to_row: null }
    },
    restated_by_kind: { cashflow: true },
    mezzanine_equity: 17133208000,
    validation: {
      status: 'ok',
      suspect_fields: [],
      row_level: false,
      checks: [
        {
          id: 'comparative_n_cashflow_act',
          severity: 'info',
          status: 'suspect',
          reason: 'comparative_restated',
          fields: ['n_cashflow_act']
        },
        {
          id: 'yahoo_n_cashflow_act',
          severity: 'info',
          status: 'suspect',
          reason: 'yahoo_restated',
          fields: ['n_cashflow_act']
        },
        {
          id: 'yahoo_total_revenue',
          severity: 'info',
          status: 'suspect',
          reason: 'yahoo_definition_diff',
          fields: ['total_revenue']
        }
      ]
    }
  }

  it('标注是 info：不置空、不算存疑，元数据不当科目', () => {
    const [row] = mergeHkPivotRows([annotated], [])
    expect(row.__suspect).toBe(false)
    expect(row.__suspectFields).toEqual([])
    expect(row.n_cashflow_act).toBe(30)
    expect(row.__source.mezzanine_equity).toBe('pdf')
    expect(row.__source).not.toHaveProperty('eps_unit')
    expect(row.__source).not.toHaveProperty('repaired_fields')
    expect(row.__source).not.toHaveProperty('build_version')
    expect(row.__notes.restated).toEqual(['n_cashflow_act'])
    expect(row.__notes.yahooDefinitionDiff).toEqual(['total_revenue'])
    expect(row.__notes.epsCents).toEqual({ basis: 'chain' })
  })

  it('tooltip 文案：已重列保留原值、修复前后行号、EPS 折元依据', () => {
    const notes = buildNotes(annotated)
    const text = buildNotesText(notes)
    expect(text).toContain('已被后续报告重列：经营现金流（显示与分析均保留首次披露值）')
    expect(text).toContain('流动资产：行 r21 → 行 r23')
    expect(text).toContain('总资产：未映射 → 分项合计推导')
    expect(text).toContain('映射已修正：')
    expect(text).toContain('与雅虎口径不同：营业收入')
    expect(epsNoteText(notes)).toBe(
      '原文以「仙」列示，已 ÷100 折为元（依据：与相邻报告的比较列首尾相接）'
    )
    const plain = buildNotes(pdf2025)
    expect(buildNotesText(plain)).toBe('')
    expect(epsNoteText(plain)).toBe('')
  })

  it('EPS 附注号守卫（构建 v2）：改指基本行或弃用交雅虎', () => {
    const redirected = buildNotes({
      ...pdf2025,
      repaired_fields: {
        basic_eps: {
          reason: 'eps_note_number',
          from_row: 'r20',
          to_row: 'r21',
          note_number: '13',
          to_value: 5.363
        }
      }
    })
    expect(buildNotesText(redirected)).toBe('映射已修正：基本每股收益：行 r20（附注号 13）→ 行 r21')
    const dropped = buildNotes({
      ...pdf2025,
      repaired_fields: {
        basic_eps: { reason: 'eps_note_number', from_row: 'r33', to_row: null, note_number: '14' }
      }
    })
    expect(buildNotesText(dropped)).toContain('行 r33（附注号 14）→ 弃用（由雅虎补缺）')
  })

  it('中国准则利息费用（构建 v3）：净额財務費用改指其中利息费用', () => {
    const notes = buildNotes({
      ...pdf2025,
      repaired_fields: {
        int_exp: { reason: 'net_finance_cost', from_row: 'r9', to_row: 'r10', to_value: 141845408 }
      }
    })
    expect(buildNotesText(notes)).toContain('行 r9（財務費用净额）→ 行 r10（其中：利息费用）')
  })
})

describe('currencySwitchText（报告币种切换点）', () => {
  it('标出时间上紧邻更早一期币种不同的那一行，其余为空', () => {
    const rows = [
      { end_date: '20221231', fp: 'FY', currency: 'HKD' },
      { end_date: '20210630', fp: 'H1', currency: 'HKD' },
      { end_date: '20211231', fp: 'FY', currency: 'HKD' },
      { end_date: '20201231', fp: 'FY', currency: 'USD' },
      { end_date: '20191231', fp: 'FY', currency: 'USD' }
    ]
    expect(currencySwitchText(rows, rows[0])).toBe('')
    expect(currencySwitchText(rows, rows[2])).toBe('')
    // 含中报时切换后的第一期是 2021 中报（紧接 2020-12-31）
    expect(currencySwitchText(rows, rows[1])).toContain('由 USD 改为 HKD')
    expect(currencySwitchText(rows, rows[3])).toBe('')
    expect(currencySwitchText(rows, rows[4])).toBe('')
    // 只看年报时切换点落在 2021 年报
    const annual = rows.filter((row) => row.fp === 'FY')
    expect(currencySwitchText(annual, annual[1])).toContain('由 USD 改为 HKD')
  })

  it('币种未知不等于同币种：任一侧（含两侧）未知都标出；单币种序列全为空', () => {
    const rows = [
      { end_date: '20251231', currency: 'CNY' },
      { end_date: '20241231', currency: null },
      { end_date: '20231231', currency: 'HKD' }
    ]
    expect(currencySwitchText(rows, rows[0])).toContain('无法确认同币种')
    expect(currencySwitchText(rows, rows[0])).toContain('未知 → CNY')
    expect(currencySwitchText(rows, rows[1])).toContain('HKD → 未知')
    // 最早一期没有可比对象
    expect(currencySwitchText(rows, rows[2])).toBe('')
    const bothUnknown = [{ end_date: '20251231' }, { end_date: '20241231', currency: '' }]
    expect(currencySwitchText(bothUnknown, bothUnknown[0])).toContain('无法确认同币种')
    const uniform = [
      { end_date: '20251231', currency: 'HKD' },
      { end_date: '20241231', currency: 'HKD' }
    ]
    expect(uniform.map((row) => currencySwitchText(uniform, row))).toEqual(['', ''])
  })
})
