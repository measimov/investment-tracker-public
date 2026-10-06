import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import {
  adminUser,
  loginThroughApi,
  mockXueqiuCapabilities,
  setAuthenticatedSession
} from './helpers'
const fixture = JSON.parse(
  readFileSync(new URL('../../docs/media/opinions-ui-fixture.json', import.meta.url), 'utf8')
)
const research = JSON.parse(
  readFileSync(new URL('../../docs/media/research-reading-fixture.json', import.meta.url), 'utf8')
)
test.use({ timezoneId: 'Asia/Shanghai' })
test.beforeEach(async ({ page }) => {
  // 本文件的虚构库保留历史摘要/作者发言，近期读取失败不表示历史不存在。
  await mockXueqiuCapabilities(page, { opinionHistory: true })
})

test('概览、作者和采集器分别读取失败保持未知，GET重试后才有已知空，旧值失败仍说明', async ({
  page,
  request
}) => {
  await setAuthenticatedSession(page, await loginThroughApi(request))
  const counts = { summaries: 0, feed: 0, collector: 0 }
  await page.route('**/api/securities/opinion-summaries', (route) => {
    counts.summaries++
    return route.fulfill(
      counts.summaries === 1 || counts.summaries === 3
        ? { status: 503, json: { detail: '明确虚构概览读取失败' } }
        : { json: { ...fixture.summaries, items: [] } }
    )
  })
  await page.route('**/api/securities/opinion-feed?*', (route) => {
    counts.feed++
    return route.fulfill(
      counts.feed === 1
        ? { status: 503, json: { detail: '明确虚构作者读取失败' } }
        : { json: { ...fixture.feed, authors: [] } }
    )
  })
  await page.route('**/api/xueqiu-collector/status', (route) => {
    counts.collector++
    return route.fulfill(
      counts.collector === 1 || counts.collector === 3
        ? { status: 503, json: { detail: '明确虚构采集器读取失败' } }
        : { json: fixture.collector }
    )
  })
  await page.goto('/opinions')
  await expect(page.getByRole('tabpanel')).toContainText('观点概览尚未加载成功')
  await expect(page.getByTestId('collector-health')).toHaveText('状态未知')
  await expect(page.getByTestId('opinion-batch-button')).toBeDisabled()
  await expect(page.getByRole('tabpanel')).not.toContainText('近期关注作者未提及')
  await page.getByRole('button', { name: '重新加载观点概览', exact: true }).click()
  await expect(page.getByRole('tabpanel')).toContainText('近期关注作者未提及')
  await page.getByRole('button', { name: '重新加载观点概览', exact: true }).click()
  await expect(page.getByRole('tabpanel')).toContainText('保留上次成功的概览，未确认最新结果')
  await page.getByRole('button', { name: '重新加载采集器状态', exact: true }).click()
  await expect(page.getByTestId('collector-health')).toHaveText('部分成功')
  await page.getByRole('button', { name: '重新加载采集器状态', exact: true }).click()
  await expect(page.getByTestId('xueqiu-collector-card')).toContainText('保留上次成功的采集器状态')
  await page.getByRole('tab', { name: '作者动态', exact: true }).click()
  await expect(page.getByRole('tabpanel')).toContainText('作者动态尚未加载成功')
  await expect(page.getByRole('tabpanel')).not.toContainText('近 30 天关注作者没有发言')
  await page.getByRole('button', { name: '重新加载作者动态', exact: true }).click()
  await expect(page.getByRole('tabpanel')).toContainText('近 30 天关注作者没有发言')
  expect(counts).toEqual({ summaries: 3, feed: 2, collector: 3 })
})

test('来源false保留历史摘要与未知计数，作者来源不被概览true覆盖，迟到失败不污染新流', async ({
  page,
  request
}) => {
  await setAuthenticatedSession(page, await loginThroughApi(request))
  let summaryReads = 0,
    feedReads = 0,
    release!: () => void
  const pending = new Promise<void>((resolve) => (release = resolve))
  await page.route('**/api/securities/opinion-summaries', (route) => {
    summaryReads++
    return route.fulfill({
      json: {
        ...fixture.summaries,
        source_available: summaryReads > 1,
        items:
          summaryReads === 1
            ? [{ ...fixture.summaries.items[1], matched_count: null, new_utterance_count: null }]
            : fixture.summaries.items
      }
    })
  })
  await page.route('**/api/xueqiu-collector/status', (route) =>
    route.fulfill({ json: fixture.collector })
  )
  await page.route('**/api/securities/opinion-feed?*', async (route) => {
    feedReads++
    if (feedReads === 1) {
      await pending
      return route.fulfill({ status: 503, json: { detail: '明确虚构过期作者失败' } })
    }
    return route.fulfill({ json: { ...fixture.feed, source_available: false, authors: [] } })
  })
  await page.goto('/opinions')
  const table = page.getByTestId('opinion-symbols-table')
  await expect(table).toContainText(fixture.summaries.items[1].name)
  await expect(table.getByRole('cell', { name: '—', exact: true })).toHaveCount(2)
  await expect(page.getByTestId('opinion-source-missing')).toContainText('已有历史摘要仍保留')
  await page.getByRole('button', { name: '重新加载观点概览', exact: true }).click()
  await expect(page.getByTestId('opinion-source-missing')).toHaveCount(0)
  await expect(table.getByRole('row').nth(1)).toContainText('UI虚构多新发言')
  await expect(table).toContainText('99+')
  const zero = table.getByRole('row').filter({ hasText: 'UIB' })
  await expect(zero.getByRole('cell', { name: '0', exact: true })).toHaveCount(2)
  await page.getByRole('tab', { name: '作者动态', exact: true }).click()
  await expect.poll(() => feedReads).toBe(1)
  await page.getByRole('tab', { name: '标的观点', exact: true }).click()
  await page.getByRole('tab', { name: '作者动态', exact: true }).click()
  await expect(page.getByRole('tabpanel')).toContainText('作者发言数据源当前不可用')
  release()
  await page.waitForLoadState('networkidle')
  await expect(page.getByRole('tabpanel')).not.toContainText('过期作者失败')
  await expect(page.getByRole('tabpanel')).not.toContainText('近 30 天关注作者没有发言')
  await expect(page.getByTestId('opinion-batch-button')).toBeEnabled()
  expect(feedReads).toBe(2)
})

test('共享作者折叠保留详情首次懒读取、默认收起、总条数及手机完整阅读', async ({
  page,
  request
}) => {
  await page.setViewportSize({ width: 393, height: 852 })
  await setAuthenticatedSession(page, await loginThroughApi(request))
  await page.route('**/api/securities/**/analysis', (route) =>
    route.fulfill({ json: research.analysis })
  )
  await page.route('**/api/securities/**/profile', (route) =>
    route.fulfill({ json: research.profile })
  )
  await page.route('**/api/securities/**/opinion-summary', (route) =>
    route.fulfill({ status: 404, json: { detail: '明确虚构未生成' } })
  )
  let feedReads = 0
  await page.route('**/api/securities/opinion-feed?*', (route) => {
    feedReads++
    return route.fulfill({ json: fixture.feed })
  })
  await page.goto('/securities/A股/UI-RESEARCH')
  await page.getByRole('tab', { name: '观点', exact: true }).click()
  expect(feedReads).toBe(0)
  await page
    .getByTestId('opinion-related-feed')
    .getByRole('button', { name: /相关作者动态/ })
    .click()
  const feed = page.getByTestId('opinion-author-feed')
  await expect(feed).toContainText(/187 条\s*（显示最新 2 条）/)
  const author = feed.getByRole('button', { name: new RegExp('UI明确虚构作者与组合') })
  await expect(author).toHaveAttribute('aria-expanded', 'false')
  await author.focus()
  await page.keyboard.press('Enter')
  await expect(author).toHaveAttribute('aria-expanded', 'true')
  await expect(feed.locator('.feed-text').first()).toBeVisible()
  await expect(feed.locator('.feed-text').first()).toHaveText(fixture.feed.authors[0].items[0].text)
  await expect(feed).toContainText(fixture.feed.authors[0].items[0].context)
  expect(
    await feed
      .locator('.feed-text')
      .first()
      .evaluate((node) => getComputedStyle(node).fontSize)
  ).toBe('17px')
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(393)
  await page
    .getByTestId('opinion-related-feed')
    .getByRole('button', { name: /相关作者动态/ })
    .click()
  await page
    .getByTestId('opinion-related-feed')
    .getByRole('button', { name: /相关作者动态/ })
    .click()
  expect(feedReads).toBe(1)
})

test('采集器管理员四个实际输入具名可聚焦，输入不自动触发管理写入', async ({ page, request }) => {
  await setAuthenticatedSession(page, await loginThroughApi(request, adminUser))
  const writes: string[] = []
  await page.route('**/api/xueqiu-collector/**', (route) => {
    if (route.request().method() !== 'GET') {
      writes.push(route.request().url())
      return route.abort()
    }
    if (route.request().url().endsWith('/status')) return route.fulfill({ json: fixture.collector })
    return route.continue()
  })
  await page.route('**/api/securities/opinion-summaries', (route) =>
    route.fulfill({ json: fixture.summaries })
  )
  await page.goto('/opinions')
  const collector = page.getByTestId('xueqiu-collector-card')
  await collector
    .getByRole('button', { name: '运行详情、关注作者、跟踪组合与最近运行', exact: true })
    .click()
  for (const [name, value] of [
    ['关注作者雪球用户 ID', '9500003'],
    ['关注作者展示名', 'UI明确虚构姓名'],
    ['跟踪组合代号', 'ZH950003'],
    ['跟踪组合展示名', 'UI明确虚构组合']
  ]) {
    const input = collector.getByRole('textbox', { name, exact: true })
    await input.focus()
    await expect(input).toBeFocused()
    await input.fill(value)
  }
  await expect(collector.getByRole('button', { name: '加入关注', exact: true })).toBeEnabled()
  await expect(collector.getByRole('button', { name: '跟踪组合', exact: true })).toBeEnabled()
  expect(writes).toEqual([])
})

for (const width of [393, 1440]) {
  test(`采集器开关 ${width}px 保存失败及同值重载保持真实状态，成功与忙时键盘仍可用`, async ({
    page,
    request
  }) => {
    await page.setViewportSize({ width, height: 852 })
    await setAuthenticatedSession(page, await loginThroughApi(request, adminUser))
    const collectorData = structuredClone(fixture.collector)
    let statusReads = 0,
      mode: 'fail' | 'success' | 'pending' = 'fail',
      release!: () => void
    const pending = new Promise<void>((resolve) => (release = resolve))
    const patches: Array<{ path: string; body: { enabled: boolean } }> = []
    await page.route('**/api/xueqiu-collector/**', async (route) => {
      const req = route.request(),
        url = new URL(req.url())
      if (req.method() === 'GET' && url.pathname.endsWith('/status')) {
        statusReads++
        return route.fulfill({ json: collectorData })
      }
      if (req.method() === 'PATCH') {
        const body = req.postDataJSON()
        expect(Object.keys(body)).toEqual(['enabled'])
        patches.push({ path: url.pathname, body })
        const source = url.pathname.includes('/authors/')
          ? collectorData.authors[0]
          : collectorData.cubes[0]
        if (mode === 'fail')
          return route.fulfill({ status: 503, json: { detail: '明确虚构保存失败，服务器仍启用' } })
        if (mode === 'pending') await pending
        source.enabled = body.enabled
        return route.fulfill({ json: source })
      }
      return route.abort()
    })
    await page.route('**/api/securities/opinion-summaries', (route) =>
      route.fulfill({ json: fixture.summaries })
    )
    await page.goto('/opinions')
    await page
      .getByRole('button', { name: '运行详情、关注作者、跟踪组合与最近运行', exact: true })
      .click()
    const author = page.getByRole('checkbox', {
      name: `启用${width === 393 ? ' ' : ''}作者 ${collectorData.authors[0].display_name}`,
      exact: true
    })
    const cube = page.getByRole('checkbox', {
      name: `启用${width === 393 ? ' ' : ''}组合 ${collectorData.cubes[0].display_name}`,
      exact: true
    })
    for (const checkbox of [author, cube]) {
      await expect(checkbox).toBeChecked()
      if (width === 393) await checkbox.getByText('启用', { exact: true }).click()
      else await checkbox.click()
      await expect(checkbox).toBeEnabled()
      await expect(checkbox).toBeChecked()
    }
    await expect.poll(() => patches.length).toBe(2)
    expect(patches.map((p) => p.body)).toEqual([{ enabled: false }, { enabled: false }])
    await page.getByRole('button', { name: '重新加载采集器状态', exact: true }).click()
    await expect.poll(() => statusReads).toBe(2)
    await expect(author).toBeChecked()
    await expect(cube).toBeChecked()
    mode = 'success'
    await author.focus()
    await page.keyboard.press('Space')
    await expect(author).not.toBeChecked()
    await cube.focus()
    await page.keyboard.press('Space')
    await expect(cube).not.toBeChecked()
    await expect.poll(() => statusReads).toBe(4)
    mode = 'pending'
    await author.focus()
    await page.keyboard.press('Space')
    await expect.poll(() => patches.length).toBe(5)
    await expect(author).toBeDisabled()
    await expect(cube).toBeDisabled()
    await expect(author).not.toBeChecked()
    await page.keyboard.press('Space')
    await cube.dispatchEvent('click')
    expect(patches).toHaveLength(5)
    release()
    await expect(author).toBeEnabled()
    await expect(author).toBeChecked()
    await expect(cube).not.toBeChecked()
    await expect.poll(() => statusReads).toBe(5)
    expect(patches.at(-1)?.body).toEqual({ enabled: true })
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width)
  })
}
