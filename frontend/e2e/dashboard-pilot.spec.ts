import { expect, test } from '@playwright/test'
import type { PeriodPnlResponse, PortfolioSnapshot } from '../src/types'
import { loginThroughApi, setAuthenticatedSession } from './helpers'

test('dashboard loading, failure and retry preserve unknown versus zero and small prices', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  const response = await request.get('http://127.0.0.1:18000/api/statistics/portfolio-snapshot', {
    headers: { Authorization: `Bearer ${token}` }
  })
  expect(response.ok()).toBeTruthy()
  const snapshot = await response.json()
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  let reads = 0
  await page.route('**/api/statistics/portfolio-snapshot', async (route) => {
    reads++
    if (reads === 1) await gate
    if (reads !== 2) return route.fulfill({ status: 503, json: { detail: '演示摘要暂不可用' } })
    return route.fulfill({
      json: {
        ...snapshot,
        performance: {
          ...snapshot.performance,
          current_performance: {
            ...snapshot.performance.current_performance,
            current_market_value_cny: 0,
            current_market_value_usd: 0
          },
          account_return: {
            ...snapshot.performance.account_return,
            total_return_cny: 0,
            total_return_rate: 0,
            annualized_return_rate: null
          },
          receivable_return: {
            ...snapshot.performance.receivable_return,
            cash_basis_return_cny: 0,
            estimated_return_cny: 0,
            known_pending_gross_cny: 0
          }
        },
        markets: [],
        recent_transactions: [
          {
            symbol: 'SMALL',
            name: '演示低价标的',
            market: '港股',
            transaction_type: 'BUY',
            quantity: 100,
            price: 0.085,
            currency: 'HKD',
            transaction_date: '2026-06-02'
          }
        ]
      }
    })
  })
  await page.goto('/')
  await expect(page.locator('.dashboard')).toHaveAttribute('aria-busy', 'true')
  await expect(page.getByTestId('dashboard-market-value')).toHaveCount(0)
  await page.screenshot({ path: test.info().outputPath('dashboard-loading.png') })
  release()
  await expect(page.getByTestId('dashboard-error')).toContainText('未知金额显示为 —')
  await expect(page.getByTestId('dashboard-market-value')).toHaveText('—')
  await expect(page.getByText('市场分布暂不可用', { exact: true })).toBeVisible()
  await expect(page.getByText('最近交易暂不可用', { exact: true })).toBeVisible()
  await page.screenshot({ path: test.info().outputPath('dashboard-error.png') })
  await page
    .getByTestId('dashboard-error')
    .getByRole('button', { name: '重试', exact: true })
    .click()
  await expect(page.getByTestId('dashboard-market-value')).toHaveText('¥0.00')
  await expect(page.getByTestId('cash-basis-return')).toHaveText('¥0.00')
  await expect(page.getByTestId('receivable-return')).toContainText('收益率 0.00%')
  await expect(page.getByTestId('receivable-return')).toContainText('年化（XIRR） —')
  await expect(page.getByTestId('receivable-return')).not.toContainText('年化（XIRR） 0.00%')
  await expect(page.getByText('0.085 HKD', { exact: true })).toBeVisible()
  await expect(page.getByTestId('recent-txn-symbol-link')).toHaveAttribute('href', /symbol=SMALL/)
  await expect(page.getByTestId('dashboard-error')).toHaveCount(0)
  await expect(page.getByText('暂无市场分布数据', { exact: true })).toBeVisible()
  await page.screenshot({
    path: test.info().outputPath('dashboard-zero-and-empty-market.png'),
    fullPage: true
  })
  await page.getByRole('button', { name: '重新加载' }).click()
  await expect(page.getByTestId('dashboard-error')).toContainText('保留上次成功加载的数据')
  await expect(page.getByTestId('dashboard-market-value')).toHaveText('¥0.00')
})

for (const width of [320, 375, 393, 1101, 1440]) {
  test(`dashboard financial explanations are keyboard reachable and contained at ${width}px`, async ({
    page,
    request
  }) => {
    await setAuthenticatedSession(page, await loginThroughApi(request))
    await page.setViewportSize({ width, height: 852 })
    await page.goto('/')
    await expect(page.getByTestId('dashboard-market-value')).toBeVisible()
    await expect(page.locator('.composition-grid')).toBeVisible()
    await expect(page.locator('.composition-grid details:not([open])')).toHaveCount(0)
    for (const name of [
      '查看仪表盘口径说明',
      '查看当日损益口径',
      '查看本月损益口径',
      '查看本年损益口径',
      '查看累计收益口径说明',
      '查看仪表盘FIFO盈亏口径',
      '查看市场成本口径',
      '查看本月待收损益口径',
      '查看本年待收损益口径'
    ]) {
      const trigger = page.getByRole('button', { name, exact: true })
      await trigger.click()
      const popover = page.locator('.n-popover')
      await expect(popover).toBeVisible()
      await page.keyboard.press('Escape')
      await expect(popover).toBeHidden()
      await expect(trigger).toBeFocused()
      await trigger.focus()
      await page.keyboard.press('Enter')
      await expect(popover).toBeVisible()
      if (name === '查看本月损益口径') {
        await expect(popover).toContainText('期初按区间起点前最近收盘价估值')
        await expect(popover).not.toContainText(/\d{4}-\d{2}-\d{2}/)
      } else if (name === '查看仪表盘FIFO盈亏口径')
        await expect(popover).toContainText('总收益一致，只是拆分归属不同')
      await expect
        .poll(async () => {
          const rect = await popover.boundingBox()
          return (
            !!rect &&
            rect.x >= 0 &&
            rect.y >= 0 &&
            rect.x + rect.width <= width &&
            rect.y + rect.height <= 852
          )
        })
        .toBe(true)
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width)
      await page.keyboard.press('Escape')
      await expect(popover).toBeHidden()
      await expect(popover).toHaveCount(0)
      await expect(trigger).toBeFocused()
    }
  })
}

test('dashboard keeps every financial metric expanded, recent trades half-width and account status last', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  const headers = { Authorization: `Bearer ${token}` }
  const snapshotResponse = await request.get(
    'http://127.0.0.1:18000/api/statistics/portfolio-snapshot',
    { headers }
  )
  const periodResponse = await request.get('http://127.0.0.1:18000/api/statistics/period-pnl', {
    headers
  })
  expect(snapshotResponse.ok()).toBeTruthy()
  expect(periodResponse.ok()).toBeTruthy()
  const snapshot = (await snapshotResponse.json()) as PortfolioSnapshot
  const periods = (await periodResponse.json()) as PeriodPnlResponse
  snapshot.performance.current_performance = {
    ...snapshot.performance.current_performance,
    current_market_value_cny: 145678.9,
    current_market_value_usd: 20000,
    current_holdings_cost_cny: 140246.8,
    unrealized_pnl_cny: 5432.1,
    unrealized_pnl_rate: 3.87,
    missing_rate_currencies: [],
    data_quality: { warnings: [] }
  }
  snapshot.performance.account_return = {
    ...snapshot.performance.account_return,
    total_return_cny: 10000,
    total_return_rate: 7.37,
    annualized_return_rate: 5.67
  }
  snapshot.performance.total_realized_return = {
    ...snapshot.performance.total_realized_return,
    total_realized_return_cny: 4567.9,
    net_dividend_income_cny: 678.9
  }
  snapshot.performance.dividend_summary.legacy_unreviewed_count = 0
  snapshot.performance.receivable_return = {
    as_of: '2026-06-11',
    cash_basis_return_cny: 10000,
    known_pending_gross_cny: 900,
    known_pending_gross_by_currency: { HKD: 1000 },
    estimated_return_cny: 10900,
    included_count: 1,
    unresolved_count: 0,
    overdue_count: 0,
    pending_overdue_count: 0,
    review_counts: {
      received: 0,
      possible_receipt: 0,
      entitlement: 0,
      amount: 0,
      zero_remaining: 0
    },
    received_review_reasons: {
      net_amount_only: 0,
      currency_mismatch: 0,
      payout_currency_unverified: 0,
      other: 0
    },
    missing_rate_currencies: [],
    is_partial: false
  }
  snapshot.prices = { missing_keys: [], stale_keys: [], freshness: {} }
  snapshot.data_quality = { warnings: [] }
  snapshot.markets = [
    { market: 'A股', total_cost: 90000 },
    { market: '港股', total_cost: 30000 },
    { market: '美股', total_cost: 20246.8 }
  ]
  snapshot.accounts = [
    '招商证券 A股',
    '招商证券 港股通',
    'IBKR 美股',
    'IBKR 港股',
    '富途 港股',
    '华泰 A股'
  ].map((account_name, index) => ({
    id: index + 1,
    account_name,
    latest_reconciliation: { status: 'MATCHED', all_scoped: true }
  }))
  snapshot.recent_transactions = Array.from({ length: 10 }, (_, index) => ({
    symbol: `UI${String(index + 1).padStart(2, '0')}`,
    name: `演示标的 ${index + 1}`,
    market: '港股',
    transaction_type: 'BUY',
    quantity: '100',
    price: (10 + index / 10).toFixed(2),
    currency: 'HKD',
    transaction_date: `2026-06-${String(11 - index).padStart(2, '0')}`
  }))
  for (const [key, label, cash, opening, estimated] of [
    ['daily', '当日', 12.34, 0, 12.34],
    ['mtd', '本月', 321.09, 100, 1121.09],
    ['ytd', '本年', 10000, 200, 10700]
  ] as const) {
    periods.periods[key] = {
      ...periods.periods[key],
      label,
      start_date: key === 'ytd' ? '2026-01-01' : key === 'mtd' ? '2026-06-01' : '2026-06-11',
      end_date: '2026-06-11',
      status: 'exact',
      pnl_cny: cash,
      return_rate: key === 'daily' ? 0.01 : key === 'mtd' ? 0.22 : 7.37,
      stale_opening_basis: [],
      stale_closing_prices: [],
      opening_unpriced_positions: [],
      unpriced_positions: [],
      stale_price_positions: [],
      dividend_income_cny: 0,
      receivable_pnl:
        key === 'daily'
          ? undefined
          : {
              cash_basis_pnl_cny: cash,
              opening_receivable_cny: opening,
              closing_receivable_cny: 900,
              receivable_change_cny: 900 - opening,
              estimated_pnl_cny: estimated,
              is_partial: false,
              unresolved_count: 0,
              missing_rate_currencies: []
            }
    }
  }
  periods.data_quality = { warnings: [] }
  await page.route('**/api/statistics/portfolio-snapshot', (route) =>
    route.fulfill({ json: snapshot })
  )
  await page.route('**/api/statistics/period-pnl', (route) => route.fulfill({ json: periods }))

  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 1280, height: 800 }
  ]) {
    await page.setViewportSize(viewport)
    await page.goto('/')
    await expect(page.getByTestId('dashboard-market-value')).toHaveText('¥145,678.90')
    await page.evaluate(() => document.fonts.ready)
    for (const [id, amount] of [
      ['dashboard-market-value', '¥145,678.90'],
      ['cash-basis-return', '¥10,000.00'],
      ['estimated-dividend-return', '¥10,900.00'],
      ['pending-dividend-return', '¥900.00'],
      ['period-pnl-daily', '¥12.34'],
      ['period-pnl-mtd', '¥321.09'],
      ['period-pnl-ytd', '¥10,000.00'],
      ['period-estimated-pnl-mtd', '¥1,121.09'],
      ['period-estimated-pnl-ytd', '¥10,700.00']
    ]) {
      const metric = page.getByTestId(id)
      await expect(metric).toHaveText(amount)
      await expect(metric).toBeInViewport({ ratio: 1 })
    }
    const cumulative = page.getByTestId('receivable-return')
    await expect(cumulative).toContainText('收益率 +7.37%')
    await expect(cumulative).toContainText('年化（XIRR） +5.67%')
    await expect(cumulative.locator('.compact-return-detail').first()).toBeInViewport({ ratio: 1 })
    await expect(cumulative.getByText('HK$1,000.00', { exact: true })).toBeInViewport({ ratio: 1 })
    const composition = page.locator('.composition-grid')
    await expect(composition).toBeInViewport({ ratio: 1 })
    await expect(composition).toContainText('¥5,432.10')
    await expect(composition).toContainText('¥4,567.90')
    await expect(composition).toContainText('含税后股息 ¥678.90')
    for (const [key, opening, change] of [
      ['mtd', '¥100.00', '¥800.00'],
      ['ytd', '¥200.00', '¥700.00']
    ]) {
      const period = page.getByTestId(`period-receivable-${key}`)
      const openingBalance = period
        .locator('.compact-period-balances > span')
        .filter({ hasText: '期初待收' })
      const closingBalance = period
        .locator('.compact-period-balances > span')
        .filter({ hasText: '期末待收' })
      await expect(openingBalance).toContainText(opening)
      await expect(closingBalance).toContainText('¥900.00')
      await expect(openingBalance).toBeInViewport({ ratio: 1 })
      await expect(closingBalance).toBeInViewport({ ratio: 1 })
      await expect(
        period.locator('.compact-period-balances > span').filter({ hasText: '本期待收变动' })
      ).toContainText(change)
      await expect(period).toBeInViewport({ ratio: 1 })
    }
    const accounts = page.locator('.account-badge')
    await expect(accounts).toHaveCount(6)
    for (const account of snapshot.accounts) {
      const badge = accounts.filter({ hasText: account.account_name })
      await expect(badge).toContainText('持仓一致')
      await expect(badge).toBeVisible()
    }
    const table = page.locator('.recent-transactions')
    const tableBounds = await table.boundingBox()
    const activityBounds = await page.locator('.activity-grid').boundingBox()
    const accountBounds = await page.locator('.reconciliation-strip').boundingBox()
    expect(tableBounds!.width / activityBounds!.width).toBeCloseTo(0.5, 1)
    expect(accountBounds!.y).toBeGreaterThanOrEqual(activityBounds!.y + activityBounds!.height)
    const rows = table.locator('tbody tr')
    await expect(rows).toHaveCount(10)
    await expect(page.getByTestId('recent-txn-symbol-link')).toHaveCount(10)
    for (const transaction of snapshot.recent_transactions) {
      const row = rows.filter({ hasText: transaction.symbol })
      await expect(row).toContainText(transaction.transaction_date.replaceAll('-', '/'))
      await expect(row).toContainText(transaction.name!)
      await expect(row).toContainText('买入')
      await expect(row).toContainText('100')
      await expect(row).toContainText(`${transaction.price} HKD`)
      await expect(row).toBeVisible()
    }
    await expect(page.getByRole('img', { name: '按市场分布的持仓成本' })).toBeInViewport({
      ratio: 1
    })
    await expect(page.locator('.market-cost-summary')).toBeInViewport({ ratio: 1 })
    expect(
      await table.evaluate((element) =>
        [element, ...element.querySelectorAll('*')].some((node) => {
          const style = getComputedStyle(node)
          return /auto|scroll/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 1
        })
      )
    ).toBe(false)
    expect(await page.evaluate(() => window.scrollY)).toBe(0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(viewport.width)
    await expect(page.locator('.dashboard details:not([open])')).toHaveCount(0)
    await page.screenshot({
      path: test.info().outputPath(`dashboard-expanded-${viewport.width}.png`)
    })
  }
})

test('missing receivable summary keeps cash return fallback and period failure stays separate', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  const headers = { Authorization: `Bearer ${token}` }
  const snapshot = await (
    await request.get('http://127.0.0.1:18000/api/statistics/portfolio-snapshot', { headers })
  ).json()
  const periods = await (
    await request.get('http://127.0.0.1:18000/api/statistics/period-pnl', { headers })
  ).json()
  snapshot.markets = [{ market: '港股', total_cost: 321.09, missing_rate_currencies: ['HKD'] }]
  delete snapshot.performance.receivable_return
  snapshot.performance.account_return.total_return_cny = 1234.56
  snapshot.performance.account_return.total_return_rate = 12.34
  snapshot.performance.account_return.annualized_return_rate = 5.67
  snapshot.performance.current_performance.unrealized_pnl_cny = 3333.33
  snapshot.data_quality = {
    ...snapshot.data_quality,
    warnings: ['演示缺少 HKD 汇率，相应金额未计入折算市值。']
  }
  snapshot.prices = {
    ...snapshot.prices,
    freshness: {
      'MISSING:港股': { source: 'missing', name: '演示缺价标的', price_date: null, stale: false },
      'OLD:A股': { source: 'cache', name: '演示陈价标的', price_date: '2026-01-01', stale: true }
    }
  }
  periods.periods.daily.status = 'exact'
  periods.periods.daily.pnl_cny = -678.9
  await page.route('**/api/statistics/portfolio-snapshot', (route) =>
    route.fulfill({ json: snapshot })
  )
  let available = false
  await page.route('**/api/statistics/period-pnl', (route) =>
    available
      ? route.fulfill({ json: periods })
      : route.fulfill({ status: 503, json: { detail: '期间损益演示失败' } })
  )
  await page.goto('/')
  const fallback = page.locator('.overview-metric').filter({ hasText: '总收益（权益仓）' })
  await expect(fallback).toContainText('¥1,234.56')
  await expect(fallback).toContainText('收益率 +12.34%')
  await expect(fallback).toContainText('年化（XIRR） +5.67%')
  await expect(page.getByRole('img', { name: '按市场分布的持仓成本' })).toHaveAccessibleDescription(
    /港股 ¥321\.09.*缺少 HKD 汇率，仅含可折算部分/
  )
  await expect(page.getByTestId('receivable-return')).toHaveCount(0)
  await expect(page.getByTestId('dashboard-period-error')).toBeVisible()
  await expect(page.locator('.overview-primary')).toContainText('已知持仓市值')
  for (const alert of [
    page.getByTestId('price-issues-missing'),
    page.getByTestId('price-issues-stale'),
    page.locator('.quality-alert').filter({ hasText: '演示缺少 HKD 汇率' }),
    page.getByTestId('dashboard-period-error')
  ]) {
    await expect(alert).toBeVisible()
    expect(
      await alert.evaluate((element) => {
        const marketValue = document.querySelector('[data-testid="dashboard-market-value"]')!
        return Boolean(
          element.compareDocumentPosition(marketValue) & Node.DOCUMENT_POSITION_FOLLOWING
        )
      })
    ).toBe(true)
  }
  await expect(page.getByTestId('period-pnl-daily')).toHaveText('—')
  await expect(page.getByTestId('period-pnl-row')).toHaveCount(0)
  await expect(page.getByTestId('dashboard-error')).toHaveCount(0)
  available = true
  await page.getByTestId('dashboard-period-error').getByRole('button', { name: '重试' }).click()
  await expect(page.getByTestId('period-pnl-row')).toBeVisible()
  await expect(page.getByTestId('dashboard-period-error')).toHaveCount(0)
  await expect(page.getByTestId('period-pnl-daily')).toHaveCount(1)
  await expect(page.locator('.portfolio-overview').getByTestId('period-pnl-daily')).toHaveText(
    '-¥678.90'
  )
  await expect(page.getByTestId('period-pnl-row').getByTestId('period-pnl-daily')).toHaveCount(0)
  await expect(page.getByRole('link', { name: '查看统计明细' })).toHaveAttribute(
    'href',
    '/statistics'
  )
})
