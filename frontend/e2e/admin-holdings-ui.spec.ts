import { expect, test, type Page } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { adminUser, loginThroughApi, setAuthenticatedSession } from './helpers'
const fixture = JSON.parse(
  readFileSync(new URL('../../docs/media/admin-holdings-ui-fixture.json', import.meta.url), 'utf8')
)
test.use({ timezoneId: 'Asia/Shanghai' })
async function selectUser(page: Page, index: number) {
  await page.getByRole('combobox', { name: '选择用户', exact: true }).focus()
  await page.keyboard.press('ArrowDown')
  await page.getByRole('option', { name: new RegExp(fixture.users[index].username) }).click()
}

test('管理员读取首次未知与真实空区分，原币/人民币汇总与缺价缺汇率保留', async ({
  page,
  request
}) => {
  await setAuthenticatedSession(page, await loginThroughApi(request, adminUser))
  let failed = true
  await page.route('**/api/users', (route) =>
    route.fulfill(
      failed ? { status: 503, json: { detail: '虚构用户目录失败' } } : { json: fixture.users }
    )
  )
  await page.route('**/api/exchange-rates/latest', (route) =>
    route.fulfill({ json: fixture.rates })
  )
  await page.route('**/api/holdings/admin/all', (route) =>
    route.fulfill(
      failed ? { status: 503, json: { detail: '虚构持仓失败' } } : { json: fixture.holdings }
    )
  )
  await page.goto('/admin/holdings')
  const summary = page.getByRole('region', { name: '管理员持仓汇总', exact: true })
  const detail = page.getByRole('region', { name: '管理员持仓明细', exact: true })
  await expect(detail).toContainText('持仓记录尚未加载成功')
  await expect(summary.locator('.financial-statistic-value')).toHaveText(['—', '—', '—', '—'])
  await expect(detail).not.toContainText('暂无持仓')
  failed = false
  await page.getByRole('button', { name: '重试用户目录', exact: true }).click()
  await page.getByRole('button', { name: '重试管理员持仓', exact: true }).click()
  await expect(summary.locator('.financial-statistic-value')).toHaveText([
    '6',
    '¥262.62',
    '¥237.65',
    '¥46.03'
  ])
  await expect(summary).toContainText('缺少 CHF 对人民币的汇率，1 个持仓未计入汇总')
  await expect(summary).toContainText('1 个持仓缺少现价，只计入总成本')
  const table = page.getByTestId('admin-holdings-table')
  await expect(table).toContainText('$20.00')
  await expect(table).toContainText('HK$32.00')
  await expect(table).toContainText('10.1234')
  await expect(table).toContainText('2.3456')
  await expect(table).toContainText('2026/10/03 07:30')
  await page.getByRole('combobox', { name: '选择用户', exact: true }).focus()
  await page.keyboard.press('ArrowDown')
  await expect(
    page.getByRole('option', { name: new RegExp(fixture.users[2].username) })
  ).toHaveCount(0)
  await page.keyboard.press('Escape')
})

test('切用户失败保留原成功身份，旧用户迟到503不污染后续成功，清空回汇总', async ({
  page,
  request
}) => {
  await setAuthenticatedSession(page, await loginThroughApi(request, adminUser))
  await page.route('**/api/users', (route) => route.fulfill({ json: fixture.users }))
  await page.route('**/api/exchange-rates/latest', (route) =>
    route.fulfill({ json: fixture.rates })
  )
  await page.route('**/api/holdings/admin/all', (route) =>
    route.fulfill({ json: fixture.holdings })
  )
  let user1Reads = 0,
    release!: () => void
  const pending = new Promise<void>((resolve) => (release = resolve))
  await page.route('**/api/holdings/admin/users/*', async (route) => {
    const id = Number(new URL(route.request().url()).pathname.split('/').at(-1))
    if (id === fixture.users[0].id && ++user1Reads === 1) {
      await pending
      return route.fulfill({ status: 503, json: { detail: '虚构旧用户失败' } })
    }
    return route.fulfill({
      json: fixture.holdings.filter((row: { user_id: number }) => row.user_id === id)
    })
  })
  await page.goto('/admin/holdings')
  await expect(page.getByTestId('admin-holdings-table')).toContainText('UIA')
  await selectUser(page, 0)
  await expect(page.getByText(/下方仍为上次成功范围：所有用户/)).toBeVisible()
  await selectUser(page, 1)
  await expect(page.getByTestId('admin-holdings-table')).not.toContainText('UIA')
  await expect(page.getByRole('region', { name: '管理员持仓明细' })).toContainText(
    fixture.users[1].username
  )
  release()
  await page.waitForLoadState('networkidle')
  await expect(page.getByRole('main')).not.toContainText('持仓记录加载失败')
  await expect(page.getByTestId('admin-holdings-table')).not.toContainText('UIA')
  const clear = page.locator('.user-select .el-select__clear')
  await page.locator('.user-select').hover()
  await clear.click()
  await expect(page.getByTestId('admin-holdings-table')).toContainText('UIA')
  await expect(page.locator('.user-select')).toContainText('所有用户（汇总）')
})

test('全部无覆盖为未知，真正零价仍零，真空确认后为零且不伪空', async ({ page, request }) => {
  await page.setViewportSize({ width: 320, height: 852 })
  await setAuthenticatedSession(page, await loginThroughApi(request, adminUser))
  await page.route('**/api/users', (route) => route.fulfill({ json: fixture.users }))
  await page.route('**/api/exchange-rates/latest', (route) =>
    route.fulfill({ json: fixture.rates })
  )
  let reads = 0
  const unconverted = fixture.holdings.find((row: { currency: string }) => row.currency === 'CHF')
  const zero = fixture.holdings.find((row: { current_price: string }) => row.current_price === '0')
  await page.route('**/api/holdings/admin/all', (route) =>
    route.fulfill({ json: ++reads === 1 ? [unconverted] : reads === 2 ? [zero] : [] })
  )
  await page.goto('/admin/holdings')
  const values = page
    .getByRole('region', { name: '管理员持仓汇总' })
    .locator('.financial-statistic-value')
  await expect(values).toHaveText(['1', '—', '—', '—'])
  await page.getByRole('button', { name: '重新加载管理员持仓', exact: true }).click()
  await expect(values.nth(2)).toHaveText('¥0.00')
  await expect(page.getByTestId('admin-holding-card').first()).toContainText('-100.00%')
  await page.getByRole('button', { name: '重新加载管理员持仓', exact: true }).click()
  await expect(values).toHaveText(['0', '¥0.00', '¥0.00', '¥0.00'])
  await expect(page.getByRole('region', { name: '管理员持仓明细' })).toContainText(
    '该范围暂无持仓记录'
  )
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320)
})

test('同SPA汇率再次失败保留缓存且持续标未确认，可只读重试恢复', async ({ page, request }) => {
  await setAuthenticatedSession(page, await loginThroughApi(request, adminUser))
  await page.route('**/api/users', (route) => route.fulfill({ json: fixture.users }))
  await page.route('**/api/holdings/admin/all', (route) =>
    route.fulfill({ json: fixture.holdings })
  )
  let rateReads = 0
  await page.route('**/api/exchange-rates/latest', (route) =>
    route.fulfill(
      ++rateReads === 2
        ? { status: 503, json: { detail: '虚构汇率失败' } }
        : { json: fixture.rates }
    )
  )
  await page.goto('/admin/holdings')
  await expect(page.getByRole('region', { name: '管理员持仓汇总' })).toContainText('¥262.62')
  await page
    .getByRole('navigation', { name: '主导航' })
    .getByRole('link', { name: '用户管理', exact: true })
    .click()
  await expect(page.getByRole('heading', { name: '用户管理', exact: true })).toBeVisible()
  await page
    .getByRole('navigation', { name: '主导航' })
    .getByRole('link', { name: '查看所有持仓', exact: true })
    .click()
  await expect(page.getByRole('main')).toContainText('外币折算暂使用已有汇率')
  await expect(page.getByRole('region', { name: '管理员持仓汇总' })).toContainText('¥262.62')
  await page.getByRole('button', { name: '重试汇率读取', exact: true }).click()
  await expect(page.getByRole('main')).not.toContainText('汇率加载失败')
  expect(rateReads).toBe(3)
})

test('窄屏完整用户名选项不越屏，键盘选择保留用户身份与查询', async ({ page, request }) => {
  await page.setViewportSize({ width: 320, height: 852 })
  await setAuthenticatedSession(page, await loginThroughApi(request, adminUser))
  await page.route('**/api/users', (route) => route.fulfill({ json: fixture.users }))
  await page.route('**/api/exchange-rates/latest', (route) =>
    route.fulfill({ json: fixture.rates })
  )
  await page.route('**/api/holdings/admin/all', (route) =>
    route.fulfill({ json: fixture.holdings })
  )
  let selectedId: number | undefined
  await page.route('**/api/holdings/admin/users/*', (route) => {
    selectedId = Number(new URL(route.request().url()).pathname.split('/').at(-1))
    return route.fulfill({
      json: fixture.holdings.filter((row: { user_id: number }) => row.user_id === selectedId)
    })
  })
  await page.goto('/admin/holdings')
  await expect(page.getByTestId('admin-holding-card').last()).toContainText('UIF')
  await page.getByRole('combobox', { name: '选择用户', exact: true }).focus()
  await page.keyboard.press('ArrowDown')
  const popup = page.locator('.el-select__popper:visible')
  await expect(popup).toBeVisible()
  await expect(
    page.getByRole('option', { name: new RegExp(fixture.users[0].username) })
  ).toContainText(fixture.users[0].email)
  await expect
    .poll(async () => {
      const box = await popup.boundingBox()
      return (
        !!box && box.x >= 0 && box.y >= 0 && box.x + box.width <= 320 && box.y + box.height <= 852
      )
    })
    .toBe(true)
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320)
  await page.keyboard.press('ArrowDown')
  await page.keyboard.press('Enter')
  await expect.poll(() => selectedId).toBe(fixture.users[0].id)
  await expect(page.getByRole('region', { name: '管理员持仓明细' })).toContainText(
    fixture.users[0].username
  )
})
