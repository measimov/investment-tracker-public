import { expect, test } from '@playwright/test'
import { loginThroughApi, setAuthenticatedSession, mockHoldingsPage } from './helpers'

test('没有除权日的近期分红预案显示已公告，并明确日期含义', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await mockHoldingsPage(page)
  const yesterday = new Date()
  yesterday.setDate(yesterday.getDate() - 1)
  const date = `${yesterday.getFullYear()}-${String(yesterday.getMonth() + 1).padStart(2, '0')}-${String(yesterday.getDate()).padStart(2, '0')}`
  await page.route('**/api/corporate-actions/security-events*', (route) => {
    expect(new URL(route.request().url()).searchParams.get('days_back')).toBe('7')
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        {
          id: 1,
          symbol: 'BAT001',
          market: 'A股',
          event_type: 'DIVIDEND_PLAN',
          event_date: date,
          payload: { date_basis: 'announcement', div_proc: '预案', ex_date: null },
          source: 'tushare-dividend'
        }
      ])
    })
  })
  await page.goto('/holdings')
  const badge = page.getByTestId('security-event-badge')
  await expect(badge).toHaveText('分红预案·已公告')
  await badge.click()
  await expect(
    page.getByText(`${date.replace(/-/g, '/')} 分红预案（公告日，除权除息日未公布）`, {
      exact: true
    })
  ).toBeVisible()
})
