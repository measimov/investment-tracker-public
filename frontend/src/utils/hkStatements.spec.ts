import { describe, expect, it } from 'vitest'
import { mergeHkPivotRows, sourceLabel, suspectFieldsToScrub } from './hkStatements'

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
