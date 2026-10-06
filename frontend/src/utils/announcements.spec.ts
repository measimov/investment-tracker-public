import { describe, expect, it } from 'vitest'
import type { AnnouncementGroup } from '@/types'
import {
  ANNOUNCEMENT_CATEGORIES,
  announcementBadge,
  groupsByDate,
  importanceLabel,
  importanceTagType,
  nextBeforeDate,
  safeAnnouncementUrl,
  sourceLabel,
  syncStatusText
} from './announcements'

function group(overrides: Partial<AnnouncementGroup> = {}): AnnouncementGroup {
  return {
    group_key: '600298|A股|2026-09-25|financing',
    symbol: '600298',
    market: 'A股',
    name: '安琪酵母',
    ann_date: '2026-09-25',
    category: 'financing',
    category_label: '融资',
    importance: 'major',
    title: '向不特定对象发行可转换公司债券预案',
    url: 'https://static.cninfo.com.cn/finalpage/x.PDF',
    source: 'cninfo',
    document_count: 6,
    first_seen_at: '2026-09-25T10:00:00Z',
    latest_published_at: '2026-09-25T10:00:00Z',
    documents: [],
    ...overrides
  }
}

describe('公告展示', () => {
  it('类别与后端 CATEGORIES 一一对应', () => {
    expect(ANNOUNCEMENT_CATEGORIES).toHaveLength(13)
    expect(ANNOUNCEMENT_CATEGORIES.find((c) => c.value === 'financing')?.label).toBe('融资')
  })

  it('重要性与来源文案', () => {
    expect(importanceLabel('major')).toBe('重要')
    expect(importanceLabel('minor')).toBe('例行')
    expect(importanceTagType('major')).toBe('danger')
    expect(importanceTagType('normal')).toBe('warning')
    expect(importanceTagType('minor')).toBe('info')
    expect(sourceLabel('hkexnews')).toBe('披露易')
    expect(sourceLabel('x')).toBe('x')
  })

  it('只放行 http(s) 链接', () => {
    expect(safeAnnouncementUrl(' https://www.sec.gov/a ')).toBe('https://www.sec.gov/a')
    expect(safeAnnouncementUrl('javascript:alert(1)')).toBeNull()
    expect(safeAnnouncementUrl('')).toBeNull()
    expect(safeAnnouncementUrl(null)).toBeNull()
  })

  it('同步状态：未同步与没有公告分得开', () => {
    expect(
      syncStatusText({ sync_status: 'pending', last_synced: null, unsupported_reason: null }).type
    ).toBe('warning')
    expect(
      syncStatusText({
        sync_status: 'unsupported',
        last_synced: null,
        unsupported_reason: 'ETF 无 orgId'
      }).text
    ).toContain('ETF 无 orgId')
    expect(
      syncStatusText({ sync_status: 'synced', last_synced: '2026-09-29', unsupported_reason: null })
        .text
    ).toContain('2026/09/29')
  })

  it('按公告日分段与翻页游标', () => {
    const groups = [
      group(),
      group({ group_key: 'b', category: 'governance' }),
      group({ group_key: 'c', ann_date: '2026-09-20' })
    ]
    const sections = groupsByDate(groups)
    expect(sections.map((s) => [s.date, s.groups.length])).toEqual([
      ['2026-09-25', 2],
      ['2026-09-20', 1]
    ])
    expect(nextBeforeDate(groups)).toBe('2026-09-20')
    expect(nextBeforeDate([])).toBeNull()
  })

  it('徽标：单组显示类别，多组显示条数，悬浮最多 5 行', () => {
    expect(announcementBadge(undefined)).toBeNull()
    expect(announcementBadge([])).toBeNull()
    const single = announcementBadge([group()])!
    expect(single.text).toBe('公告·融资')
    expect(single.lines).toEqual(['09-25 融资：向不特定对象发行可转换公司债券预案（6 份）'])
    const many = announcementBadge(
      Array.from({ length: 7 }, (_, i) => group({ group_key: String(i), document_count: 1 }))
    )!
    expect(many.text).toBe('公告·7')
    expect(many.lines).toHaveLength(6)
    expect(many.lines[5]).toBe('…另 2 组')
    expect(many.lines[0]).not.toContain('份')
  })
})
