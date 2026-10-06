import { expect, test, type Page } from '@playwright/test'
import { loginThroughApi, setAuthenticatedSession } from './helpers'
const row = (market: string) => ({
  id: market === '港股' ? 202 : 101,
  symbol: market === '港股' ? 'UIB' : 'UIA',
  name: market === '港股' ? '虚构乙公司' : '虚构甲公司',
  market: market || 'A股',
  currency: 'CNY',
  action_type: 'CASH_DIVIDEND',
  ex_date: '2026-01-02',
  payment_date: '2026-01-03',
  net_dividend: market === '港股' ? 22.22 : 111.11,
  amount_basis: 'NET_ONLY',
  receipt_status: 'RECEIVED',
  read_only: true
})
const summary = (net: number) => ({
  total_count: 1,
  cash_dividends: {
    legacy_unreviewed_count: 0,
    amounts_incomplete_count: 1,
    total_dividend: null,
    total_tax: null,
    net_dividend: net,
    missing_rate_currencies: []
  }
})

// 徽标回归只使用虚构会话和拦截；未列出的 API 一律中止，不访问真实后端。
async function mockSuggestionCountPage(page: Page) {
  const user = { id: 992, username: 'ui-count-only', is_active: true, is_admin: false, email: null }
  await setAuthenticatedSession(page, 'ui-count-fixture-session', user)
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/auth/me') return route.fulfill({ json: user })
    if (path === '/api/auth/refresh') return route.fulfill({ json: { user } })
    if (path === '/api/corporate-actions/count') return route.fulfill({ json: { total: 0 } })
    if (path === '/api/corporate-actions/statistics/summary')
      return route.fulfill({
        json: {
          total_count: 0,
          cash_dividends: { total_dividend: 0, total_tax: 0, net_dividend: 0 }
        }
      })
    if (path === '/api/broker-accounts' || path === '/api/corporate-actions')
      return route.fulfill({ json: [] })
    return route.abort()
  })
}

const countSuggestion = {
  id: 993,
  symbol: 'UI-COUNT',
  name: '虚构徽标回归股息',
  market: 'A股',
  currency: 'CNY',
  action_type: 'CASH_DIVIDEND',
  cash_div_pre_tax: null,
  ex_date: '2026-01-02',
  status: 'NEW',
  receipt_state: 'ANNOUNCED',
  updated_at: '2026-01-03T00:00:00Z'
}

test('pending suggestion count stays unknown after initial failure and a retry can confirm zero', async ({
  page
}) => {
  await mockSuggestionCountPage(page)
  let release!: () => void
  let first = true
  const held = new Promise<void>((resolve) => (release = resolve))
  await page.route('**/api/corporate-actions/suggestions/count', async (route) => {
    if (first) {
      first = false
      await held
      return route.fulfill({ status: 503, json: { detail: '虚构徽标计数失败' } })
    }
    return route.fulfill({ json: { total: 0 } })
  })
  await page.goto('/corporate-actions')
  const status = page.getByTestId('suggestion-count-status')
  await expect(status).toHaveText('数量加载中')
  await expect(page.locator('.tab-badge')).toHaveCount(0)
  release()
  await expect(status).toHaveText('数量未确认')
  await expect(page.getByTestId('suggestion-count-error')).toContainText(
    '读取失败不表示没有待处理股息'
  )
  await page.getByRole('button', { name: '重试待处理数量', exact: true }).click()
  await expect(status).toHaveCount(0)
  await expect(page.getByTestId('suggestion-count-error')).toHaveCount(0)
  await expect(page.locator('.tab-badge')).toHaveCount(0)
})

test('counts-changed failure retains the last pending count with an update warning and nearby retry', async ({
  page
}) => {
  await mockSuggestionCountPage(page)
  let reads = 0
  let ignored = false
  await page.route('**/api/corporate-actions/suggestions/count', (route) => {
    reads++
    return route.fulfill(
      reads === 2
        ? { status: 503, json: { detail: '虚构徽标更新失败' } }
        : { json: { total: reads === 1 ? 3 : 0 } }
    )
  })
  await page.route('**/api/corporate-actions/suggestions?**', (route) =>
    route.fulfill({ json: ignored ? [] : [countSuggestion] })
  )
  await page.route('**/api/corporate-actions/suggestions/993/ignore', (route) => {
    ignored = true
    return route.fulfill({ json: { ...countSuggestion, status: 'IGNORED' } })
  })
  await page.goto('/corporate-actions')
  const badge = page.locator('.tab-badge')
  await expect(badge).toBeVisible()
  await expect(badge.locator('sup')).toHaveAttribute('title', '3')
  await expect(page.getByTestId('suggestion-count-status')).toHaveCount(0)
  await page.getByRole('tab', { name: /预计与待收股息/ }).click()
  await page.getByRole('button', { name: /^忽略 UI-COUNT/ }).click()
  await expect(page.getByTestId('suggestion-count-status')).toHaveText('数量更新失败')
  await expect(badge).toBeVisible()
  await expect(badge.locator('sup')).toHaveAttribute('title', '3')
  await expect(page.getByTestId('suggestion-count-error')).toContainText('保留上次成功计数 3')
  await page.getByRole('button', { name: '重试待处理数量', exact: true }).click()
  await expect(page.getByTestId('suggestion-count-error')).toHaveCount(0)
  await expect(badge).toHaveCount(0)
  await expect.poll(() => reads).toBe(3)
})

test('an older pending count response cannot overwrite a newer counts-changed result', async ({
  page
}) => {
  await mockSuggestionCountPage(page)
  let release!: () => void
  let reads = 0
  let ignored = false
  const held = new Promise<void>((resolve) => (release = resolve))
  await page.route('**/api/corporate-actions/suggestions/count', async (route) => {
    const first = ++reads === 1
    if (first) await held
    return route.fulfill({ json: { total: first ? 3 : 0 } })
  })
  await page.route('**/api/corporate-actions/suggestions?**', (route) =>
    route.fulfill({ json: ignored ? [] : [countSuggestion] })
  )
  await page.route('**/api/corporate-actions/suggestions/993/ignore', (route) => {
    ignored = true
    return route.fulfill({ json: { ...countSuggestion, status: 'IGNORED' } })
  })
  await page.goto('/corporate-actions')
  await expect.poll(() => reads).toBe(1)
  await page.getByRole('tab', { name: /预计与待收股息/ }).click()
  await page.getByRole('button', { name: /^忽略 UI-COUNT/ }).click()
  await expect(page.getByTestId('suggestion-count-status')).toHaveCount(0)
  await expect.poll(() => reads).toBe(2)
  release()
  await page.waitForLoadState('networkidle')
  await expect(page.locator('.tab-badge')).toHaveCount(0)
  await expect(page.getByTestId('suggestion-count-status')).toHaveCount(0)
})
test('late company action summaries cannot replace the current market or turn unknown tax into zero', async ({
  page,
  request
}) => {
  let release!: () => void
  let arrived = false
  const held = new Promise<void>((resolve) => (release = resolve))
  await page.route('**/api/corporate-actions?**', (route) =>
    route.fulfill({ json: [row(new URL(route.request().url()).searchParams.get('market') || '')] })
  )
  await page.route('**/api/corporate-actions/count**', (route) =>
    route.fulfill({ json: { total: 1 } })
  )
  await page.route('**/api/corporate-actions/statistics/summary**', async (route) => {
    const market = new URL(route.request().url()).searchParams.get('market')
    if (market === 'A股') {
      arrived = true
      await held
    }
    await route.fulfill({ json: summary(market === '港股' ? 22.22 : 111.11) })
  })
  await setAuthenticatedSession(page, await loginThroughApi(request))
  await page.goto('/corporate-actions')
  const market = page.getByRole('combobox', { name: '筛选市场', exact: true })
  const net = page.locator('.financial-statistic').filter({ hasText: '税后净额' })
  await expect(net).toContainText('¥111.11')
  await market.selectOption('A股')
  await expect.poll(() => arrived).toBe(true)
  await market.selectOption('港股')
  await expect(net).toContainText('¥22.22')
  release()
  await page.waitForLoadState('networkidle')
  await expect(net).toContainText('¥22.22')
  await expect(
    page.locator('.records-table').getByText('虚构乙公司', { exact: true })
  ).toBeVisible()
  await expect(
    page.locator('.financial-statistic').filter({ hasText: '股息总额' }).locator('strong')
  ).toHaveText('—')
  await expect(
    page.locator('.financial-statistic').filter({ hasText: '预扣税' }).locator('strong')
  ).toHaveText('—')
})
test('a failed company action request stays unknown until a successful empty response', async ({
  page,
  request
}) => {
  let fail = true
  await page.route('**/api/corporate-actions?**', (route) =>
    route.fulfill(fail ? { status: 503, json: { detail: '虚构记录暂不可用' } } : { json: [] })
  )
  await page.route('**/api/corporate-actions/count**', (route) =>
    route.fulfill({ json: { total: 0 } })
  )
  await page.route('**/api/corporate-actions/statistics/summary**', (route) =>
    route.fulfill(
      fail
        ? { status: 503, json: { detail: '虚构汇总暂不可用' } }
        : {
            json: {
              total_count: 0,
              cash_dividends: { total_dividend: 0, total_tax: 0, net_dividend: 0 }
            }
          }
    )
  )
  await setAuthenticatedSession(page, await loginThroughApi(request))
  await page.goto('/corporate-actions')
  await expect(page.getByTestId('action-load-error')).toContainText('数量与记录暂不可用')
  await expect(page.getByTestId('action-summary-error')).toContainText('股息总额未知')
  await expect(page.getByTestId('action-result-count')).toHaveText('— 条')
  await expect(page.getByText('暂无公司行动记录', { exact: true })).toHaveCount(0)
  fail = false
  await page.getByRole('button', { name: '重试记录', exact: true }).click()
  await expect(page.getByTestId('action-result-count')).toHaveText('0 条')
  await expect(page.getByText('暂无公司行动记录', { exact: true })).toBeVisible()
  await expect(
    page.locator('.financial-statistic').filter({ hasText: '股息总额' }).locator('strong')
  ).toHaveText('¥0.00')
})
test('narrow receipt candidates distinguish failure from empty and return focus on cancel', async ({
  page,
  request
}) => {
  await page.setViewportSize({ width: 320, height: 852 })
  let failing = true
  await page.route('**/api/corporate-actions/suggestions?**', (route) =>
    route.fulfill({
      json: [
        {
          id: 992,
          symbol: 'UI-ONLY',
          name: '虚构核对样例：长名称账户分次到账与公告派发币种核对',
          market: '港股',
          currency: 'HKD',
          action_type: 'CASH_DIVIDEND',
          cash_div_pre_tax: null,
          ex_date: '2026-01-02',
          status: 'NEW',
          receipt_state: 'NEEDS_REVIEW',
          review_reason: 'amount',
          receipt_ids: [],
          receipt_complete: false,
          updated_at: '2026-01-03T00:00:00Z'
        }
      ]
    })
  )
  await page.route('**/api/corporate-actions/suggestions/992/receipts', (route) =>
    route.fulfill(failing ? { status: 503, json: { detail: '虚构候选暂不可用' } } : { json: [] })
  )
  await setAuthenticatedSession(page, await loginThroughApi(request))
  await page.goto('/corporate-actions')
  await page.getByRole('tab', { name: /预计与待收股息/ }).click()
  const card = page.getByTestId('dividend-suggestion-card')
  await expect(card.locator('.suggestion-amount > span')).toHaveText('—')
  const trigger = card.getByRole('button', { name: /^核对到账 UI-ONLY/ })
  await trigger.focus()
  await trigger.press('Enter')
  const dialog = page.getByRole('dialog', { name: '核对股息到账', exact: true })
  await expect(dialog.getByTestId('receipt-load-error')).toContainText('暂不能确认或保存关联')
  await expect(dialog.getByRole('button', { name: '保存核对', exact: true })).toBeDisabled()
  const complete = dialog.getByRole('checkbox', {
    name: '我已核对，本次公告股息已全部到账',
    exact: true
  })
  await expect(complete).toBeDisabled()
  await expect(complete).not.toBeChecked()
  await expect(dialog.getByText(/暂无可关联到账/)).toHaveCount(0)
  await expect
    .poll(async () => {
      const box = await dialog.boundingBox()
      return (
        box !== null &&
        box.x >= 0 &&
        box.y >= 0 &&
        box.x + box.width <= 320 &&
        box.y + box.height <= 852
      )
    })
    .toBe(true)
  failing = false
  await dialog.getByRole('button', { name: '重试到账候选', exact: true }).click()
  await expect(dialog.getByText(/暂无可关联到账/)).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(trigger).toBeFocused()
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBe(320)
})
