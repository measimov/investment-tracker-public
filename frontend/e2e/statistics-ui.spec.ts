import { expect, test } from '@playwright/test'
import { loginThroughApi, setAuthenticatedSession } from './helpers'
const analyticsData = {
  calculation_level: 'full',
  date_range: { start_date: '2026-01-02', end_date: '2026-02-03' },
  curve: [
    { date: '2026-01-02', cumulative_return_rate: 0, drawdown_rate: 0 },
    { date: '2026-02-03', cumulative_return_rate: 10, drawdown_rate: 0 }
  ],
  metrics: { annualized_return_rate: 10, max_drawdown_rate: 0 },
  trade_skill: { sample_count: 0 },
  range_summary: { realized_pnl_cny: 0, dividend_net_cny: 0, xirr_annualized_rate: 0 },
  data_quality: { warnings: [] }
}
test('failed statistics are unknown and failed range changes keep the successful curve identity', async ({
  page,
  request
}) => {
  let failing = true
  const token = await loginThroughApi(request)
  await page.route('**/api/statistics/performance-analytics**', (route) =>
    route.fulfill(
      failing ? { status: 503, json: { detail: '演示曲线暂不可用' } } : { json: analyticsData }
    )
  )
  for (const path of ['by-market', 'by-time', 'holdings-cost-breakdown'])
    await page.route(`**/api/statistics/${path}*`, (route) =>
      route.fulfill({ status: 503, json: { detail: '演示分布暂不可用' } })
    )
  await setAuthenticatedSession(page, token)
  await page.goto('/statistics')
  await expect(page.getByTestId('analytics-error')).toContainText('平仓样本与曲线未知')
  await expect(page.getByTestId('range-ttwr')).toHaveText('—')
  await expect(page.getByTestId('win-rate-metric')).toContainText('平仓样本尚未加载')
  await expect(page.getByTestId('market-stats-error')).toContainText('当前分布未知')
  await expect(page.getByTestId('time-stats-error')).toContainText('当前交易趋势未知')
  await expect(page.getByTestId('holdings-cost-error')).toContainText('当前排行未知')
  await expect(page.getByText('区间内无平仓', { exact: false })).toHaveCount(0)
  failing = false
  await page.getByRole('button', { name: '重试收益曲线' }).click()
  await expect(page.getByTestId('analytics-error')).toHaveCount(0)
  await expect(page.getByTestId('range-ttwr')).toHaveText('+10.00%')
  await expect(page.getByTestId('win-rate-metric')).toContainText('样本 0 笔平仓，区间内无平仓')
  await expect(page.getByRole('img', { name: /证券组合收益率曲线/ })).toHaveAccessibleName(
    /2026\/01\/02 至 2026\/02\/03/
  )
  failing = true
  await page.getByRole('button', { name: '近1月', exact: true }).click()
  await expect(page.getByTestId('analytics-error')).toContainText('当前区间与基准尚未确认')
  await expect(page.getByTestId('range-ttwr')).toHaveText('+10.00%')
  await expect(page.getByRole('img', { name: /证券组合收益率曲线/ })).toHaveAccessibleName(
    /2026\/01\/02 至 2026\/02\/03.*保留上次成功结果/
  )
})

for (const terminalRate of [null, '', '   ', 'NaN', 'Infinity', '-Infinity', 'invalid']) {
  test(`期末 TTWR 为 ${String(terminalRate) || '空值'} 时不冒充前一有效点`, async ({
    page,
    request
  }) => {
    await page.route('**/api/statistics/performance-analytics**', (route) =>
      route.fulfill({
        json: {
          ...analyticsData,
          curve: [
            { date: '2026-01-02', cumulative_return_rate: 8.5, drawdown_rate: 0 },
            { date: '2026-02-03', cumulative_return_rate: terminalRate, drawdown_rate: null }
          ]
        }
      })
    )
    await setAuthenticatedSession(page, await loginThroughApi(request))
    await page.goto('/statistics')
    await expect(page.getByTestId('range-ttwr')).toHaveText('—')
    await expect(page.getByTestId('ttwr-unknown')).toHaveText('期末未知')
    await expect(page.getByRole('img', { name: /证券组合收益率曲线/ })).toHaveAccessibleName(
      /2026\/01\/02 至 2026\/02\/03；期末累计 TTWR —，回撤 —/
    )
    await expect(page.getByTestId('range-ttwr')).not.toHaveText('+8.50%')
    await expect(page.getByTestId('range-ttwr')).not.toHaveText('0.00%')
  })
}

test('期末真实零收益仍显示零，不标成未知', async ({ page, request }) => {
  await page.route('**/api/statistics/performance-analytics**', (route) =>
    route.fulfill({
      json: {
        ...analyticsData,
        curve: [{ date: '2026-02-03', cumulative_return_rate: 0, drawdown_rate: 0 }]
      }
    })
  )
  await setAuthenticatedSession(page, await loginThroughApi(request))
  await page.goto('/statistics')
  await expect(page.getByTestId('range-ttwr')).toHaveText('0.00%')
  await expect(page.getByTestId('ttwr-unknown')).toHaveCount(0)
  await expect(page.getByRole('img', { name: /证券组合收益率曲线/ })).toHaveAccessibleName(
    /期末累计 TTWR 0.00%，回撤 0.00%/
  )
})

test('空收益曲线的期末 TTWR 保留未知', async ({ page, request }) => {
  await page.route('**/api/statistics/performance-analytics**', (route) =>
    route.fulfill({ json: { ...analyticsData, curve: [] } })
  )
  await setAuthenticatedSession(page, await loginThroughApi(request))
  await page.goto('/statistics')
  await expect(page.getByTestId('range-ttwr')).toHaveText('—')
  await expect(page.getByTestId('ttwr-unknown')).toBeVisible()
  await expect(page.getByRole('img', { name: /证券组合收益率曲线/ })).toHaveCount(0)
})
test('narrow custom range supports keyboard apply and cancel without overflow or extra queries', async ({
  page,
  request
}) => {
  await page.setViewportSize({ width: 320, height: 852 })
  const queries: string[] = []
  await page.route('**/api/statistics/performance-analytics**', (route) => {
    queries.push(route.request().url())
    return route.fulfill({ json: analyticsData })
  })
  await setAuthenticatedSession(page, await loginThroughApi(request))
  await page.goto('/statistics')
  await expect(page.getByRole('button', { name: '自定义', exact: true })).toBeVisible()
  await expect.poll(() => queries.length).toBe(1)
  const trigger = page.getByRole('button', { name: '自定义', exact: true })
  await trigger.focus()
  await trigger.press('Enter')
  const dialog = page.getByRole('dialog', { name: '收益日期范围' })
  await expect(dialog).toBeVisible()
  await expect
    .poll(async () => {
      const r = await dialog.boundingBox()
      return r !== null && r.x >= 0 && r.x + r.width <= 320
    })
    .toBe(true)
  await dialog.getByRole('textbox', { name: '开始日期', exact: true }).fill('2026-02-03')
  await dialog.getByRole('textbox', { name: '结束日期', exact: true }).fill('2026-01-02')
  await expect(dialog.getByRole('button', { name: '应用范围' })).toBeDisabled()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(trigger).toBeFocused()
  expect(queries).toHaveLength(1)
  await expect(page.getByRole('button', { name: '成立以来', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true'
  )
  await trigger.press('Enter')
  await dialog.getByRole('textbox', { name: '开始日期', exact: true }).fill('2026-01-02')
  await dialog.getByRole('textbox', { name: '结束日期', exact: true }).fill('2026-02-03')
  await dialog.getByRole('button', { name: '应用范围' }).click()
  await expect.poll(() => queries.length).toBe(2)
  expect(queries[1]).toContain('start_date=2026-01-02')
  expect(queries[1]).toContain('end_date=2026-02-03')
  await page.getByRole('button', { name: '成立以来', exact: true }).click()
  await expect.poll(() => queries.length).toBe(3)
  await trigger.press('Enter')
  await expect(dialog).toBeVisible()
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await expect(dialog).toBeHidden()
  expect(queries).toHaveLength(3)
  await expect(page.getByRole('button', { name: '成立以来', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true'
  )
  await trigger.press('Enter')
  await expect(dialog).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  expect(queries).toHaveLength(3)
  await expect(page.getByRole('button', { name: '成立以来', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true'
  )
  await expect.poll(() => page.evaluate(() => document.body.scrollWidth)).toBe(320)
})

test('named benchmark selection enforces three choices and supports replacing a choice', async ({
  page,
  request
}) => {
  const selections: string[] = []
  await page.route('**/api/statistics/benchmarks', (route) =>
    route.fulfill({
      json: [
        { code: '000300.SH', name: '测试基准一' },
        { code: 'TEST2', name: '测试基准二' },
        { code: 'TEST3', name: '测试基准三' },
        { code: 'TEST4', name: '测试基准四' }
      ]
    })
  )
  await page.route('**/api/statistics/performance-analytics**', (route) => {
    selections.push(new URL(route.request().url()).searchParams.get('benchmarks') ?? '')
    return route.fulfill({ json: analyticsData })
  })
  await setAuthenticatedSession(page, await loginThroughApi(request))
  await page.goto('/statistics')
  const input = page.getByRole('combobox', { name: '对比基准，最多三项' })
  await input.focus()
  await input.press('ArrowDown')
  await expect(input).toHaveAttribute('aria-expanded', 'true')
  const active = await input.getAttribute('aria-activedescendant')
  expect(active).toBeTruthy()
  await expect(page.locator(`[id="${active}"]`)).toHaveAttribute('role', 'option')
  await page.getByRole('option', { name: '测试基准二', exact: true }).click()
  await page.getByRole('option', { name: '测试基准三', exact: true }).click()
  await expect.poll(() => selections.at(-1)).toBe('000300.SH,TEST2,TEST3')
  await input.fill('基准四')
  await expect(page.getByRole('option', { name: '测试基准四', exact: true })).toBeVisible()
  await expect(page.getByRole('option', { name: '测试基准三', exact: true })).toBeHidden()
  await expect(page.getByRole('option', { name: '测试基准四', exact: true })).toHaveAttribute(
    'aria-disabled',
    'true'
  )
  const beforeLimit = selections.length
  await input.press('ArrowDown')
  await input.press('Enter')
  await page.waitForLoadState('networkidle')
  expect(selections).toHaveLength(beforeLimit)
  await input.fill('基准三')
  await page.getByRole('option', { name: '测试基准三', exact: true }).click()
  await expect.poll(() => selections.at(-1)).toBe('000300.SH,TEST2')
  await input.fill('基准四')
  await page.getByRole('option', { name: '测试基准四', exact: true }).click()
  await expect.poll(() => selections.at(-1)).toBe('000300.SH,TEST2,TEST4')
  await input.fill('完全没有匹配的测试基准')
  await expect(page.getByText('暂无匹配的基准', { exact: true })).toBeVisible()
  await expect(page.getByText('基准目录加载失败', { exact: false })).toHaveCount(0)
  const beforeEmpty = selections.length
  await input.press('ArrowDown')
  await input.press('Enter')
  await page.waitForLoadState('networkidle')
  expect(selections).toHaveLength(beforeEmpty)
  await input.press('Escape')
  await expect(input).toBeFocused()
})

for (const width of [1440, 320]) {
  test(`交易趋势明细可在 ${width}px 通过键盘展开并读取完整金额`, async ({ page, request }) => {
    await page.setViewportSize({ width, height: 900 })
    await page.route('**/api/statistics/performance-analytics**', (route) =>
      route.fulfill({ json: analyticsData })
    )
    await page.route('**/api/statistics/by-time*', (route) =>
      route.fulfill({
        json: [
          { period: '2026-01', buy_amount: 1234567.89, sell_amount: 98765.43 },
          { period: '2026-02', buy_amount: 0, sell_amount: 250000.12 }
        ]
      })
    )
    await setAuthenticatedSession(page, await loginThroughApi(request))
    await page.goto('/statistics')
    const chart = page.getByRole('img', { name: /按月交易金额趋势/ })
    await expect(chart).toHaveAccessibleName(/共 2 期/)
    const details = page.getByTestId('time-trend-details')
    const table = details.getByRole('table')
    await expect(table).toBeHidden()
    const summary = details.locator('summary')
    await summary.focus()
    await summary.press('Enter')
    await expect(table).toBeVisible()
    await expect(table.getByRole('row', { name: /2026-01/ })).toContainText('¥1,234,567.89')
    await expect(table.getByRole('row', { name: /2026-01/ })).toContainText('¥98,765.43')
    await expect(table.getByRole('row', { name: /2026-02/ })).toContainText('¥0.00')
    await expect
      .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
      .toBe(true)
    await summary.press('Space')
    await expect(table).toBeHidden()
  })
}
