/**
 * 券商导入「疑似重复」清单的展示口径（招商 #190 / IBKR）。
 *
 * 两家共用同一张可勾选表格与同一套确认流程（confirm_suspected_row_hashes），
 * 区别只在于配对依据：招商是同一笔成交的价格精度漂移；IBKR 还会把股息/预扣税
 * 与分红公告建议入账的股息配对（币种不同、按日期窗口），以及把成交与手工录入/
 * trade_history.xlsx 导入的交易配对。文案按导入模式切换，逻辑放这里便于单测。
 */
import type { SuspectedDuplicateSample } from '@/types'
import { ACTION_TYPE_LABELS, transactionTypeLabel } from '@/utils/labels'

export type SuspectedImportMode = 'cmb' | 'ibkr'

/** 支持疑似重复确认流程的导入模式 */
export function supportsSuspectedConfirm(mode: string): mode is SuspectedImportMode {
  return mode === 'cmb' || mode === 'ibkr'
}

/** 行类型文案：成交用交易类型，股息/预扣税单独命名 */
export function suspectedRowTypeLabel(type: string): string {
  if (type === 'DIVIDEND_TAX') return '预扣税'
  return ACTION_TYPE_LABELS[type] ?? transactionTypeLabel(type)
}

export function suspectedAlertTitle(
  mode: SuspectedImportMode,
  heldCount: number,
  totalCount: number
): string {
  if (heldCount <= 0) {
    return `${totalCount} 条此前归档的疑似重复流水仍待确认（本次按重复跳过）`
  }
  if (mode === 'ibkr') {
    return `${heldCount} 条流水疑似与账本已有记录重复（同日同向同数量的交易，或日期窗口内已入账的同标的股息/预扣税），本次不入账，待人工确认`
  }
  return `${heldCount} 条成交疑似与已入账流水重复（同日/同标的/同数量/同金额，成交价精度不同），本次不入账，待人工确认`
}

export function suspectedAlertDescription(mode: SuspectedImportMode): string {
  if (mode === 'ibkr') {
    return (
      '同一笔成交可能已由 trade_history.xlsx 导入或手工录入（流水指纹不同）；同一笔股息可能已从分红公告建议入账（港币、真实除净日/派息日），' +
      '而 IBKR 报表按美元在到账日记账。勾选确认为「真实的另一笔」后重新预览，再导入即入账（确认股息时其同日预扣税一并入账）；不勾选则保持归档不入账。'
    )
  }
  return '券商新旧导出的成交价小数位不同时，同一笔成交会算出不同的流水指纹。勾选确认为「真实的另一笔成交」后重新预览，再导入即入账；不勾选则保持归档不入账。'
}

/** 已入账一侧的简述：优先来源说明（IBKR），否则文件名 + 行号（招商） */
export function suspectedExistingSource(row: SuspectedDuplicateSample): string {
  if (row.existing_source) return row.existing_source
  if (row.existing_source_filename) {
    return row.existing_row_number
      ? `${row.existing_source_filename} 第 ${row.existing_row_number} 行`
      : row.existing_source_filename
  }
  return '—'
}

/** 是否为股息/预扣税行（不显示成交价列，改显示金额与币种） */
export function isSuspectedCashRow(row: SuspectedDuplicateSample): boolean {
  return row.transaction_type === 'CASH_DIVIDEND' || row.transaction_type === 'DIVIDEND_TAX'
}
