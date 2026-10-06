import { expect, test, type Page } from '@playwright/test'
import { readFileSync } from 'node:fs'
import {
  user,
  adminUser,
  loginThroughApi,
  setAuthenticatedSession,
  mockXueqiuCapabilities,
  createTemporaryUser,
  deleteTemporaryUser
} from './helpers'

const research = JSON.parse(
  readFileSync(new URL('../../docs/media/research-reading-fixture.json', import.meta.url), 'utf8')
)
const opinions = JSON.parse(
  readFileSync(new URL('../../docs/media/opinions-ui-fixture.json', import.meta.url), 'utf8')
)
const post = {
  post_id: 'UI_CAP_POST',
  created_at_ms: 1790983800000,
  title: 'UI明确虚构历史帖子',
  text: 'UI明确虚构已保存帖子正文',
  author_id: 'UI_CAP_AUTHOR',
  author_name: 'UI明确虚构作者',
  url: '',
  reply_count: 0,
  like_count: 0,
  links: [],
  first_seen_at: '2026-10-02T23:30:00Z',
  last_seen_at: '2026-10-02T23:30:00Z'
}

async function login(page: Page, credentials = user) {
  await page.getByPlaceholder('请输入用户名').fill(credentials.username)
  await page.getByPlaceholder('请输入密码').fill(credentials.password)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '仪表盘', exact: true })).toBeVisible()
}

for (const width of [320, 1440]) {
  for (const history of ['posts', 'summary']) {
    test(`${width}px only ${history} history retains its own entry and preserves official announcements`, async ({
      page,
      request
    }) => {
      await page.setViewportSize({ width, height: 852 })
      await setAuthenticatedSession(page, await loginThroughApi(request))
      await mockXueqiuCapabilities(page, {
        opinionHistory: history === 'summary',
        postHistory: history === 'posts'
      })
      const reads = { summary: 0, authors: 0, posts: 0 }
      await page.route('**/api/securities/**/analysis', (route) =>
        route.fulfill({ json: research.analysis })
      )
      await page.route('**/api/securities/**/profile', (route) =>
        route.fulfill({ json: research.profile })
      )
      await page.route('**/api/securities/**/opinion-summary', (route) => {
        reads.summary++
        return route.fulfill({ json: research.opinion })
      })
      await page.route('**/api/securities/opinion-feed?*', (route) => {
        reads.authors++
        return route.fulfill({ json: opinions.feed })
      })
      await page.route('**/api/xueqiu-collector/symbol-feed?*', (route) => {
        reads.posts++
        return route.fulfill({
          json: {
            symbol: '600900',
            market: 'A股',
            announcements: [post],
            discussions: [],
            last_cycle_finished_at: post.last_seen_at
          }
        })
      })
      const capability = page.waitForResponse((response) =>
        response.url().endsWith('/api/capabilities')
      )
      await page.goto('/securities/A股/600900')
      await capability
      await expect(page.getByRole('tab', { name: '公告', exact: true })).toBeVisible()
      await expect(page.getByRole('link', { name: '雪球观点', exact: true })).toHaveCount(
        history === 'summary' ? 1 : 0
      )
      await page.getByRole('tab', { name: '观点', exact: true }).click()
      const section = page.getByTestId('opinion-section')
      await expect(section.getByTestId('generate-opinion-button')).toHaveCount(
        history === 'summary' ? 1 : 0
      )
      await expect(section.getByTestId('opinion-related-feed')).toHaveCount(
        history === 'summary' ? 1 : 0
      )
      await expect(section.getByTestId('xueqiu-symbol-feed')).toHaveCount(
        history === 'posts' ? 1 : 0
      )
      if (history === 'posts') {
        await section.getByTestId('xueqiu-symbol-feed').getByRole('button').press('Enter')
        await expect(page.getByTestId('xueqiu-announcements')).toContainText(post.title)
        expect(reads).toEqual({ summary: 0, authors: 0, posts: 1 })
      } else {
        await expect(section).toContainText(research.opinion.summary)
        expect(reads).toEqual({ summary: 1, authors: 0, posts: 0 })
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width)
    })
  }
}

test('capability read failure remains unknown and does not block real login or the opinions route', async ({
  page
}) => {
  let reads = 0
  await page.route('**/api/capabilities', (route) => {
    reads++
    return route.fulfill({ status: 503, json: { detail: 'UI明确虚构能力读取失败' } })
  })
  await page.route('**/api/securities/opinion-summaries', (route) =>
    route.fulfill({ json: opinions.summaries })
  )
  await page.route('**/api/xueqiu-collector/status', (route) =>
    route.fulfill({ json: opinions.collector })
  )
  await page.goto('/login')
  await login(page)
  await expect.poll(() => reads).toBe(1)
  await page.getByRole('link', { name: '雪球观点', exact: true }).click()
  await expect(page.getByRole('heading', { name: '雪球观点', exact: true })).toBeVisible()
  await expect(page).toHaveURL('/opinions')
  expect(reads).toBe(1)
})

test('real SPA logout/login ABA cannot allow a late old capability to replace the current identity', async ({
  page,
  request
}) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  let reads = 0,
    release!: () => void
  const held = new Promise<void>((resolve) => (release = resolve))
  await page.route('**/api/capabilities', async (route) => {
    const count = ++reads
    if (count === 1) await held
    return route.fulfill({
      json: {
        opinions: { available: count === 1, reason: count === 1 ? 'history' : 'unconfigured' },
        xueqiu_symbol_feed: { available: false, reason: 'unconfigured' }
      }
    })
  })
  async function logout() {
    await page.getByRole('button', { name: '设置', exact: true }).click()
    await page.getByRole('button', { name: '退出登录', exact: true }).click()
    await expect(page).toHaveURL('/login')
  }
  try {
    await page.goto('/login')
    await login(page)
    await expect.poll(() => reads).toBe(1)
    await logout()
    await login(page, { username: createdUser.username, password })
    await expect.poll(() => reads).toBe(2)
    await expect(page.getByRole('link', { name: '雪球观点', exact: true })).toHaveCount(0)
    await logout()
    await login(page)
    await expect.poll(() => reads).toBe(3)
    await expect(page.getByRole('link', { name: '雪球观点', exact: true })).toHaveCount(0)
    const oldResponse = page.waitForResponse(
      async (response) =>
        response.url().endsWith('/api/capabilities') &&
        (await response.json()).opinions.available === true
    )
    release()
    await (await oldResponse).finished()
    await page.evaluate(
      () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
    )
    await expect(page.getByRole('link', { name: '雪球观点', exact: true })).toHaveCount(0)
    expect(reads).toBe(3)
  } finally {
    release()
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

test('explicit Cookie update refreshes capabilities through the existing hook without probing', async ({
  page,
  request
}) => {
  await setAuthenticatedSession(page, await loginThroughApi(request, adminUser))
  let reads = 0
  await page.route('**/api/capabilities', (route) => {
    reads++
    const configured = reads > 1
    return route.fulfill({
      json: {
        opinions: { available: configured, reason: configured ? 'configured' : 'unconfigured' },
        xueqiu_symbol_feed: {
          available: configured,
          reason: configured ? 'configured' : 'unconfigured'
        }
      }
    })
  })
  await page.route('**/api/securities/opinion-summaries', (route) =>
    route.fulfill({ json: opinions.summaries })
  )
  await page.route('**/api/xueqiu-collector/status', (route) =>
    route.fulfill({ json: opinions.collector })
  )
  const status = {
    source: 'file',
    file_path: null,
    file_exists: false,
    file_mtime: null,
    backup_exists: false,
    backup_mtime: null,
    writable: true,
    writable_reason: null,
    level: 'unconfigured',
    message: 'UI明确虚构未配置',
    keys: [],
    primary: [],
    read_error: null
  }
  let writes = 0
  await page.route('**/api/xueqiu-collector/cookie', (route) => {
    if (route.request().method() === 'GET') return route.fulfill({ json: status })
    expect(route.request().method()).toBe('PUT')
    expect(route.request().postDataJSON()).toEqual({
      content: 'xq_a_token=UI_FICTION; xqat=UI_FICTION',
      probe: false
    })
    writes++
    return route.fulfill({
      json: {
        status: { ...status, file_exists: true, level: 'normal', message: 'UI明确虚构已配置' },
        backup_created: false,
        source_format: 'header',
        notes: [],
        probe: null
      }
    })
  })
  await page.goto('/opinions')
  await page
    .getByRole('button', { name: '运行详情、关注作者、跟踪组合与最近运行', exact: true })
    .click()
  await expect(page.getByTestId('collector-update-cookie')).toBeVisible()
  await expect.poll(() => reads).toBe(1)
  await page.getByTestId('collector-update-cookie').click()
  const dialog = page.getByTestId('xueqiu-cookie-dialog')
  await expect(dialog.getByTestId('xueqiu-cookie-input')).toBeVisible()
  await dialog
    .getByRole('textbox', { name: '雪球 Cookie 内容' })
    .fill('xq_a_token=UI_FICTION; xqat=UI_FICTION')
  await dialog.getByTestId('xueqiu-cookie-submit').click()
  await expect(dialog.getByTestId('xueqiu-cookie-result')).toContainText('Cookie 已更新')
  await expect.poll(() => reads).toBe(2)
  await expect(dialog.getByRole('textbox', { name: '雪球 Cookie 内容' })).toHaveValue('')
  expect(writes).toBe(1)
})
