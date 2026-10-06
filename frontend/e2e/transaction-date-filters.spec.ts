import { expect, test, type Page } from '@playwright/test'
import {
  createTemporaryUser,
  deleteTemporaryUser,
  loginThroughApi,
  setAuthenticatedSession
} from './helpers'

async function setRange(page: Page, start: string, end: string) {
  await page.getByRole('button', { name: '选择交易日期范围' }).click()
  await page.getByRole('textbox', { name: '开始日期', exact: true }).fill(start)
  await page.getByRole('textbox', { name: '结束日期', exact: true }).fill(end)
  await page.getByRole('button', { name: '应用范围', exact: true }).click()
}

test('date range filters list and count together, resets pagination and keeps date caches separate', async ({
  page,
  request
}) => {
  const temporary = await createTemporaryUser(request)
  try {
    const token = await loginThroughApi(request, {
      username: temporary.createdUser.username,
      password: temporary.password
    })
    const headers = { Authorization: `Bearer ${token}` }
    for (let day = 1; day <= 60; day++) {
      const date = new Date(2026, 0, day)
      const transactionDate = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
      const response = await request.post('http://127.0.0.1:18000/api/transactions', {
        headers,
        data: {
          symbol: 'DATEFILTER',
          name: '日期筛选演示',
          market: '美股',
          transaction_type: 'BUY',
          quantity: 1,
          price: 1,
          fee: 0,
          currency: 'USD',
          transaction_date: transactionDate
        }
      })
      expect(response.status()).toBe(201)
    }
    await setAuthenticatedSession(page, token, temporary.createdUser)
    const calls: URL[] = []
    page.on('request', (request) => {
      const url = new URL(request.url())
      if (['/api/transactions', '/api/transactions/count'].includes(url.pathname)) calls.push(url)
    })
    await page.goto('/transactions')
    await expect(page.locator('.pagination-bar .pagination-info')).toHaveText('共 60 条')
    await page
      .locator('.pagination-bar .el-pagination')
      .getByRole('button', { name: '下一页' })
      .click()
    await expect(page.locator('.el-pagination .number.is-active')).toHaveText('2')
    await expect(page.locator('.n-data-table-tbody tr')).toHaveCount(10)

    await setRange(page, '2026-01-01', '2026-02-28')
    await expect(page.locator('.pagination-bar .pagination-info')).toHaveText('共 59 条')
    await expect(page.locator('.el-pagination .number.is-active')).toHaveText('1')
    await page
      .locator('.pagination-bar .el-pagination')
      .getByRole('button', { name: '下一页' })
      .click()
    await expect(page.locator('.n-data-table-tbody tr')).toHaveCount(9)
    await expect(page.locator('.n-data-table-tbody tr').last()).toContainText('2026/01/01')

    await setRange(page, '2026-01-02', '2026-03-01')
    await expect(page.locator('.el-pagination .number.is-active')).toHaveText('1')
    await expect(page.locator('.n-data-table-tbody tr').first()).toContainText('2026/03/01')
    await page
      .locator('.pagination-bar .el-pagination')
      .getByRole('button', { name: '下一页' })
      .click()
    await expect(page.locator('.n-data-table-tbody tr')).toHaveCount(9)
    await expect(page.locator('.n-data-table-tbody tr').last()).toContainText('2026/01/02')
    for (const [start, end] of [
      ['2026-01-01', '2026-02-28'],
      ['2026-01-02', '2026-03-01']
    ]) {
      const matching = calls.filter(
        (url) =>
          url.searchParams.get('start_date') === start && url.searchParams.get('end_date') === end
      )
      expect(matching.some((url) => url.pathname === '/api/transactions')).toBeTruthy()
      expect(matching.some((url) => url.pathname === '/api/transactions/count')).toBeTruthy()
    }
    await setRange(page, '2026-01-03', '2026-01-03')
    await expect(page.locator('.pagination-bar .pagination-info')).toHaveText('共 1 条')
    await expect(page.locator('.n-data-table-tbody tr')).toHaveCount(1)
    await page.getByRole('button', { name: '重置', exact: true }).click()
    await expect(page.locator('.pagination-bar .pagination-info')).toHaveText('共 60 条')
    await expect(page.getByRole('button', { name: '选择交易日期范围' })).toHaveText('选择日期范围')
    expect(calls.at(-1)?.searchParams.has('start_date')).toBe(false)
  } finally {
    await deleteTemporaryUser(request, temporary.adminToken, temporary.createdUser.id)
  }
})

test('date filter remains usable in narrow screens and is counted as one mobile filter', async ({
  page,
  request
}) => {
  await setAuthenticatedSession(page, await loginThroughApi(request))
  for (const width of [320, 375, 393, 720, 1440]) {
    const height = width === 720 ? 450 : 852
    await page.setViewportSize({ width, height })
    await page.goto('/transactions')
    if (width <= 640) await page.getByRole('button', { name: '筛选', exact: true }).click()
    const trigger = page.getByRole('button', { name: '选择交易日期范围' })
    await trigger.focus()
    await trigger.press('Enter')
    const start = page.getByRole('textbox', { name: '开始日期', exact: true })
    const popper = page.getByRole('dialog', { name: '交易日期范围' })
    await expect(popper).toBeVisible()
    await expect
      .poll(async () => {
        const rect = await popper.boundingBox()
        return (
          !!rect &&
          rect.x >= 0 &&
          rect.x + rect.width <= width &&
          rect.y >= 0 &&
          rect.y + rect.height <= height
        )
      })
      .toBe(true)
    await page.screenshot({ path: test.info().outputPath(`date-open-${width}.png`) })
    await page.keyboard.press('Escape')
    await expect(popper).toBeHidden()
    await expect(trigger).toBeFocused()
    await setRange(page, '2026-01-03', '2026-01-03')
    if (width <= 640)
      await expect(page.getByRole('button', { name: '收起筛选（1）' })).toBeVisible()
    if (width === 393) {
      await trigger.press('Space')
      await expect(popper).toBeVisible()
      const calendar = popper.locator(
        '.n-date-panel-calendar--start .n-date-panel-date:not(.n-date-panel-date--excluded)'
      )
      await calendar.getByText('4', { exact: true }).click()
      await calendar.getByText('5', { exact: true }).click()
      await expect(start).toHaveValue('2026-01-04')
      await expect(page.getByPlaceholder('结束日期', { exact: true })).toHaveValue('2026-01-05')
      await page.getByRole('button', { name: '应用范围', exact: true }).click()
      await expect(popper).toBeHidden()
    }
    await expect
      .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
      .toBe(true)
    await page.screenshot({
      path: test.info().outputPath(`date-filter-${width}.png`),
      fullPage: true
    })
  }
})
