import { describe, expect, it } from 'vitest'
import {
  SYMBOL_FEED_STALE_HOURS,
  feedFreshnessHint,
  isSafeExternalUrl,
  postExcerpt,
  postHeadline,
  postTimeMs,
  safeLinks,
  truncateText
} from './xueqiuFeed'

describe('truncateText', () => {
  it('折叠空白并按长度截断', () => {
    expect(truncateText('  a \n b  ', 10)).toBe('a b')
    expect(truncateText('一二三四五六', 4)).toBe('一二三…')
    expect(truncateText(null, 4)).toBe('')
  })
})

describe('postHeadline / postExcerpt', () => {
  it('有标题：主行用标题、摘要用正文', () => {
    const post = { title: '2026年半年度报告', text: '贵州茅台：贵州茅台2026年半年度报告 网页链接' }
    expect(postHeadline(post)).toBe('2026年半年度报告')
    expect(postExcerpt(post)).toBe('贵州茅台：贵州茅台2026年半年度报告 网页链接')
  })

  it('无标题：主行取正文开头，短正文不重复出摘要', () => {
    const short = { title: '', text: '茅台机场茅台专卖店停业。' }
    expect(postHeadline(short)).toBe('茅台机场茅台专卖店停业。')
    expect(postExcerpt(short)).toBe('')

    const long = { title: '', text: '长'.repeat(100) }
    expect(postHeadline(long)).toHaveLength(60)
    expect(postExcerpt(long)).toBe('长'.repeat(100))
  })

  it('空帖给占位', () => {
    expect(postHeadline({ title: '', text: '' })).toBe('（无正文）')
  })
})

describe('isSafeExternalUrl', () => {
  it('只放行 http(s)', () => {
    expect(isSafeExternalUrl('https://xueqiu.com/1/2')).toBe(true)
    expect(isSafeExternalUrl('http://static.cninfo.com.cn/a.PDF')).toBe(true)
    expect(isSafeExternalUrl('javascript:alert(1)')).toBe(false)
    expect(isSafeExternalUrl('')).toBe(false)
    expect(isSafeExternalUrl(undefined)).toBe(false)
  })
})

describe('safeLinks', () => {
  it('缺字段按空，剔除非 http(s)', () => {
    expect(safeLinks({})).toEqual([])
    expect(safeLinks({ links: ['javascript:void(0)', 'https://a.example/x.pdf'] })).toEqual([
      'https://a.example/x.pdf'
    ])
  })
})

describe('postTimeMs', () => {
  it('0 视为缺失', () => {
    expect(postTimeMs({ created_at_ms: 0 })).toBeNull()
    expect(postTimeMs({ created_at_ms: 1790465005000 })).toBe(1790465005000)
  })
})

describe('feedFreshnessHint', () => {
  const now = Date.parse('2026-09-27T12:00:00Z')

  it('从未运行给提示', () => {
    expect(feedFreshnessHint(null, now)).toContain('尚未运行')
  })

  it('阈值内不提示，超过阈值按天提示', () => {
    const recent = new Date(now - (SYMBOL_FEED_STALE_HOURS - 1) * 3_600_000).toISOString()
    expect(feedFreshnessHint(recent, now)).toBe('')
    const old = new Date(now - 3 * 24 * 3_600_000).toISOString()
    expect(feedFreshnessHint(old, now)).toBe('按标的采集已 3 天未更新，采集器可能已停摆')
  })

  it('无法解析的时间不提示', () => {
    expect(feedFreshnessHint('not-a-date', now)).toBe('')
  })
})
