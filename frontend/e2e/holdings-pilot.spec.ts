import { expect, test, type Page } from '@playwright/test'
import {
  BATCH_HOLDINGS,
  loginThroughApi,
  mockHoldingsPage,
  setAuthenticatedSession
} from './helpers'

async function expectHoldingsCount(page: Page, text: string) {
  const help = page.getByRole('button', { name: '查看持仓成本与盈亏口径' })
  await help.click()
  await expect(page.getByTestId('holdings-count')).toContainText(text)
  await help.press('Escape')
  await expect(page.locator('.n-popover')).toHaveCount(0)
}

// WebKit 的滚动条可能让 CSS 与 matchMedia 在临界宽度上暂时不一致。
// Chromium CI 重现这两个判断的分歧，验证明细和操作仍然可见。
for (const [width, mobile] of [
  [633, false],
  [641, true]
] as const) {
  test(`holdings stay usable when CSS and media query disagree (${width}px)`, async ({
    page,
    request
  }) => {
    await page.setViewportSize({ width, height: 1000 })
    await page.addInitScript((matches) => {
      const matchMedia = window.matchMedia.bind(window)
      window.matchMedia = (query) => {
        const result = matchMedia(query)
        if (query === '(max-width: 640px)')
          Object.defineProperty(result, 'matches', { get: () => matches })
        return result
      }
    }, mobile)
    await setAuthenticatedSession(page, await loginThroughApi(request))
    await mockHoldingsPage(page, [{ ...BATCH_HOLDINGS[0], broker_account_id: 9 }])
    await page.route('**/api/broker-accounts*', (route) =>
      route.fulfill({
        json: [9, 10].map((id) => ({
          id,
          broker_name: '虚构券商',
          account_name: `虚构账户${id}`,
          account_number: '',
          is_active: true
        }))
      })
    )
    await page.route('**/api/securities/industries*', (route) =>
      route.fulfill({
        json: [{ symbol: 'BAT001', market: 'A股', industry: '虚构行业', source: 'rule' }]
      })
    )
    await page.goto('/holdings')
    await expectHoldingsCount(page, '1 只持仓')
    const account = page.getByRole('textbox', { name: '筛选账户', exact: true })
    await account.fill('虚构')
    await account.press('Escape')
    await page.getByTestId('holdings-tag-filter').click()
    const tag = page.getByTestId('holdings-tag-filter').locator('input')
    await tag.fill('虚构')
    await tag.press('Escape')
    await page.getByRole('button', { name: '按账户', exact: true }).click()
    await expect(page.getByRole('link', { name: /批量测试A/ }).first()).toBeVisible()
    await page
      .getByRole('button', { name: mobile ? '转仓到其他账户' : '转仓', exact: true })
      .click()
    await expect(page.getByTestId('transfer-dialog')).toBeVisible()
    await page.getByRole('button', { name: '取消', exact: true }).click()
  })
}

for (const width of [320, 1440]) {
  test(`unconfirmed and failed prices preserve valuation; confirmed prices update all accounts (${width}px)`, async ({
    page,
    request
  }) => {
    await page.setViewportSize({ width, height: 852 })
    await setAuthenticatedSession(page, await loginThroughApi(request))
    const holdings = [null, 9].map((account, index) => ({
      ...BATCH_HOLDINGS[0],
      id: 96001 + index,
      user_id: 2,
      symbol: 'UICONF',
      name: 'UI明确虚构待确认价格',
      broker_account_id: account,
      quantity: '100',
      avg_cost: '10',
      total_cost: '1000',
      unknown_cost_quantity: '0',
      current_price: '10',
      price_source: 'tencent-quote',
      price_as_of: '2026-01-05' as string | null,
      price_updated_at: '2026-01-05T00:00:00Z',
      updated_at: '2026-01-05T00:00:00Z'
    }))
    await mockHoldingsPage(page, holdings)
    let writes = 0,
      fail = true,
      release!: () => void
    let pending = new Promise<void>((resolve) => (release = resolve))
    await page.route('**/api/holdings/*/price', async (route) => {
      writes++
      expect(route.request().method()).toBe('PUT')
      expect(route.request().postDataJSON()).toEqual({ current_price: 12.3456 })
      await pending
      if (fail)
        return route.fulfill({ status: 503, json: { detail: '明确虚构价格保存失败，后端仍为10' } })
      for (const holding of holdings) {
        holding.current_price = '12.3456'
        holding.price_source = 'manual'
        holding.price_as_of = null
        holding.price_updated_at = '2026-10-03T00:00:00Z'
      }
      return route.fulfill({ json: holdings[0] })
    })
    await page.goto('/holdings')
    const total = page.getByTestId('holdings-total-value')
    await expect(total).toHaveText('¥2,000.00')
    await page.getByRole('button', { name: '按账户', exact: true }).click()
    const displays = page.getByTestId('price-display')
    await expect(displays).toHaveText(['10.00', '10.00'])
    await displays.first().press('Enter')
    const input = page.getByTestId('price-input').locator('input')
    await input.fill('12.3456')
    await input.press('Enter')
    await expect(page.getByTestId('price-input')).toHaveCount(0)
    await expect.poll(() => writes).toBe(1)
    await expect(displays).toHaveText(['10.00', '10.00'])
    await expect(total).toHaveText('¥2,000.00')
    release()
    await expect(page.locator('#app')).toContainText('明确虚构价格保存失败，后端仍为10')
    await expect(displays).toHaveText(['10.00', '10.00'])
    await expect(total).toHaveText('¥2,000.00')
    fail = false
    pending = new Promise<void>((resolve) => (release = resolve))
    await displays.first().press('Enter')
    await input.fill('12.3456')
    await input.press('Enter')
    await expect.poll(() => writes).toBe(2)
    await expect(displays).toHaveText(['10.00', '10.00'])
    await expect(total).toHaveText('¥2,000.00')
    release()
    await expect(displays).toHaveText(['12.3456', '12.3456'])
    await expect(total).toHaveText('¥2,469.12')
    await expect(page.getByText('手工', { exact: true })).toHaveCount(2)
    await displays.first().press('Enter')
    await input.fill('99')
    await input.press('Escape')
    await expect(displays.first()).toBeFocused()
    expect(writes).toBe(2)
    await expect(total).toHaveText('¥2,469.12')
  })
}

for (const width of [320, 1440]) {
  for (const olderOutcome of ['success', 'failure']) {
    test(`${width}px changing back to the confirmed price preserves latest intent after older ${olderOutcome}`, async ({
      page,
      request
    }) => {
      await page.setViewportSize({ width, height: 852 })
      await setAuthenticatedSession(page, await loginThroughApi(request))
      const holdings = [null, 9].map((account, index) => ({
        ...BATCH_HOLDINGS[0],
        id: 96101 + index,
        user_id: 2,
        symbol: 'UIRETURN',
        name: 'UI明确虚构回原价',
        broker_account_id: account,
        quantity: '100',
        avg_cost: '10',
        total_cost: '1000',
        unknown_cost_quantity: '0',
        current_price: '10',
        price_source: 'manual',
        price_as_of: null,
        price_updated_at: '2026-01-05T00:00:00Z',
        updated_at: '2026-01-05T00:00:00Z'
      }))
      await mockHoldingsPage(page, holdings)
      let release!: () => void
      const held = new Promise<void>((resolve) => (release = resolve))
      const writes: number[] = []
      await page.route('**/api/holdings/*/price', async (route) => {
        expect(route.request().method()).toBe('PUT')
        const price = route.request().postDataJSON().current_price
        expect(route.request().postDataJSON()).toEqual({ current_price: price })
        writes.push(price)
        if (price === 20) {
          await held
          return olderOutcome === 'success'
            ? route.fulfill({ json: { ...holdings[0], current_price: '20' } })
            : route.fulfill({ status: 503, json: { detail: 'UI明确虚构旧20保存失败' } })
        }
        expect(price).toBe(10)
        return route.fulfill({ json: { ...holdings[0], current_price: '10' } })
      })
      try {
        await page.goto('/holdings')
        const total = page.getByTestId('holdings-total-value')
        await expect(total).toHaveText('¥2,000.00')
        await page.getByRole('button', { name: '按账户', exact: true }).click()
        const displays = page.getByTestId('price-display')
        const input = page.getByTestId('price-input').locator('input')
        await displays.first().press('Enter')
        await input.fill('20')
        await input.press('Enter')
        await expect.poll(() => writes).toEqual([20])
        await expect(displays).toHaveText(['10.00', '10.00'])
        await expect(total).toHaveText('¥2,000.00')
        const latest = page.waitForResponse(
          (response) =>
            response.url().endsWith('/price') &&
            response.request().postDataJSON().current_price === 10
        )
        await displays.first().press('Enter')
        await input.fill('10')
        await input.press('Enter')
        await expect.poll(() => writes).toEqual([20, 10])
        await (await latest).finished()
        const older = page.waitForResponse(
          (response) =>
            response.url().endsWith('/price') &&
            response.request().postDataJSON().current_price === 20
        )
        release()
        await (await older).finished()
        if (olderOutcome === 'failure')
          await expect(page.locator('#app')).toContainText('UI明确虚构旧20保存失败')
        else
          await page.evaluate(
            () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
          )
        await expect(displays).toHaveText(['10.00', '10.00'])
        await expect(total).toHaveText('¥2,000.00')
        await displays.first().press('Enter')
        await input.fill('99')
        await input.press('Escape')
        await expect(displays.first()).toBeFocused()
        expect(writes).toEqual([20, 10])
      } finally {
        release()
      }
    })
  }
}

test('accounting explanation remains fully readable when opened by keyboard on narrow screens', async ({
  page,
  request
}) => {
  await setAuthenticatedSession(page, await loginThroughApi(request))
  await mockHoldingsPage(page)
  for (const width of [320, 375, 393]) {
    await page.setViewportSize({ width, height: 852 })
    await page.goto('/holdings')
    await expectHoldingsCount(page, '3 只持仓')
    const help = page.getByRole('button', { name: '查看持仓成本与盈亏口径' })
    await help.focus()
    await help.press('Enter')
    const explanation = page.locator('.n-popover').filter({ hasText: '成本与市值均按今日汇率' })
    await expect(explanation).toBeVisible()
    await expect(explanation).toContainText('总收益一致，只是拆分归属不同')
    await expect
      .poll(async () => {
        const bounds = await explanation.boundingBox()
        return (
          !!bounds &&
          bounds.x >= 0 &&
          bounds.x + bounds.width <= width &&
          bounds.y >= 0 &&
          bounds.y + bounds.height <= 852
        )
      })
      .toBe(true)
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBe(width)
    await expect(help).toBeFocused()
    await help.press('Enter')
    await expect(explanation).toBeHidden()
  }
})

for (const width of [1440, 393]) {
  test(`holdings loading, failure and retry never imply zero holdings (${width}px)`, async ({
    page,
    request
  }) => {
    await page.setViewportSize({ width, height: 900 })
    await setAuthenticatedSession(page, await loginThroughApi(request))
    await mockHoldingsPage(page)
    let release!: () => void
    const pending = new Promise<void>((resolve) => (release = resolve))
    let failed = true
    await page.route('**/api/holdings*', async (route) => {
      await pending
      await route.fulfill(
        failed ? { status: 503, json: { detail: '持仓暂不可用' } } : { json: BATCH_HOLDINGS }
      )
    })
    await page.goto('/holdings?symbol=BAT001&market=A股')
    await expect(page.getByTestId('holdings-count')).toHaveText('正在读取持仓…')
    await expect(page.getByTestId('holdings-total-value')).toHaveText('—')
    await expect(page.getByTestId('holdings-not-held')).toHaveCount(0)
    await expect(page.getByTestId('market-subtotals')).toHaveCount(0)
    release()
    await expect(page.getByText('持仓数据加载失败', { exact: true })).toBeVisible()
    await expect(page.getByTestId('holdings-count')).toHaveText('加载失败，汇总暂不可用')
    await expect(page.getByText('数据尚未加载，请重试')).toBeVisible()
    await expect(page.getByText('暂无持仓数据', { exact: true })).toHaveCount(0)
    await expect(page.getByTestId('holdings-not-held')).toHaveCount(0)
    failed = false
    await page.getByRole('button', { name: '重试加载持仓' }).click()
    await expectHoldingsCount(page, '3 只持仓')
    await expect(page.getByText('持仓数据加载失败', { exact: true })).toHaveCount(0)
  })
}

test('real table keyboard sorting and account expansion preserve missing valuation and row-only search', async ({
  page,
  request
}) => {
  await setAuthenticatedSession(page, await loginThroughApi(request))
  await mockHoldingsPage(page, [
    BATCH_HOLDINGS[0],
    { ...BATCH_HOLDINGS[0], id: 4, broker_account_id: 9 },
    { ...BATCH_HOLDINGS[1], unknown_cost_quantity: 10 },
    { ...BATCH_HOLDINGS[2], current_price: null }
  ])
  await page.route('**/api/exchange-rates/latest', (route) =>
    route.fulfill({ json: { rates: { USD: 7 } } })
  )
  await page.goto('/holdings')
  const rows = page.getByTestId('holding-row')
  await expect(rows).toHaveCount(3)
  const total = page.getByTestId('holdings-total-value')
  await expect(total).toHaveText('¥3,500.00')
  await expect(page.getByText('1 只持仓暂无价格或缺汇率', { exact: false })).toBeVisible()
  await expect(rows.last()).toContainText('BATBTC')
  await expect(rows.filter({ hasText: 'BAT002' })).toContainText('成本未知 10')
  const sort = page.getByRole('button', { name: /市值 \/ 占比：按折人民币金额排序/ })
  await sort.focus()
  await sort.press('Enter')
  await expect(page.getByRole('columnheader', { name: /市值 \/ 占比/ })).toHaveAttribute(
    'aria-sort',
    'ascending'
  )
  await expect(rows.first()).toContainText('BAT002')
  await expect(rows.last()).toContainText('BATBTC')
  const expand = page.getByRole('button', { name: '展开 批量测试A 的账户明细' })
  await expand.focus()
  await expand.press('Space')
  await expect(page.getByTestId('holding-accounts')).toContainText('成本 ¥1,000.00')
  await expect(page.getByRole('button', { name: '收起 批量测试A 的账户明细' })).toBeFocused()
  await page.getByTestId('holdings-search').fill('BAT002')
  await expect(rows).toHaveCount(1)
  await expect(total).toHaveText('¥3,500.00')
  await expect(rows.getByRole('link')).toHaveAttribute('href', '/securities/A%E8%82%A1/BAT002')
  await page.getByRole('button', { name: '按账户', exact: true }).click()
  await expect(page.getByRole('columnheader', { name: /市值 \/ 占比/ })).toHaveAttribute(
    'aria-sort',
    'ascending'
  )
  await page.setViewportSize({ width: 393, height: 852 })
  await expect(page.getByTestId('holding-card')).toHaveCount(1)
  await page.setViewportSize({ width: 1440, height: 900 })
  await expect(page.getByRole('columnheader', { name: /市值 \/ 占比/ })).toHaveAttribute(
    'aria-sort',
    'ascending'
  )
})
