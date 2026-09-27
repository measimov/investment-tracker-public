/**
 * 标的详情页的展示格式化（#221）：纯函数、无组件依赖，有 spec。
 *
 * 数值精度按字段语义定（此前 formatMaybe 一律 4 位小数：股数 1,234,567.0000、
 * 毛利率 35.1234、质押比例 12.3400%）——股数 0 位、百分比 2 位、pp 1 位、比率 2 位、
 * EPS 2–3 位、每股分红 2–4 位。缺值统一 EMPTY。
 */
import { EMPTY } from '@/utils/helpers'

function toFinite(value: unknown): number | null {
  if (value == null || value === '') return null
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? n : null
}

export function formatFixed(value: unknown, minDigits: number, maxDigits = minDigits): string {
  const n = toFinite(value)
  if (n === null) return EMPTY
  return n.toLocaleString('zh-CN', {
    minimumFractionDigits: minDigits,
    maximumFractionDigits: maxDigits
  })
}

/** 比率（CFO/净利润、应计率、M-score、扣非占比）：2 位 */
export const formatRatio = (value: unknown) => formatFixed(value, 2)
/** 百分数（数值本身已是 % 口径，如 ROE 12.34）：2 位，不带 % 号（列名里有） */
export const formatPct = (value: unknown) => formatFixed(value, 2)
/** 百分点差：1 位 */
export const formatPp = (value: unknown) => formatFixed(value, 1)
/** 每股收益：2–3 位 */
export const formatEps = (value: unknown) => formatFixed(value, 2, 3)
/** 每股分红/送转：2–4 位（0.0512 这类小额分红要看得到有效位） */
export const formatPerShare = (value: unknown) => formatFixed(value, 2, 4)
/** 股数/笔数：整数 */
export const formatCount = (value: unknown) => formatFixed(value, 0)

/** 元 → 亿（2 位） */
export function formatYi(value: unknown): string {
  const n = toFinite(value)
  return n === null ? EMPTY : formatFixed(n / 1e8, 2)
}

/** 股 → 万股（2 位）。Tushare share_float.float_share 单位是「股」（官方文档：流通股份(股)） */
export function formatWanShares(value: unknown): string {
  const n = toFinite(value)
  return n === null ? EMPTY : formatFixed(n / 1e4, 2)
}

/** YYYYMMDD → YYYY-MM-DD；其他原样，空值 EMPTY */
export function formatPeriod(value: unknown): string {
  const text = String(value ?? '')
  if (/^\d{8}$/.test(text)) return `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)}`
  return text || EMPTY
}

export type PeriodKind = 'Q1' | 'H1' | 'Q3' | 'FY'

/**
 * A 股报告期期别：由报告期末日推断（Tushare 的 Q1/H1/Q3 是**年初至今累计值**，与年报混排时
 * 必须标明，否则 Q3 的 9 个月收入看起来像「同比大降」）。非季末日返回 null。
 */
export function periodKind(endDate: unknown): PeriodKind | null {
  const text = String(endDate ?? '')
  if (!/^\d{8}$/.test(text)) return null
  const mmdd = text.slice(4)
  if (mmdd === '0331') return 'Q1'
  if (mmdd === '0630') return 'H1'
  if (mmdd === '0930') return 'Q3'
  if (mmdd === '1231') return 'FY'
  return null
}

const PERIOD_KIND_LABELS: Record<PeriodKind, string> = {
  Q1: '一季报',
  H1: '中报',
  Q3: '三季报',
  FY: '年报'
}

export function periodKindLabel(endDate: unknown): string {
  const kind = periodKind(endDate)
  return kind ? PERIOD_KIND_LABELS[kind] : EMPTY
}

/** 「只看年报」过滤：保留期末 12-31 的行 */
export function annualOnly<T extends Record<string, unknown>>(rows: T[]): T[] {
  return rows.filter((row) => periodKind(row.end_date) === 'FY')
}

const DIGEST_TYPE_LABELS: Record<string, string> = {
  annual: '年报',
  semi: '中报',
  interim: '中报',
  '10-K': '10-K',
  '20-F': '20-F'
}

export function digestTypeLabel(type: unknown): string {
  const key = String(type ?? '')
  return DIGEST_TYPE_LABELS[key] || key || EMPTY
}

/**
 * 财报摘要「查看原文」链接：A 股/港股是 PDF 直链字符串；美股 EDGAR 存的是
 * {cik, accession, document}，拼成 SEC Archives 地址（与后端 edgar_download_filing 同构）。
 * 其他形状返回 null（不渲染链接）。
 */
export function digestSourceHref(source: unknown): string | null {
  if (typeof source === 'string') return /^https?:\/\//i.test(source) ? source : null
  if (!source || typeof source !== 'object') return null
  const { cik, accession, document } = source as Record<string, unknown>
  if (cik == null || !accession || !document) return null
  const cikText = String(cik).replace(/^0+/, '') || '0'
  if (!/^\d+$/.test(cikText)) return null
  const accessionNoDash = String(accession).replace(/-/g, '')
  if (!/^\d+$/.test(accessionNoDash)) return null
  return `https://www.sec.gov/Archives/edgar/data/${cikText}/${accessionNoDash}/${encodeURIComponent(String(document))}`
}

/** 整天数差（本地时区自然日），用于「N 天前」 */
export function daysAgo(iso: string | null | undefined, now: Date = new Date()): number | null {
  if (!iso) return null
  const then = new Date(iso)
  if (Number.isNaN(then.getTime())) return null
  const start = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
  return Math.max(0, Math.round((start(now) - start(then)) / 86_400_000))
}

export function daysAgoText(iso: string | null | undefined, now: Date = new Date()): string {
  const days = daysAgo(iso, now)
  if (days === null) return ''
  if (days === 0) return '今天'
  if (days === 1) return '昨天'
  return `${days} 天前`
}

/**
 * 分析是否「可能过期」：分析生成时间早于最新的摘要生成/报表抽取时间（后端 latest_data_at）。
 * 分析 job 自身会先补摘要再调 LLM，所以同一次任务产出的摘要时间必然早于分析——不会误报。
 */
export function isAnalysisOutdated(
  analysisCreatedAt: string | null | undefined,
  latestDataAt: string | null | undefined
): boolean {
  if (!analysisCreatedAt || !latestDataAt) return false
  const created = new Date(analysisCreatedAt).getTime()
  const latest = new Date(latestDataAt).getTime()
  if (!Number.isFinite(created) || !Number.isFinite(latest)) return false
  return latest > created
}

/**
 * 分红历史「最新公告」：取**已实施**行的最大 ann_date（此前取数据集最大 period_key，
 * 最新一期可能是预案行，与表格只列实施行对不上）。
 */
export function latestImplementedDividendAnnDate(
  rows: Array<Record<string, unknown>>
): string | null {
  let latest: string | null = null
  for (const row of rows || []) {
    if (row.div_proc !== '实施') continue
    const ann = String(row.ann_date ?? '')
    if (/^\d{8}$/.test(ann) && (latest === null || ann > latest)) latest = ann
  }
  return latest
}
