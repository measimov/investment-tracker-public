import { expect, test } from '@playwright/test'
import { loginThroughApi, setAuthenticatedSession, mockHoldingsPage } from './helpers'

const ANNOUNCEMENT_GROUP = {
  group_key: 'BAT001|A股|2026-09-25|financing',
  symbol: 'BAT001',
  market: 'A股',
  name: '批量测试A',
  ann_date: '2026-09-25',
  category: 'financing',
  category_label: '融资',
  importance: 'major',
  title: '向不特定对象发行可转换公司债券预案',
  url: 'https://static.cninfo.com.cn/finalpage/2026-09-25/1.PDF',
  source: 'cninfo',
  document_count: 2,
  first_seen_at: '2026-09-25T10:00:00Z',
  latest_published_at: '2026-09-25T10:00:00Z',
  documents: [
    {
      title: '向不特定对象发行可转换公司债券预案',
      url: 'https://static.cninfo.com.cn/finalpage/2026-09-25/1.PDF',
      published_at: '2026-09-25T10:00:00Z',
      importance: 'major',
      category: 'financing'
    },
    {
      title: '可转换公司债券持有人会议规则',
      url: 'javascript:alert(1)',
      published_at: '2026-09-25T09:00:00Z',
      importance: 'normal',
      category: 'financing'
    }
  ]
}

test('holdings show a badge for recent major announcements', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await mockHoldingsPage(page)
  await page.route('**/api/announcements/recent*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ days: 7, importance: 'major', groups: [ANNOUNCEMENT_GROUP] })
    })
  )

  // 同一标的同时有未来事件徽标：两个徽标 + 四字名称曾挤在 200px 一行里，名称被压成一字一行、
  // 公告徽标被截断
  const eventDate = new Date(Date.now() + 31 * 86400000).toISOString().slice(0, 10)
  await page.route('**/api/corporate-actions/security-events*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        {
          id: 1,
          symbol: 'BAT001',
          market: 'A股',
          event_type: 'EARNINGS_DISCLOSURE',
          event_date: eventDate,
          payload: null,
          source: 'tushare'
        }
      ])
    })
  )

  await page.goto('/holdings')
  const badges = page.getByTestId('announcement-badge')
  await expect(badges).toHaveCount(1)
  await expect(badges.first()).toContainText('公告·融资')

  // 布局：名称单行，两个徽标完整落在单元格内
  const row = page.locator('[data-testid=holding-row]', { hasText: 'BAT001' }).first()
  const name = row.locator('.holding-name')
  const cell = row.locator('td', { has: page.locator('.holding-name') })
  const nameBox = (await name.boundingBox())!
  const lineHeight = await name.evaluate((el) => parseFloat(getComputedStyle(el).fontSize) * 1.8)
  expect(nameBox.height).toBeLessThan(lineHeight)
  const cellBox = (await cell.boundingBox())!
  for (const testId of ['security-event-badge', 'announcement-badge']) {
    const box = (await row.getByTestId(testId).boundingBox())!
    expect(box.x + box.width).toBeLessThanOrEqual(cellBox.x + cellBox.width + 0.5)
  }
})

test('security detail announcements tab lists grouped filings', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await page.route('**/api/securities/**/profile', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        symbol: 'BAT001',
        market: 'A股',
        supported: true,
        capabilities: { structured: true, report_digest: true, risk_signals: true },
        datasets: {},
        latest_periods: {},
        events: [],
        report_digests: [],
        digest_progress: { digested: 0, failed_capped: 0 },
        statement_progress: null,
        business: { profile: null, peers: [], industry: null },
        earnings_quality: { status: 'no_data' },
        graham_screen: { status: 'no_data' }
      })
    })
  )
  await page.route('**/api/securities/**/analysis', (route) =>
    route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"暂无分析"}' })
  )
  const requested: string[] = []
  await page.route('**/api/securities/**/announcements*', (route) => {
    requested.push(route.request().url())
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        symbol: 'BAT001',
        market: 'A股',
        sync_status: 'synced',
        last_synced: '2026-09-29',
        unsupported_reason: null,
        groups: [ANNOUNCEMENT_GROUP],
        has_more: false
      })
    })
  })

  await page.goto('/securities/A股/BAT001')
  await page.getByRole('tab', { name: '公告' }).click()
  const section = page.getByTestId('announcements-section')
  await expect(section.getByTestId('announcement-group')).toHaveCount(1)
  await expect(section).toContainText('已同步至 2026/09/29')
  const title = section.getByRole('link', { name: '向不特定对象发行可转换公司债券预案' })
  await expect(title).toHaveAttribute('href', ANNOUNCEMENT_GROUP.url)

  await section.getByTestId('announcement-toggle').click()
  await expect(section).toContainText('可转换公司债券持有人会议规则')
  // 非 http(s) 链接只显示文字、不渲染成链接
  await expect(section.getByRole('link', { name: '可转换公司债券持有人会议规则' })).toHaveCount(0)

  await section
    .getByRole('group', { name: '公告重要程度' })
    .getByRole('button', { name: '全部', exact: true })
    .click()
  await expect.poll(() => requested.some((url) => url.includes('importance=all'))).toBe(true)
})
