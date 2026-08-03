// Stat 卡排版基准回归（阶段 3）：标签 13px / 值 22px。
//
// 这条基准分散在 4 个视图 + 1 条全局 el-statistic 覆盖里，靠人眼比对
// 截图无法可靠守住——本轮就有一次 24px→22px 的替换误伤了图标而数值
// 纹丝不动（PR #98 评审实测发现）。断言浏览器的 computed font-size，
// 让任何一处漂回旧值时直接红。
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const user = { username: 'demo', password: 'e2e-user-password' }
const authCookieName = 'investment_session'
const csrfCookieName = 'investment_csrf'

const LABEL_SIZE = '13px'
const VALUE_SIZE = '22px'

async function loginThroughApi(request: APIRequestContext): Promise<string> {
  const response = await request.post('http://127.0.0.1:18000/api/auth/token', { data: user })
  expect(response.ok()).toBeTruthy()
  return (await response.json()).access_token
}

async function seedHolding(request: APIRequestContext, token: string) {
  const response = await request.post('http://127.0.0.1:18000/api/transactions', {
    headers: { Authorization: `Bearer ${token}` },
    data: {
      symbol: '600000',
      name: '排版基准标的',
      market: 'A股',
      transaction_type: 'BUY',
      quantity: 100,
      price: 10,
      fee: 1,
      transaction_date: '2026-01-05',
      currency: 'CNY'
    }
  })
  expect(response.ok(), `种子交易失败: ${response.status()} ${await response.text()}`).toBeTruthy()
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

async function expectFontSize(page: Page, selector: string, expected: string, label: string) {
  const element = page.locator(selector).first()
  await expect(element, `${label}: 选择器 ${selector} 未渲染，断言会假通过`).toBeVisible()
  const size = await element.evaluate((el) => getComputedStyle(el).fontSize)
  expect(size, `${label}（${selector}）应为 ${expected}`).toBe(expected)
}

test('stat 卡标签与数值遵循全局排版基准', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await seedHolding(request, token)
  await setSession(page, token)

  await page.goto('/')
  await page.waitForLoadState('networkidle')
  await expectFontSize(page, '.card-title', LABEL_SIZE, '仪表盘摘要卡标签')
  await expectFontSize(page, '.card-value', VALUE_SIZE, '仪表盘摘要卡数值')

  await page.goto('/holdings')
  await page.waitForLoadState('networkidle')
  await expectFontSize(page, '.summary-label', LABEL_SIZE, '持仓汇总标签')
  await expectFontSize(page, '.summary-value', VALUE_SIZE, '持仓汇总数值')

  await page.goto('/statistics')
  await page.waitForLoadState('networkidle')
  await expectFontSize(page, '.metric-label', LABEL_SIZE, '统计指标标签')
  await expectFontSize(page, '.metric-value', VALUE_SIZE, '统计指标数值')
  // el-statistic 由全局覆盖统一，视图侧不再各写 :deep
  await expectFontSize(page, '.el-statistic__head', LABEL_SIZE, 'el-statistic 标签')
  await expectFontSize(page, '.el-statistic__content', VALUE_SIZE, 'el-statistic 数值')

  await page.goto('/account-data')
  await page.waitForLoadState('networkidle')
  await expectFontSize(page, '.summary-item strong', VALUE_SIZE, '账户数据汇总数值')
})

test('移动端不再单独压缩 stat 数值', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await setSession(page, token)
  await page.setViewportSize({ width: 393, height: 851 })

  await page.goto('/')
  await page.waitForLoadState('networkidle')
  await expectFontSize(page, '.card-value', VALUE_SIZE, '仪表盘摘要卡数值（移动端）')
})
