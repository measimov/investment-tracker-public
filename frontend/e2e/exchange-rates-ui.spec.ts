import { expect, test } from '@playwright/test'
import { adminUser, loginThroughApi, setAuthenticatedSession } from './helpers'
// 全部明确虚构响应；不调用真实汇率刷新。
const row = {
  id: 91001,
  from_currency: 'USD',
  to_currency: 'CNY',
  rate: '7.1234',
  effective_date: '2026-01-02',
  source: 'manual',
  is_active: true,
  created_at: '2026-01-01T23:30:00Z'
}
const inactive = { ...row, id: 91002, from_currency: 'GBP', rate: '9.1234', is_active: false }
const latest = {
  base_currency: 'CNY',
  rates: { CNY: '1', USD: '7.1234' },
  effective_date: '2026-01-02',
  source: 'manual',
  details: { USD: { rate: '7.1234', effective_date: '2026-01-02', source: 'manual' } }
}
const check = {
  check_date: '2026-01-02',
  from_currency: 'USD',
  to_currency: 'CNY',
  official_rate: '7.1234',
  official_date: '2026-01-02',
  official_source: 'cfets-ccpr',
  reference_rate: '7.1234',
  reference_source: 'api-ecb',
  diff_pct: '0'
}

test('汇率三读取首次失败未知，各自只读重试，刷新失败保留旧值，成功空结果才显示暂无', async ({
  page,
  request
}) => {
  await setAuthenticatedSession(page, await loginThroughApi(request))
  const counts = new Map<string, number>()
  await page.route('**/api/exchange-rates**', (route) => {
    const path = new URL(route.request().url()).pathname
    const count = (counts.get(path) || 0) + 1
    counts.set(path, count)
    expect(route.request().method()).toBe('GET')
    if (count === 1 || count === 3)
      return route.fulfill({ status: 503, json: { detail: '明确虚构汇率读取失败' } })
    return route.fulfill({
      json: path.endsWith('/latest')
        ? count === 2
          ? latest
          : { base_currency: 'CNY', rates: {}, effective_date: null, source: null }
        : path.endsWith('/source-checks')
          ? count === 2
            ? [check]
            : []
          : count === 2
            ? [row]
            : []
    })
  })
  await page.goto('/exchange-rates')
  const main = page.getByRole('main')
  await expect(main).toContainText('尚未确认当前汇率，请重试')
  await expect(main).toContainText('尚未确认比对记录，请重试')
  await expect(main).toContainText('尚未确认汇率历史，请重试')
  await expect(main).not.toContainText('暂无')
  for (const name of ['当前汇率', '汇率比对', '汇率历史'])
    await main.getByRole('button', { name: `重试${name}`, exact: true }).click()
  await expect(main.locator('.current-rates')).toContainText('7.1234')
  await expect(main.locator('.rate-checks')).toContainText('0.00%')
  await expect(main.locator('.rate-history')).toContainText('7.1234')
  for (const name of ['当前汇率', '汇率比对', '汇率历史'])
    await main.getByRole('button', { name: `重新加载${name}`, exact: true }).click()
  await expect(main.locator('.current-rates')).toContainText('显示上次成功加载的汇率')
  await expect(main.locator('.rate-checks')).toContainText('显示上次成功加载的比对记录')
  await expect(main.locator('.rate-history')).toContainText('显示上次成功加载的历史记录')
  await expect(main.locator('.rate-history')).toContainText('7.1234')
  for (const name of ['当前汇率', '汇率比对', '汇率历史'])
    await main.getByRole('button', { name: `重试${name}`, exact: true }).click()
  for (const text of ['暂无可用汇率', '暂无比对记录', '暂无汇率历史记录'])
    await expect(main).toContainText(text)
  expect([...counts.values()]).toEqual([4, 4, 4])
})

test('汇率停用筛选失败明确旧范围，迟到旧响应不覆盖当前成功范围', async ({ page, request }) => {
  await setAuthenticatedSession(page, await loginThroughApi(request, adminUser))
  await page.route('**/api/exchange-rates/latest', (route) => route.fulfill({ json: latest }))
  await page.route('**/api/exchange-rates/source-checks**', (route) =>
    route.fulfill({ json: [check] })
  )
  let reads = 0
  let releaseOld!: () => void
  const gate = new Promise<void>((resolve) => {
    releaseOld = resolve
  })
  await page.route(/\/api\/exchange-rates\?/, async (route) => {
    reads++
    const count = reads
    const includes = new URL(route.request().url()).searchParams.get('include_inactive') === 'true'
    if (count === 2) return route.fulfill({ status: 503, json: { detail: '明确虚构停用范围失败' } })
    if (count === 4) await gate
    return route.fulfill({ json: includes ? [row, inactive] : [row] })
  })
  await page.goto('/exchange-rates')
  const history = page.getByRole('region', { name: '汇率历史记录', exact: true })
  await expect(history).toContainText('7.1234')
  const filter = history.getByRole('checkbox', { name: '显示已停用', exact: true })
  await filter.check()
  await expect(history).toContainText('上次成功范围：仅生效记录')
  await expect(history).toContainText('当前筛选尚未成功加载')
  await expect(history).not.toContainText('9.1234')
  await history.getByRole('button', { name: '重试汇率历史', exact: true }).click()
  await expect(history).toContainText('9.1234')
  await filter.uncheck()
  await expect.poll(() => reads).toBe(4)
  await expect(history).toContainText('上次成功范围：包含已停用')
  await filter.check()
  await expect.poll(() => reads).toBe(5)
  await expect(history.locator('.n-spin-body:visible')).toHaveCount(0)
  const oldResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname === '/api/exchange-rates'
  )
  releaseOld()
  await oldResponse
  await expect(history).toContainText('9.1234')
  await expect(history).not.toContainText('上次成功范围')
  expect(reads).toBe(5)
})
