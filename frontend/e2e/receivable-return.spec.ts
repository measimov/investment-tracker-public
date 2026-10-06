import { expect, test } from '@playwright/test'
import type { PeriodPnlResponse, PeriodPnlSummary } from '../src/types'
import { loginThroughApi, setAuthenticatedSession } from './helpers'

const estimate = {
  as_of: '2026-06-02',
  cash_basis_return_cny: 100,
  known_pending_gross_cny: 900,
  known_pending_gross_by_currency: { HKD: 1000 },
  estimated_return_cny: 1000,
  included_count: 1,
  unresolved_count: 0,
  overdue_count: 1,
  pending_overdue_count: 1,
  review_counts: { received: 0, possible_receipt: 0, entitlement: 0, amount: 0, zero_remaining: 0 },
  received_review_reasons: {
    net_amount_only: 0,
    currency_mismatch: 0,
    payout_currency_unverified: 0,
    other: 0
  },
  missing_rate_currencies: [] as string[],
  is_partial: false
}

function periodSummary(overrides: Partial<PeriodPnlSummary> = {}): PeriodPnlSummary {
  return {
    label: '本月',
    start_date: '2026-06-01',
    end_date: '2026-06-02',
    status: 'exact',
    pnl_cny: -900,
    return_rate: -9,
    stale_opening_basis: [],
    opening_unpriced_positions: [],
    opening_market_value_cny: 10000,
    closing_market_value_cny: 9100,
    cash_in_cny: 0,
    cash_out_cny: 0,
    dividend_income_cny: 0,
    points: 2,
    unpriced_positions: [],
    stale_price_positions: [],
    receivable_pnl: {
      cash_basis_pnl_cny: -900,
      opening_receivable_cny: 0,
      closing_receivable_cny: 900,
      receivable_change_cny: 900,
      estimated_pnl_cny: 0,
      is_partial: false,
      unresolved_count: 0,
      missing_rate_currencies: []
    },
    ...overrides
  }
}

function periodResponse(
  mtd = periodSummary(),
  ytd = periodSummary({ label: '本年', start_date: '2026-01-01' })
): PeriodPnlResponse {
  return {
    base_currency: 'CNY',
    as_of: mtd.end_date,
    methodology: { scope: 'equity', method: 'curve', status: 'experimental', description: '' },
    periods: { daily: periodSummary({ label: '当日', receivable_pnl: undefined }), mtd, ytd },
    data_quality: { warnings: [] }
  }
}

function summary(receivable = estimate) {
  return {
    current_performance: { current_market_value_cny: 0, holdings_detail: [] },
    realized_pnl: { trades_detail: [], data_quality: { warnings: [] } },
    dividend_summary: {
      total_dividend_net: 0,
      by_symbol: [],
      legacy_unreviewed_count: 2,
      forecast: {
        pending_gross_by_currency: { HKD: 1000 },
        announced_gross_by_currency: {},
        overdue_count: 58,
        unknown_amount_count: 46
      }
    },
    total_realized_return: { total_realized_return_cny: 100, net_dividend_income_cny: 0 },
    account_return: { total_return: 100, total_return_cny: 100, annualized_return_rate: null },
    receivable_return: receivable
  }
}

for (const withReceivable of [true, false]) {
  test(`NET_ONLY 已知金额及累计组成在 ${withReceivable ? '双口径' : 'fallback'} 手机布局常显`, async ({
    page,
    request
  }) => {
    await page.setViewportSize({ width: 320, height: 852 })
    const fixture = {
      ...summary({ ...estimate, cash_basis_return_cny: 170, estimated_return_cny: 1070 }),
      receivable_return: withReceivable
        ? { ...estimate, cash_basis_return_cny: 170, estimated_return_cny: 1070 }
        : null,
      account_return: {
        total_return: 170,
        total_return_rate: 17,
        annualized_return_rate: null,
        net_invested_principal_cny: 1000,
        current_market_value_cny: 1050,
        realized_trading_pnl_cny: 25,
        unrealized_pnl_cny: 50,
        net_dividend_income_cny: 95
      },
      dividend_summary: {
        total_dividend_gross: 100,
        total_tax: 20,
        total_dividend_net: 95,
        amounts_incomplete_count: 1,
        unallocated_tax_count: 1,
        legacy_unreviewed_count: 2,
        missing_rate_currencies: ['CHF'],
        by_symbol: []
      }
    }
    await page.route('**/api/statistics/performance-summary', (route) =>
      route.fulfill({ json: fixture })
    )
    await setAuthenticatedSession(page, await loginThroughApi(request))
    await page.goto('/statistics')
    const composition = page.locator('.equity-return')
    for (const [label, amount] of [
      ['净投入本金（权益仓）：', '¥1,000.00'],
      ['当前市值：', '¥1,050.00'],
      ['已实现交易盈亏：', '¥25.00'],
      ['未实现盈亏：', '¥50.00'],
      ['税后股息：', '¥95.00']
    ]) {
      const item = composition.locator('.stat-item').filter({ hasText: label })
      await expect(item).toBeVisible()
      await expect(item.locator('.value')).toHaveText(amount)
    }
    const dividend = page.locator('.dividend-section')
    await expect(dividend).toContainText('税前总额未知')
    await expect(dividend).toContainText('股息税尚待归属')
    await expect(dividend).toContainText('历史股息仍沿用旧账本计算')
    await expect(dividend).toContainText('缺少 CHF 汇率')
    for (const [label, amount] of [
      ['累计实收净股息', '¥95.00'],
      ['已知税前金额', '¥100.00'],
      ['已记录股息税', '¥20.00']
    ]) {
      const metric = dividend.locator('.financial-statistic').filter({ hasText: label })
      await expect(metric).toBeVisible()
      await expect(metric.locator('.financial-statistic-value')).toHaveText(amount)
    }
    await expect(dividend.getByText('累计股息（税前）', { exact: true })).toHaveCount(0)
    await expect(page.getByTestId('receivable-return')).toHaveCount(withReceivable ? 1 : 0)
    if (!withReceivable)
      await expect(
        composition
          .locator('.financial-statistic')
          .filter({ hasText: /^总收益/ })
          .first()
      ).toContainText('¥170.00')
    await expect
      .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
      .toBe(true)
  })
}

test('dashboard and statistics share cash and receivable totals, then show receipt replacement', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  let current = { ...estimate }
  await page.route('**/api/statistics/period-pnl', (route) =>
    route.fulfill({ json: periodResponse() })
  )
  await page.route('**/api/statistics/performance-summary', (route) =>
    route.fulfill({ json: summary(current) })
  )
  await page.route('**/api/statistics/portfolio-snapshot', (route) =>
    route.fulfill({
      json: {
        performance: summary(current),
        prices: {},
        markets: [],
        recent_transactions: [],
        accounts: [],
        data_quality: { warnings: [] }
      }
    })
  )
  await setAuthenticatedSession(page, token)
  for (const path of ['/', '/statistics']) {
    await page.goto(path)
    const card = page.getByTestId('receivable-return')
    await expect(card.getByTestId('cash-basis-return')).toHaveText('¥100.00')
    await expect(card.getByTestId('pending-dividend-return')).toHaveText('¥900.00')
    await expect(card.getByTestId('estimated-dividend-return')).toHaveText('¥1,000.00')
    await expect(card).toContainText('2 笔旧计算历史股息，到账依据待核验')
    const trigger = card.getByRole('button', { name: '查看累计收益口径说明' })
    await trigger.click()
    const explanation = page.locator('.n-popover')
    await expect(explanation).toContainText('当前收益包含沿用旧计算的历史股息')
    await expect(explanation).toContainText('未扣预计税款')
    await page.keyboard.press('Escape')
    await expect(explanation).toBeHidden()
    await card.screenshot({
      path: test.info().outputPath(path === '/' ? 'dashboard.png' : 'statistics.png')
    })
  }
  // The server replaces the estimate with actual net proceeds; neither view adds the old forecast.
  current = {
    ...estimate,
    cash_basis_return_cny: 910,
    known_pending_gross_cny: 0,
    known_pending_gross_by_currency: { HKD: 0 },
    estimated_return_cny: 910,
    included_count: 0,
    overdue_count: 0,
    pending_overdue_count: 0
  }
  for (const path of ['/', '/statistics']) {
    await page.goto(path)
    await expect(page.getByTestId('cash-basis-return')).toHaveText('¥910.00')
    await expect(page.getByTestId('pending-dividend-return')).toHaveText('¥0.00')
    await expect(page.getByTestId('estimated-dividend-return')).toHaveText('¥910.00')
  }
})

test('partial amounts and missing exchange rates stay explicit on mobile', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  const partial = {
    ...estimate,
    unresolved_count: 46,
    overdue_count: 58,
    pending_overdue_count: 11,
    review_counts: {
      received: 40,
      possible_receipt: 2,
      entitlement: 3,
      amount: 1,
      zero_remaining: 1
    },
    received_review_reasons: {
      net_amount_only: 27,
      currency_mismatch: 11,
      payout_currency_unverified: 2,
      other: 0
    },
    missing_rate_currencies: ['CHF'],
    is_partial: true
  }
  await page.route('**/api/statistics/period-pnl', (route) =>
    route.fulfill({ json: periodResponse() })
  )
  await page.route('**/api/statistics/performance-summary', (route) =>
    route.fulfill({ json: summary(partial) })
  )
  await page.route('**/api/statistics/portfolio-snapshot', (route) =>
    route.fulfill({
      json: {
        performance: summary(partial),
        prices: {},
        markets: [],
        recent_transactions: [],
        accounts: [],
        data_quality: { warnings: [] }
      }
    })
  )
  await setAuthenticatedSession(page, token)
  await page.setViewportSize({ width: 390, height: 844 })
  for (const path of ['/', '/statistics']) {
    await page.goto(path)
    const card = page.getByTestId('receivable-return')
    await expect(card).toContainText('累计收益·含已知待收估算（部分）')
    await expect(card.getByTestId('receivable-unresolved')).toContainText(
      '46 笔公告需核对，不等于未收股息'
    )
    await expect(card.getByTestId('receivable-received-review')).toContainText(
      '40 笔已匹配实收，待确认是否收齐'
    )
    await card.getByRole('button', { name: '查看累计收益口径说明' }).click()
    const explanation = page.locator('.n-popover')
    await expect(explanation).toBeVisible()
    await expect(explanation).toContainText('已收金额已计入收益')
    await expect(explanation).toContainText('27 笔仅有净到账额')
    await expect(explanation).toContainText('11 笔公告与到账币种不同')
    await expect(explanation).toContainText('2 笔派息币种待核实')
    await expect(card).toContainText('2 笔疑似到账，尚未关联')
    await expect(card).toContainText('3 笔账户或持股数量待核对')
    await expect(card).toContainText('1 笔派息金额或币种待核实')
    await expect(explanation.getByTestId('receivable-overdue')).toContainText('11 笔已过预计派息日')
    await expect(card).not.toContainText('58 笔')
    await expect(page.getByTestId('dividend-forecast-summary')).toHaveCount(0)
    await expect(page.getByText('58 笔已过预计派息日，待核对。')).toHaveCount(0)
    await expect(card.getByTestId('receivable-zero-remaining')).toContainText(
      '1 笔已匹配实收且剩余为零'
    )
    await expect(explanation).toContainText('剩余金额尚不能确定，暂不再加算待收')
    await expect(explanation).toContainText('未再计入待收，也不列为逾期未收')
    await page.keyboard.press('Escape')
    await expect(explanation).toBeHidden()
    await expect(card).toContainText('缺少 CHF 汇率')
    const bounds = await card.boundingBox()
    expect(bounds!.x).toBeGreaterThanOrEqual(0)
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390)
    await expect(page.getByRole('link', { name: '核对股息' })).toHaveAttribute(
      'href',
      '/corporate-actions'
    )
    await card.screenshot({
      path: test.info().outputPath(path === '/' ? 'dashboard-mobile.png' : 'statistics-mobile.png')
    })
  }
})

test('monthly receipt cancels opening receivable and both pages keep annual cash and estimated P&L separate', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  let periods = periodResponse()
  await page.route('**/api/statistics/period-pnl', (route) => route.fulfill({ json: periods }))
  await page.route('**/api/statistics/performance-summary', (route) =>
    route.fulfill({ json: summary() })
  )
  await page.route('**/api/statistics/portfolio-snapshot', (route) =>
    route.fulfill({
      json: {
        performance: summary(),
        prices: {},
        markets: [],
        recent_transactions: [],
        accounts: [],
        data_quality: { warnings: [] }
      }
    })
  )
  await setAuthenticatedSession(page, token)
  await page.setViewportSize({ width: 390, height: 844 })
  for (const path of ['/', '/statistics']) {
    await page.goto(path)
    await expect(page.getByTestId('period-pnl-mtd')).toHaveText('-¥900.00')
    await expect(page.getByTestId('period-estimated-pnl-mtd')).toHaveText('¥0.00')
    await expect(page.getByTestId('period-estimated-pnl-ytd')).toHaveText('¥0.00')
    await expect(page.getByTestId('period-receivable-daily')).toHaveCount(0)
  }
  // A June dividend of 900 paid net 810 in July: July includes only the -90 tax difference.
  // Annual cash P&L already includes June's -900 price change and July's +810 receipt.
  periods = periodResponse(
    periodSummary({
      start_date: '2026-07-01',
      end_date: '2026-07-15',
      pnl_cny: 810,
      dividend_income_cny: 810,
      receivable_pnl: {
        cash_basis_pnl_cny: 810,
        opening_receivable_cny: 900,
        closing_receivable_cny: 0,
        receivable_change_cny: -900,
        estimated_pnl_cny: -90,
        is_partial: false,
        unresolved_count: 0,
        missing_rate_currencies: []
      }
    }),
    periodSummary({
      label: '本年',
      start_date: '2026-01-01',
      end_date: '2026-07-15',
      pnl_cny: -90,
      receivable_pnl: {
        cash_basis_pnl_cny: -90,
        opening_receivable_cny: 0,
        closing_receivable_cny: 0,
        receivable_change_cny: 0,
        estimated_pnl_cny: -90,
        is_partial: false,
        unresolved_count: 0,
        missing_rate_currencies: []
      }
    })
  )
  for (const path of ['/', '/statistics']) {
    await page.goto(path)
    await expect(page.getByTestId('period-pnl-mtd')).toHaveText('¥810.00')
    await expect(page.getByTestId('period-estimated-pnl-mtd')).toHaveText('-¥90.00')
    await expect(page.getByTestId('period-estimated-pnl-ytd')).toHaveText('-¥90.00')
    const month = page.getByTestId('period-receivable-mtd')
    await expect(month.locator('details')).toHaveCount(0)
    await expect(month).toContainText(/期初待收\s*¥900\.00/)
    await expect(month).toContainText(/期末待收\s*¥0\.00/)
    await month.getByRole('button', { name: '查看本月待收损益口径' }).click()
    const explanation = page.locator('.n-popover')
    await expect(explanation).toContainText('避免跨月重复计算')
    await page.keyboard.press('Escape')
    await expect(explanation).toBeHidden()
    const bounds = await month.boundingBox()
    expect(bounds!.x).toBeGreaterThanOrEqual(0)
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390)
    await expect(page.getByTestId('period-estimated-pnl-daily')).toHaveCount(0)
  }
})

test('missing historical data stays partial and unavailable period P&L never becomes zero', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  const partial = periodSummary({
    status: 'unavailable',
    pnl_cny: null,
    return_rate: null,
    receivable_pnl: {
      cash_basis_pnl_cny: null,
      opening_receivable_cny: 0,
      closing_receivable_cny: 100,
      receivable_change_cny: 100,
      estimated_pnl_cny: null,
      is_partial: true,
      unresolved_count: 2,
      missing_rate_currencies: ['HKD']
    }
  })
  await page.route('**/api/statistics/period-pnl', (route) =>
    route.fulfill({ json: periodResponse(partial) })
  )
  await page.route('**/api/statistics/performance-summary', (route) =>
    route.fulfill({ json: summary() })
  )
  await page.route('**/api/statistics/portfolio-snapshot', (route) =>
    route.fulfill({
      json: {
        performance: summary(),
        prices: {},
        markets: [],
        recent_transactions: [],
        accounts: [],
        data_quality: { warnings: [] }
      }
    })
  )
  await setAuthenticatedSession(page, token)
  for (const path of ['/', '/statistics']) {
    await page.goto(path)
    await expect(page.getByTestId('period-pnl-mtd')).toHaveText('无法计算')
    await expect(page.getByTestId('period-estimated-pnl-mtd')).toHaveText('无法计算')
    await expect(page.getByTestId('period-receivable-mtd')).toContainText('含已知待收（税前·部分）')
    await expect(page.getByTestId('period-receivable-mtd')).toContainText(
      '2 笔历史权益、金额或到账关联尚待核对'
    )
    await expect(page.getByTestId('period-receivable-mtd')).toContainText('缺少 HKD 对应日期汇率')
  }
})
