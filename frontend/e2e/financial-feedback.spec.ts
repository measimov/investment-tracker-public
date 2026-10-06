import { expect, test } from '@playwright/test'
import { loginThroughApi, setAuthenticatedSession } from './helpers'

const zeroSummary = {
  current_performance: {
    unrealized_pnl_cny: 0,
    current_holdings_cost_cny: 0,
    unrealized_pnl_rate: 0,
    current_market_value_cny: 0,
    holdings_detail: []
  },
  realized_pnl: { realized_pnl: 0, sold_cost: 0, realized_pnl_rate: 0, trades_detail: [] },
  dividend_summary: { total_dividend_gross: 0, total_tax: 0, total_dividend_net: 0, by_symbol: [] },
  total_realized_return: { total_realized_return: 0, total_realized_return_rate: 0 },
  account_return: { total_return: 0, total_return_rate: 0, annualized_return_rate: null }
}
const emptyAnalytics = {
  calculation_level: 'empty',
  curve: [],
  metrics: {},
  trade_skill: {},
  data_quality: { warnings: [] }
}

test('failed summary displays missing metrics and a visible error instead of zero', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await page.route('**/api/statistics/performance-summary', (route) =>
    route.fulfill({ status: 422, json: { detail: '摘要暂不可用' } })
  )
  await setAuthenticatedSession(page, token)
  await page.goto('/statistics')
  await expect(page.getByTestId('summary-error')).toContainText('摘要暂不可用')
  const total = page
    .locator('.financial-statistic')
    .filter({ hasText: /^总收益/ })
    .first()
  await expect(total.locator('.financial-statistic-value')).toHaveText('—')
  const dividend = page.locator('.financial-statistic').filter({ hasText: '累计实收净股息' })
  await expect(dividend.locator('.financial-statistic-value')).toHaveText('—')
})

test('real zero rates are unsigned and zero profits use neutral colors', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await page.route('**/api/statistics/performance-summary', (route) =>
    route.fulfill({ json: zeroSummary })
  )
  await setAuthenticatedSession(page, token)
  await page.goto('/statistics')
  const rate = page
    .locator('.financial-statistic')
    .filter({ hasText: /^总收益率/ })
    .first()
  await expect(rate.locator('.financial-statistic-value')).toHaveText('0.00%')
  const color = await rate
    .locator('.financial-statistic-value')
    .evaluate((node) => getComputedStyle(node).color)
  expect(color).not.toBe('rgb(5, 150, 105)')
  await expect(page.getByTestId('summary-error')).toHaveCount(0)
})

test('partial history sync reloads analytics and reports the useful partial result', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  let analyticsReads = 0
  await page.route('**/api/statistics/performance-analytics**', (route) => {
    analyticsReads += 1
    return route.fulfill({ json: emptyAnalytics })
  })
  await page.route('**/api/statistics/performance-summary', (route) =>
    route.fulfill({ json: zeroSummary })
  )
  await page.route('**/api/statistics/performance-history-sync', (route) =>
    route.fulfill({ json: { id: 'partial', status: 'queued' } })
  )
  await page.route('**/api/statistics/performance-history-sync/partial', (route) =>
    route.fulfill({
      json: {
        id: 'partial',
        status: 'failed',
        success_count: 2,
        failed_count: 1,
        progress_percent: 100
      }
    })
  )
  await setAuthenticatedSession(page, token)
  await page.goto('/statistics')
  await expect.poll(() => analyticsReads).toBe(1)
  await page.getByRole('button', { name: '同步历史行情' }).click()
  await expect.poll(() => analyticsReads).toBe(2)
  await expect(page.locator('.el-message--warning')).toContainText('成功2项，失败1项')
  await expect(page.getByText('历史行情同步部分完成', { exact: true })).toBeVisible()
})
