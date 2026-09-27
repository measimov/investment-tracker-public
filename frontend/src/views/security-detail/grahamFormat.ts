/**
 * 格雷厄姆准则卡的依据/补充口径展示串（纯函数，有 spec）。
 *
 * 判定口径是 TTM（后端 graham_screen）：basis 说明价格日期（陈价标注）、TTM 构成与汇率折算后的
 * 每股盈利；supplement 是年报静态 PE 与原著「近三年平均每股盈利」PE，只作参考。
 */
import { EMPTY, formatPrice } from '@/utils/helpers'
import { formatFixed, formatPeriod } from './format'
import type { GrahamBasis, GrahamSupplement } from './types'

function periodText(period: string): string {
  const [end, fp] = period.split('|')
  return `${formatPeriod(end)}${fp && fp !== 'FY' ? ` ${fp}` : ' 年报'}`
}

/** 价格 + 日期（陈价标注） */
export function grahamPriceText(basis: GrahamBasis | undefined): string {
  if (!basis || basis.price == null) return ''
  const currency = basis.price_currency ? ` ${basis.price_currency}` : ''
  const date = basis.price_date ? formatPeriod(basis.price_date) : EMPTY
  const stale = basis.price_stale ? `，陈价 ${basis.price_age_days ?? '?'} 天` : ''
  return `价格 ${formatPrice(basis.price)}${currency}（${date}${stale}）`
}

/** 依据行：TTM 构成 / MRQ 净资产 / 价格日期 / ADS 口径 / 备注 */
export function grahamBasisText(basis: GrahamBasis | undefined): string {
  if (!basis) return ''
  const parts: string[] = []
  if (basis.label) parts.push(basis.label)
  if (basis.components && basis.components.length > 1) {
    parts.push(
      '构成 ' +
        basis.components
          .map(
            (item) =>
              `${item.sign === '-' ? '−' : '+'}${periodText(item.period)} ` +
              `${formatFixed(item.eps, 2, 4)}${item.currency ? ` ${item.currency}` : ''}`
          )
          .join(' ')
    )
  }
  if (typeof basis.eps_ttm === 'number') {
    parts.push(
      `每股盈利 ${formatFixed(basis.eps_ttm, 2, 4)}${basis.price_currency ? ` ${basis.price_currency}` : ''}`
    )
  }
  if (typeof basis.bvps === 'number') {
    parts.push(
      `每股净资产 ${formatFixed(basis.bvps, 2, 4)}${basis.price_currency ? ` ${basis.price_currency}` : ''}`
    )
  }
  const price = grahamPriceText(basis)
  if (price) parts.push(price)
  if (basis.share_ratio_note) parts.push(basis.share_ratio_note)
  if (basis.note) parts.push(basis.note)
  return parts.join('；')
}

/** 补充口径行：「年报口径 PE x · 三年均值 PE y（仅供参考）」 */
export function grahamSupplementText(supplement: GrahamSupplement | undefined): string {
  if (!supplement) return ''
  const pe = (value: number | null | undefined) =>
    typeof value === 'number' ? formatFixed(value, 2) : EMPTY
  return `年报口径 PE ${pe(supplement.static_pe)} · 三年均值 PE ${pe(supplement.graham_avg3_pe)}（仅供参考）`
}

/** 补充口径的 tooltip：两项各自的每股盈利来源 */
export function grahamSupplementTitle(supplement: GrahamSupplement | undefined): string {
  if (!supplement) return ''
  return [supplement.static_basis, supplement.avg3_basis, supplement.basis]
    .filter(Boolean)
    .join('；')
}
