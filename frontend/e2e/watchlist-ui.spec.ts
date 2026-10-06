import { expect, test } from '@playwright/test'
import { loginThroughApi, setAuthenticatedSession } from './helpers'

const row = {
  id: 901,
  symbol: 'UIAUTO',
  market: '美股',
  name: '明确虚构自动重读样例',
  note: '明确虚构原备注',
  current_price: '0.0851',
  price_as_of: '2026-01-01',
  price_source: 'fictional-source',
  price_updated_at: '2026-01-01T09:00:00Z',
  added_price: '0.08',
  added_price_date: '2026-01-01',
  added_price_basis: 'quote',
  change_since_added_pct: 0.06375,
  created_at: '2026-01-01T09:00:00Z',
  graham_summary: null
}

test('自动503不弹错误通知，手动503仍提示且保持全局服务状态', async ({ page, request }) => {
  await setAuthenticatedSession(page, await loginThroughApi(request))
  let reads = 0
  await page.route('**/api/watchlist', (route) => {
    reads++
    return route.fulfill(
      reads === 1
        ? { json: [row] }
        : {
            status: 503,
            json: { detail: '明确虚构后台读取失败' }
          }
    )
  })
  await page.goto('/watchlist')
  const main = page.getByRole('main')
  await expect(main).toContainText(row.note)
  const automaticFailure = page.waitForResponse(
    (response) => new URL(response.url()).pathname === '/api/watchlist' && response.status() === 503
  )
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  await automaticFailure
  await expect(page.locator('.status-overlay')).toContainText('明确虚构后台读取失败')
  await expect(page.locator('.el-notification')).toHaveCount(0)
  await expect(main).not.toContainText('观察清单加载失败')
  await expect(main).toContainText(row.note)
  await main.getByRole('button', { name: '重新加载', exact: true }).click()
  await expect(page.locator('.el-notification__content')).toContainText('明确虚构后台读取失败')
  await expect(main).toContainText('显示上次成功加载的观察标的，尚未确认最新结果')
  await expect(main).toContainText(row.note)
  expect(reads).toBe(3)
})

test('观察清单首次失败保持未知，刷新失败保留旧数据，成功空结果才显示暂无', async ({
  page,
  request
}) => {
  await setAuthenticatedSession(page, await loginThroughApi(request))
  let reads = 0
  await page.route('**/api/watchlist', (route) => {
    reads++
    return route.fulfill(
      reads === 1 || reads === 3
        ? { status: 503, json: { detail: '明确虚构的观察读取失败' } }
        : { json: reads === 2 ? [row] : [] }
    )
  })
  await page.goto('/watchlist')
  const main = page.getByRole('main')
  await expect(main).toContainText('尚未确认观察标的，请重试')
  await expect(main).not.toContainText('暂无观察标的')
  await expect(main).not.toContainText('0 个观察标的')
  await main.getByRole('button', { name: '重试加载', exact: true }).click()
  await expect(main).toContainText(row.note)
  await expect(main).toContainText('1 个观察标的')
  await main.getByRole('button', { name: '重新加载', exact: true }).click()
  await expect(main).toContainText('显示上次成功加载的观察标的，尚未确认最新结果')
  await expect(main).toContainText(row.note)
  await expect(main).toContainText('1 个观察标的 · 上次成功加载')
  await main.getByRole('button', { name: '重试加载', exact: true }).click()
  await expect(main).toContainText('暂无观察标的；点击添加观察')
  await expect(main).toContainText('0 个观察标的')
  expect(reads).toBe(4)
})

test('迟到自动读取不能覆盖已保存备注，编辑时暂停自动重读且静默失败保留数据', async ({
  page,
  request
}) => {
  await setAuthenticatedSession(page, await loginThroughApi(request))
  let current = { ...row }
  let reads = 0
  const payloads: unknown[] = []
  let releaseOld!: () => void
  const gate = new Promise<void>((resolve) => {
    releaseOld = resolve
  })
  await page.route('**/api/watchlist', async (route) => {
    const snapshot = { ...current }
    reads++
    if (reads === 2) await gate
    if (reads === 4) return route.fulfill({ status: 503, json: { detail: '明确虚构静默读取失败' } })
    return route.fulfill({ json: [snapshot] })
  })
  await page.route('**/api/watchlist/901', async (route) => {
    expect(route.request().method()).toBe('PUT')
    const payload = route.request().postDataJSON()
    payloads.push(payload)
    current = { ...current, ...payload }
    return route.fulfill({ json: current })
  })
  await page.goto('/watchlist')
  const main = page.getByRole('main')
  await expect(main).toContainText(row.note)
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  await expect.poll(() => reads).toBe(2)
  await main.getByRole('button', { name: '编辑 UIAUTO 观察标的', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '编辑观察标的', exact: true })
  await dialog.locator('textarea').fill('明确虚构的新备注')
  await dialog.getByTestId('watchlist-submit').click()
  await expect(dialog).toBeHidden()
  await expect(main).toContainText('明确虚构的新备注')
  const oldResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname === '/api/watchlist'
  )
  releaseOld()
  await oldResponse
  await expect(main).not.toContainText(row.note)
  await expect(main).toContainText('明确虚构的新备注')
  expect(payloads).toEqual([{ name: row.name, note: '明确虚构的新备注' }])
  await main.getByRole('button', { name: '编辑 UIAUTO 观察标的', exact: true }).click()
  await expect(dialog).toBeVisible()
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  expect(reads).toBe(3)
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await expect(dialog).toBeHidden()
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  await expect.poll(() => reads).toBe(4)
  await expect(main).toContainText('明确虚构的新备注')
  await expect(main).not.toContainText('观察清单加载失败')
  await expect(main.getByRole('button', { name: '重新加载', exact: true })).toBeEnabled()
})

test('最新静默读取接管首次手动读取后解除加载，迟到首次响应不能污染新数据', async ({
  page,
  request
}) => {
  await setAuthenticatedSession(page, await loginThroughApi(request))
  let reads = 0
  let releaseOld!: () => void
  const gate = new Promise<void>((resolve) => {
    releaseOld = resolve
  })
  await page.route('**/api/watchlist', async (route) => {
    reads++
    const first = reads === 1
    if (first) await gate
    return route.fulfill({ json: first ? [] : [row] })
  })
  await page.goto('/watchlist')
  await expect.poll(() => reads).toBe(1)
  const reload = page.getByRole('button', { name: '重新加载', exact: true })
  await expect(reload).toBeDisabled()
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  await expect.poll(() => reads).toBe(2)
  await expect(page.getByRole('main')).toContainText(row.note)
  await expect(reload).toBeEnabled()
  const oldResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname === '/api/watchlist'
  )
  releaseOld()
  await oldResponse
  await expect(page.getByRole('main')).toContainText(row.note)
  await expect(page.getByRole('main')).not.toContainText('暂无观察标的')
  await expect(reload).toBeEnabled()
})
