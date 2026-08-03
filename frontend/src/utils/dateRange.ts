// 区间预设的日期运算：纯函数、注入"今天"，便于测试。
//
// 两个刻意回避的坑：
// - toISOString() 输出 UTC 日期，正时区（如 Asia/Shanghai）的 00:00–08:00
//   会把"今天"变成前一天 —— 必须用本地日期格式化。
// - setMonth() 在月末会溢出（7月31日 减 1 月得到 7月1日）—— 月份运算需
//   把日号钳制到目标月的最后一天（7月31日 → 6月30日）。

export function formatLocalDate(date: Date): string {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

export function monthsBefore(base: Date, months: number): Date {
  const target = new Date(base.getFullYear(), base.getMonth() - months, 1)
  const lastDayOfTargetMonth = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate()
  target.setDate(Math.min(base.getDate(), lastDayOfTargetMonth))
  return target
}

const PRESET_MONTHS: Record<string, number> = { '1m': 1, '3m': 3, '6m': 6, '1y': 12 }

export interface DateRangeParams {
  start_date: string
  end_date: string
}

// preset: '1m' | '3m' | '6m' | '1y' | 'ytd' → { start_date, end_date }
// 其他值（'all'/'custom'）不属于本函数职责，返回 null。
export function presetRangeParams(preset: string, now: Date = new Date()): DateRangeParams | null {
  if (preset === 'ytd') {
    return {
      start_date: formatLocalDate(new Date(now.getFullYear(), 0, 1)),
      end_date: formatLocalDate(now)
    }
  }
  const months = PRESET_MONTHS[preset]
  if (!months) return null
  return {
    start_date: formatLocalDate(monthsBefore(now, months)),
    end_date: formatLocalDate(now)
  }
}
