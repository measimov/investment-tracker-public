import { describe, expect, it } from 'vitest'
import { OPINION_CHANGE_TAGS, opinionTagStyle, opinionTagType } from './opinionTags'

describe('opinionTagStyle', () => {
  it('多空不用红绿（与全站绿涨红跌冲突），改 primary/warning + 箭头', () => {
    for (const tag of ['一致看多', '偏多', '一致看空', '偏空', '近期转多', '近期转空']) {
      expect(['danger', 'success']).not.toContain(opinionTagType(tag))
    }
    expect(opinionTagStyle('偏多')).toMatchObject({ type: 'primary', label: '↑ 偏多' })
    expect(opinionTagStyle('一致看空')).toMatchObject({ type: 'warning', label: '↓ 一致看空' })
  })

  it('变化类标签先判断，实心突出，不被立场分支截走', () => {
    expect(opinionTagStyle('近期转多')).toEqual({
      type: 'primary',
      effect: 'dark',
      label: '↑ 近期转多',
      change: true
    })
    expect(opinionTagStyle('近期转空')).toMatchObject({ effect: 'dark', change: true })
    expect(opinionTagStyle('新增关注')).toMatchObject({ effect: 'dark', change: true })
    for (const tag of OPINION_CHANGE_TAGS) expect(opinionTagStyle(tag).change).toBe(true)
  })

  it('语境类与未知标签为中性', () => {
    expect(opinionTagStyle('多空分歧')).toEqual({
      type: 'info',
      effect: 'light',
      label: '多空分歧',
      change: false
    })
    expect(opinionTagType('观点数据不足')).toBe('info')
  })
})
