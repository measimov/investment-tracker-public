import { expect, test } from '@playwright/test'
import { loginThroughApi, setAuthenticatedSession } from './helpers'

const transaction = {
  id: 900001,
  symbol: 'SMALL',
  name: '演示低价标的',
  market: '港股',
  transaction_type: 'BUY',
  quantity: '1000',
  price: '0.085',
  fee: '0.1234',
  currency: 'HKD',
  transaction_date: '2026-01-03',
  broker_account_id: null,
  notes: '浏览器拦截演示，不写入账本',
  read_only: false
}

test('transaction load states distinguish unknown, empty and retained results after changed-filter failure', async ({
  page,
  request
}) => {
  await setAuthenticatedSession(page, await loginThroughApi(request))
  let mode: 'failed' | 'empty' | 'rows' = 'failed'
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  let blocked = true
  await page.route('**/api/transactions?*', async (route) => {
    if (blocked) await gate
    if (mode === 'failed') return route.fulfill({ status: 503, json: { detail: '演示加载失败' } })
    return route.fulfill({ json: mode === 'rows' ? [transaction] : [] })
  })
  await page.route('**/api/transactions/count*', async (route) => {
    if (blocked) await gate
    if (mode === 'failed') return route.fulfill({ status: 503, json: { detail: '演示加载失败' } })
    return route.fulfill({ json: { total: mode === 'rows' ? 1 : 0 } })
  })
  await page.goto('/transactions')
  await expect(page.locator('.transactions-page')).toHaveAttribute('aria-busy', 'true')
  await expect(page.getByTestId('transaction-result-count')).toHaveText('—')
  await expect(page.getByText('暂无交易记录', { exact: true })).toHaveCount(0)
  blocked = false
  release()
  await expect(page.getByTestId('transaction-load-error')).toContainText('数量与记录暂不可用')
  await expect(page.getByText('暂无交易记录', { exact: true })).toHaveCount(0)
  mode = 'empty'
  await page.getByTestId('transaction-load-error').getByRole('button', { name: '重试' }).click()
  await expect(page.getByTestId('transaction-result-count')).toHaveText('0')
  await expect(page.getByText('暂无交易记录', { exact: true })).toBeVisible()
  mode = 'rows'
  await page.getByRole('button', { name: '查询', exact: true }).click()
  await expect(page.getByTestId('transaction-result-count')).toHaveText('1')
  await expect(page.getByTestId('transactions-table')).toContainText('0.085')
  mode = 'failed'
  await page.getByRole('combobox', { name: '筛选市场', exact: true }).selectOption('美股')
  await expect(page.getByTestId('transaction-load-error')).toContainText('当前筛选结果尚未确认')
  await expect(page.getByTestId('transaction-result-count')).toHaveText('—')
  await expect(page.getByTestId('transactions-table')).toContainText('演示低价标的')
  await expect(page.getByTestId('transactions-table')).toContainText('0.1234')
  await expect(
    page.locator('.pagination-bar').getByRole('button', { name: '下一页' })
  ).toBeDisabled()
})

test('transaction rows preserve precision, native links and read-only or paired-transfer actions on narrow and desktop views', async ({
  page,
  request
}) => {
  await setAuthenticatedSession(page, await loginThroughApi(request))
  const rows = [
    {
      ...transaction,
      name: '长名称'.repeat(35),
      quantity: '1234567890.1234',
      broker_account_id: 987654
    },
    { ...transaction, id: 900002, symbol: 'READONLY', read_only: true },
    {
      ...transaction,
      id: 900003,
      symbol: 'TRANSFER',
      transaction_type: 'TRANSFER_OUT',
      linked_transaction_id: 900004
    }
  ]
  await page.route('**/api/transactions?*', (route) => route.fulfill({ json: rows }))
  await page.route('**/api/transactions/count*', (route) =>
    route.fulfill({ json: { total: rows.length } })
  )
  for (const width of [1440, 393, 320]) {
    await page.setViewportSize({ width, height: 852 })
    await page.goto('/transactions')
    const entries =
      width > 640 ? page.locator('.n-data-table-tbody tr') : page.getByTestId('transaction-card')
    await expect(entries).toHaveCount(3)
    const normal = entries.filter({ hasText: 'SMALL' })
    await expect(normal).toContainText('0.085')
    await expect(normal).toContainText('0.1234')
    await expect(normal).toContainText('HKD')
    await expect(normal).toContainText('已删除账户')
    await expect(normal.getByTestId('transaction-symbol-link')).toHaveAttribute(
      'href',
      /\/holdings\?.*symbol=SMALL/
    )
    const readOnly = entries.filter({ hasText: 'READONLY' })
    if (width > 640) {
      const readOnlyTag = readOnly.locator('.n-tag', { hasText: /^只读$/ })
      await expect(readOnlyTag).toBeVisible()
      await expect(readOnlyTag).toHaveAttribute('title', '对账单导入 · 只读')
      await expect(readOnly).not.toContainText('对账单导入 · 只读')
    } else {
      await expect(readOnly).toContainText('对账单导入 · 只读')
    }
    await expect(readOnly.getByRole('button', { name: /^编辑/ })).toHaveCount(0)
    await expect(readOnly.getByRole('button', { name: /^删除/ })).toHaveCount(0)
    const transfer = entries.filter({ hasText: 'TRANSFER' })
    await expect(transfer.getByRole('button', { name: /^编辑/ })).toHaveCount(0)
    const deletion = transfer.getByRole('button', { name: /^删除/ })
    await deletion.click()
    const confirmation = page.getByRole('dialog', { name: '删除交易' })
    await expect(confirmation).toContainText('同时删除配对的另一腿')
    await confirmation.getByRole('button', { name: '取消', exact: true }).click()
    await expect(confirmation).toBeHidden()
    await expect
      .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
      .toBe(true)
    await page.screenshot({
      path: test.info().outputPath(`transaction-content-${width}.png`),
      fullPage: true
    })
  }
})

for (const width of [1440, 393]) {
  test(`transaction whole-page pagination and on-demand notes retain truthful results at ${width}px`, async ({
    page
  }) => {
    await page.setViewportSize({ width, height: 852 })
    const fakeUser = {
      id: 9999,
      username: '明确虚构分页验收用户',
      email: null,
      is_active: true,
      is_admin: false
    }
    await page.addInitScript((user) => {
      localStorage.setItem('user', JSON.stringify(user))
    }, fakeUser)
    const noteFor = (symbol: string) =>
      `${symbol} 的明确虚构交易备注。\n第二段保留换行及低价 0.085。\n${'仅用于分页与展开验收，不写入真实账本。'.repeat(15)}`
    const rowsFor = (pageNumber: number) =>
      Array.from({ length: 50 }, (_, index) => {
        const symbol = `PAGE${pageNumber}_${String(index + 1).padStart(2, '0')}`
        return {
          ...transaction,
          id: pageNumber * 1000 + index,
          symbol,
          name: `虚构标的 ${symbol}`,
          notes: noteFor(symbol),
          read_only: true
        }
      })
    const firstPage = rowsFor(1)
    const secondPage = rowsFor(2)
    const scriptErrors: string[] = []
    page.on('pageerror', (error) => scriptErrors.push(error.message))
    const blockedWrites: string[] = []
    const unhandled: string[] = []
    let failNext = false
    let total = 150
    await page.route('**/api/**', (route) => {
      const request = route.request()
      const url = new URL(request.url())
      const path = url.pathname
      if (!path.startsWith('/api/')) return route.continue()
      if (path === '/api/auth/refresh') return route.fulfill({ json: { user: fakeUser } })
      if (request.method() !== 'GET') {
        blockedWrites.push(`${request.method()} ${path}`)
        return route.abort()
      }
      if (path === '/api/auth/me') return route.fulfill({ json: fakeUser })
      if (path === '/api/broker-accounts') return route.fulfill({ json: [] })
      if (path === '/api/transactions/count') return route.fulfill({ json: { total } })
      if (path === '/api/transactions') {
        const skip = Number(url.searchParams.get('skip') || 0)
        if (failNext && skip === 100)
          return route.fulfill({ status: 503, json: { detail: '明确虚构下一页读取失败' } })
        return route.fulfill({ json: skip === 0 ? firstPage : secondPage })
      }
      if (path === '/api/capabilities')
        return route.fulfill({
          json: {
            opinions: { available: false, reason: 'unconfigured' },
            xueqiu_symbol_feed: { available: false, reason: 'unconfigured' }
          }
        })
      if (path === '/api/securities/active-analysis-jobs') return route.fulfill({ json: [] })
      unhandled.push(path)
      return route.abort()
    })
    await page.goto('/transactions')
    const entries = () =>
      width > 640
        ? page.getByTestId('transactions-table').locator('.n-data-table-tbody > tr')
        : page.getByTestId('transaction-card')
    await expect(entries()).toHaveCount(50)
    const topPager = page.getByTestId('transaction-top-pagination')
    await expect(
      page.locator('.detail-heading').getByTestId('transaction-top-pagination')
    ).toBeVisible()
    await expect(topPager.getByRole('button', { name: '上一页' })).toBeDisabled()
    if (width <= 640) {
      const filterToggle = page
        .locator('.detail-heading')
        .getByRole('button', { name: '筛选', exact: true })
      await expect(filterToggle).toHaveAttribute('aria-controls', 'transaction-filters')
      await expect(filterToggle).toHaveAttribute('aria-expanded', 'false')
      await expect(page.locator('#transaction-filters')).toBeHidden()
      await filterToggle.click()
      const collapseFilters = page
        .locator('.detail-heading')
        .getByRole('button', { name: '收起筛选', exact: true })
      await expect(collapseFilters).toHaveAttribute('aria-expanded', 'true')
      await expect(page.locator('#transaction-filters')).toBeVisible()
      await collapseFilters.click()
      await expect(filterToggle).toHaveAttribute('aria-expanded', 'false')
      await expect(page.locator('#transaction-filters')).toBeHidden()
    }
    await expect(page.locator('[data-testid="transaction-notes"]:visible')).toHaveCount(0)
    const first = entries().first()
    await expect(first).toContainText('PAGE1_01')
    await expect(first).toContainText('0.085')
    await expect(first).toContainText('0.1234')
    const last = entries().last()
    await last.scrollIntoViewIfNeeded()
    await expect(first).not.toBeInViewport()
    expect(await page.evaluate(() => document.documentElement.scrollHeight > innerHeight)).toBe(
      true
    )
    // Full notes must be available from the row without rendering fifty expanded paragraphs.
    const openNotes = async (entry: ReturnType<typeof entries>) => {
      await entry.getByRole('button', { name: /交易备注/ }).click()
    }
    if (width <= 640) {
      const trigger = last.getByRole('button', { name: /交易备注/ })
      await trigger.focus()
      const closedBounds = await trigger.boundingBox()
      await trigger.press('Enter')
      await expect(trigger).toHaveAttribute('aria-expanded', 'true')
      const openBounds = await trigger.boundingBox()
      expect(openBounds?.x).toBe(closedBounds?.x)
      expect(openBounds?.y).toBe(closedBounds?.y)
      await trigger.press('Space')
      await expect(trigger).toHaveAttribute('aria-expanded', 'false')
      await expect(last.getByTestId('transaction-notes')).toHaveCount(0)
    }
    await openNotes(last)
    const oldNote = page.locator('[data-testid="transaction-notes"]:visible').first()
    await expect(oldNote).toContainText(noteFor('PAGE1_50'))
    const original = width > 640 ? oldNote.locator('p') : oldNote
    expect(await original.textContent()).toBe(noteFor('PAGE1_50'))
    const bottom = page.locator('.pagination-bar')
    await bottom.getByRole('button', { name: '下一页' }).click()
    await expect(entries().first()).toContainText('PAGE2_01')
    await expect(entries().first()).toBeInViewport()
    await expect(topPager).toBeInViewport()
    await expect(page.getByTestId('transaction-list-top')).toBeFocused()
    await expect(page.getByTestId('transaction-top-pagination')).toContainText(
      width > 640 ? '第 2 / 3 页' : '2 / 3'
    )
    await expect(entries()).toHaveCount(50)
    await expect(page.locator('[data-testid="transaction-notes"]:visible')).toHaveCount(0)
    await expect(page.getByText(noteFor('PAGE1_50'), { exact: false })).toHaveCount(0)
    await openNotes(entries().first())
    await expect(page.locator('[data-testid="transaction-notes"]:visible').first()).toContainText(
      noteFor('PAGE2_01')
    )
    failNext = true
    await bottom.getByRole('button', { name: '下一页' }).click()
    await expect(page.getByTestId('transaction-load-error')).toContainText('保留上次成功查询的数据')
    await expect(entries().first()).toContainText('PAGE2_01')
    await expect(page.getByTestId('transaction-top-pagination')).toContainText('页码尚未确认')
    await expect(page.getByTestId('transaction-top-pagination')).not.toContainText('第 3 / 3 页')
    await expect(entries().filter({ hasText: 'PAGE3_' })).toHaveCount(0)
    await expect(page.getByTestId('transaction-result-count')).toHaveText('—')
    await expect(bottom.getByRole('button', { name: '下一页' })).toBeDisabled()
    failNext = false
    total = 50
    await page.getByTestId('transaction-load-error').getByRole('button', { name: '重试' }).click()
    await expect(entries().first()).toContainText('PAGE1_01')
    await expect(page.getByTestId('transaction-result-count')).toHaveText('50')
    await expect(topPager).toHaveCount(0)
    await expect(bottom).toContainText('共 50 条')
    expect(scriptErrors).toEqual([])
    expect(blockedWrites).toEqual([])
    expect(unhandled).toEqual([])
  })
}
