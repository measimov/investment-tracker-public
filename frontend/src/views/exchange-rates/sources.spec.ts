import { describe, expect, it } from 'vitest'
import { isDiffAbnormal, sourceLabel, sourceTagType } from './sources'

describe('汇率来源展示', () => {
  it('官方中间价与第三方有中文标签，未知来源原样显示', () => {
    expect(sourceLabel('cfets-ccpr')).toBe('官方中间价')
    expect(sourceLabel('api-ecb')).toBe('欧洲央行参考价')
    expect(sourceLabel('custom')).toBe('custom')
    expect(sourceLabel(null)).toBe('—')
  })

  it('第三方成为生效汇率时用醒目色提示降级', () => {
    expect(sourceTagType('cfets-ccpr')).toBe('success')
    expect(sourceTagType('api-backup')).toBe('danger')
    expect(sourceTagType('manual')).toBe('warning')
    expect(sourceTagType(undefined)).toBe('info')
  })

  it('比对差异超过 2%（中间价波动区间）才视为异常，字符串数值同样处理', () => {
    expect(isDiffAbnormal('0.1234')).toBe(false)
    expect(isDiffAbnormal(-0.53)).toBe(false)
    expect(isDiffAbnormal(-2.01)).toBe(true)
    expect(isDiffAbnormal(null)).toBe(false)
  })
})
