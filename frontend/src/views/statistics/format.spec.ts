import { describe, expect, it } from 'vitest'
import { EMPTY } from '@/utils/helpers'
import {
  formatAmountTick,
  formatNullableNumber,
  formatNullablePercent,
  formatPlainPercent,
  isShortRange,
  rangeSpanDays,
  signedNumber,
  riskFreeText
} from './format'

describe('百分比正号规则', () => {
  it('有方向的百分比带正号，与 formatPercent 一致', () => {
    expect(formatNullablePercent(12.345)).toBe('+12.35%')
    expect(formatNullablePercent(-5)).toBe('-5.00%')
    expect(formatNullablePercent(null)).toBe(EMPTY)
  })

  it('无方向的比例不带正号', () => {
    expect(formatPlainPercent(55.5)).toBe('55.50%')
    expect(formatPlainPercent(undefined)).toBe(EMPTY)
  })

  it('el-statistic formatter：正数带 +、缺值占位', () => {
    expect(signedNumber(20)).toBe('+20.00')
    expect(signedNumber(-3.2)).toBe('-3.20')
    expect(signedNumber(1234.5)).toBe('+1,234.50')
    expect(signedNumber(null)).toBe(EMPTY)
    expect(signedNumber(0)).toBe('0.00')
    expect(signedNumber(-0.004)).toBe('0.00')
  })

  it('formatNullableNumber 缺值占位', () => {
    expect(formatNullableNumber(1.234)).toBe('1.23')
    expect(formatNullableNumber(null)).toBe(EMPTY)
  })
})

describe('短区间年化标注', () => {
  it('rangeSpanDays 按本地日期计天数', () => {
    expect(rangeSpanDays('2026-08-26', '2026-09-26')).toBe(31)
    expect(rangeSpanDays(null, '2026-09-26')).toBeNull()
  })

  it('不足 180 天为短区间', () => {
    expect(isShortRange(31)).toBe(true)
    expect(isShortRange(179)).toBe(true)
    expect(isShortRange(180)).toBe(false)
    expect(isShortRange(null)).toBe(false)
  })
})

describe('riskFreeText（#200 无风险利率口径说明）', () => {
  it('序列口径写明来源与区间均值，开头缺值如实说明', () => {
    expect(
      riskFreeText({ basis: 'series', label: 'SHIBOR 3M', series: 'SHIBOR_3M', average: 1.4321 })
    ).toBe('无风险利率按 SHIBOR 3M 日序列逐期扣除，区间均值 1.43%')
    expect(
      riskFreeText({ basis: 'series', label: 'SHIBOR 3M', average: 1.5, missing_points: 3 })
    ).toContain('区间开头 3 个观测点早于序列首值，按 0 计')
  })

  it('无数据时用后端 note，缺块时回落到按 0 的说明', () => {
    expect(
      riskFreeText({ basis: 'none', note: 'SHIBOR 3M 暂无数据（参考利率尚未同步），按 0 计算' })
    ).toBe('SHIBOR 3M 暂无数据（参考利率尚未同步），按 0 计算')
    expect(riskFreeText(undefined)).toBe('无风险利率按 0 计算（未扣除存款/国债收益）')
    expect(riskFreeText({ basis: 'constant', average: 2 })).toBe(
      '无风险利率按请求指定的年化 2.00% 计算'
    )
  })
})

describe('交易图表金额刻度', () => {
  it('保留正负号与量级，金额明细不受刻度缩写影响', () => {
    expect(formatAmountTick(0)).toBe('0')
    expect(formatAmountTick(1500)).toBe('1,500')
    expect(formatAmountTick(500000)).toBe('50万')
    expect(formatAmountTick(-125000)).toBe('-12.5万')
    expect(formatAmountTick(100000000)).toBe('1亿')
  })
})
