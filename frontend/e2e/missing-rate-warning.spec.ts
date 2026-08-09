// 统计页：缺汇率提示必须可见（PR #148 复审要求的前端回归）。
//
// 后端在缺汇率时会把无法折算的金额从 CNY 汇总里剔除（不再按原值当人民币混入），
// 但只有 realized_pnl 那一块带 data_quality.warnings；汇总 / 分市场 / 持仓表现 /
// 股息四块只给 missing_rate_currencies 字段。前端若漏接任一路径，用户只会看到
// 「金额变成 0」而不知道原因——这里用 mock 响应把每条路径单独钉死，
// 以后从 sources 数组里漏掉哪一个，对应用例就会红。
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const user = { username: 'demo', password: 'e2e-user-password' }
const authCookieName = 'investment_session'
const csrfCookieName = 'investment_csrf'

async function loginThroughApi(request: APIRequestContext): Promise<string> {
  const response = await request.post('http://127.0.0.1:18000/api/auth/token', { data: user })
  expect(response.ok()).toBeTruthy()
  return (await response.json()).access_token
}

async function setSession(page: Page, token: string) {
  await page.context().addCookies([
    {
      name: authCookieName,
      value: token,
      url: 'http://127.0.0.1:18000',
      httpOnly: true,
      secure: false,
      sameSite: 'Strict'
    },
    {
      name: csrfCookieName,
      value: 'e2e-csrf-token',
      url: 'http://127.0.0.1:18000',
      httpOnly: false,
      secure: false,
      sameSite: 'Strict'
    }
  ])
  await page.addInitScript(() => {
    window.localStorage.setItem(
      'user',
      JSON.stringify({ id: 2, username: 'demo', email: null, is_active: true, is_admin: false })
    )
  })
}

const EMPTY_REALIZED = {
  realized_pnl: 0,
  realized_pnl_cny: 0,
  sold_cost: 0,
  sold_cost_cny: 0,
  realized_pnl_rate: 0,
  trades_detail: [],
  closed_trades: [],
  missing_rate_currencies: [],
  // 关键：realized 这一块**没有** warnings，提示只能来自其他路径
  data_quality: { warnings: [] }
}

const EMPTY_DIVIDEND = {
  total_dividend_gross: 0,
  total_tax: 0,
  total_dividend_net: 0,
  by_symbol: [],
  missing_rate_currencies: []
}

const EMPTY_CURRENT = {
  unrealized_pnl: 0,
  current_holdings_cost: 0,
  unrealized_pnl_rate: 0,
  current_market_value: 0,
  holdings_detail: [],
  missing_rate_currencies: [],
  data_quality: { warnings: [] }
}

/** 只 mock 统计页首屏依赖的端点；其余走真实后端。 */
async function mockStatistics(
  page: Page,
  options: {
    summaryMissing?: string[]
    marketMissing?: string[]
    currentMissing?: string[]
    dividendMissing?: string[]
    realizedWarnings?: string[]
  }
) {
  await page.route('**/api/statistics/summary*', (route) =>
    route.fulfill({
      json: {
        total_invested_cny: 0,
        total_invested_by_currency: { THB: 1000 },
        total_holdings: 1,
        total_transactions: 1,
        markets_count: 1,
        base_currency: 'CNY',
        exchange_rates_used: {},
        missing_rate_currencies: options.summaryMissing ?? []
      }
    })
  )
  await page.route('**/api/statistics/by-market*', (route) =>
    route.fulfill({
      json: [
        {
          market: '美股',
          total_cost: 0,
          total_cost_cny: 0,
          total_cost_usd: 0,
          total_cost_by_currency: { THB: 1000 },
          holdings_count: 1,
          missing_rate_currencies: options.marketMissing ?? []
        }
      ]
    })
  )
  await page.route('**/api/statistics/performance-summary*', (route) =>
    route.fulfill({
      json: {
        current_performance: {
          ...EMPTY_CURRENT,
          missing_rate_currencies: options.currentMissing ?? []
        },
        realized_pnl: {
          ...EMPTY_REALIZED,
          data_quality: { warnings: options.realizedWarnings ?? [] }
        },
        dividend_summary: {
          ...EMPTY_DIVIDEND,
          missing_rate_currencies: options.dividendMissing ?? []
        },
        total_realized_return: {},
        account_return: {}
      }
    })
  )
}

async function openStatistics(page: Page) {
  await page.goto('/statistics')
  // 等首屏渲染完（骨架屏退场）：提示条挂在 v-else 分支里
  await expect(page.locator('.statistics-page')).toBeVisible()
  await expect(page.getByRole('main').getByText('含股息已实现收益：')).toBeVisible()
}

test.beforeEach(async ({ page, request }) => {
  await setSession(page, await loginThroughApi(request))
})

test('仅 current_performance 命中缺汇率时统计页也要显示提示', async ({ page }) => {
  // 复审点名的场景：realized warnings 为空，提示只能由 current_performance 触发
  await mockStatistics(page, { currentMissing: ['THB'], realizedWarnings: [] })
  await openStatistics(page)

  const alert = page.locator('.summary-warning').filter({ hasText: 'THB' })
  await expect(alert).toBeVisible()
  await expect(alert).toContainText('未计入 CNY 汇总')
})

test('仅 summary_statistics 命中缺汇率时同样要显示提示', async ({ page }) => {
  await mockStatistics(page, { summaryMissing: ['THB'], realizedWarnings: [] })
  await openStatistics(page)

  await expect(page.locator('.summary-warning').filter({ hasText: 'THB' })).toBeVisible()
})

test('仅 by-market 命中缺汇率时同样要显示提示', async ({ page }) => {
  await mockStatistics(page, { marketMissing: ['THB'], realizedWarnings: [] })
  await openStatistics(page)

  await expect(page.locator('.summary-warning').filter({ hasText: 'THB' })).toBeVisible()
})

test('仅 dividend_summary 命中缺汇率时同样要显示提示', async ({ page }) => {
  await mockStatistics(page, { dividendMissing: ['THB'], realizedWarnings: [] })
  await openStatistics(page)

  await expect(page.locator('.summary-warning').filter({ hasText: 'THB' })).toBeVisible()
})

test('多路径命中同一币种时不重复提示', async ({ page }) => {
  await mockStatistics(page, {
    summaryMissing: ['THB'],
    marketMissing: ['THB'],
    currentMissing: ['THB'],
    dividendMissing: ['THB'],
    realizedWarnings: []
  })
  await openStatistics(page)

  await expect(page.locator('.summary-warning').filter({ hasText: 'THB' })).toHaveCount(1)
})

test('realized 已提到该币种时不再追加重复提示', async ({ page }) => {
  await mockStatistics(page, {
    currentMissing: ['THB'],
    realizedWarnings: ['缺少 THB 对 CNY 的汇率，这些币种的金额未计入 CNY 汇总（不会按原值混入）。']
  })
  await openStatistics(page)

  await expect(page.locator('.summary-warning').filter({ hasText: 'THB' })).toHaveCount(1)
})

test('汇率齐备时不得误报', async ({ page }) => {
  await mockStatistics(page, { realizedWarnings: [] })
  await openStatistics(page)

  await expect(page.locator('.summary-warning')).toHaveCount(0)
})
