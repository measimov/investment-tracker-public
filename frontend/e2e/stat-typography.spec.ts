// 已批准的数值角色：桌面持仓主市值 30px、累计收益 22px、次级指标 20px；
// 仪表盘主市值在 30–44px 间随可用宽度变化，强调市值及美元折算值。
// 手机主市值 32px、累计收益 28px、次级指标 22px。
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
const VALUE_SIZE = '20px'

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
  await page.setViewportSize({ width: 1440, height: 900 })
  const token = await loginThroughApi(request)
  await seedHolding(request, token)
  await setSession(page, token)

  await page.goto('/')
  await page.waitForLoadState('networkidle')
  await expectFontSize(page, '.portfolio-overview .metric-label', '14px', '仪表盘主市值标签')
  const dashboardValue = page.locator('.portfolio-overview .hero-value')
  await expect(dashboardValue).toBeVisible()
  const dashboardSize = await dashboardValue.evaluate((el) =>
    parseFloat(getComputedStyle(el).fontSize)
  )
  expect(dashboardSize).toBeGreaterThanOrEqual(30)
  expect(dashboardSize).toBeLessThanOrEqual(44)
  await expectFontSize(page, '.composition-metric .metric-value', '20px', '仪表盘核对数值')

  await page.goto('/holdings')
  await page.waitForLoadState('networkidle')
  await expectFontSize(page, '.summary-section .metric-label', '13px', '持仓汇总标签')
  await expectFontSize(page, '.summary-section .hero-value', '30px', '持仓市值主数值')
  await expectFontSize(page, '.summary-section .metric-value', '20px', '持仓其余汇总数值')

  await page.goto('/statistics')
  await page.waitForLoadState('networkidle')
  await expectFontSize(page, '.metric-label', LABEL_SIZE, '统计指标标签')
  await expectFontSize(page, '.ttwr-value', '30px', '所选区间累计 TTWR 主指标')
  await expectFontSize(
    page,
    '.analytics-summary-grid .analytics-metric:not(.primary-metric) .metric-value',
    '22px',
    '统计曲线辅助指标'
  )
  await expectFontSize(page, '.risk-metrics .metric-value', '18px', '风险与平仓指标')
  // 财务数字保留完整格式字符串，主收益与其他指标分别体现阅读层级
  await expectFontSize(page, '.financial-statistic-label', '13px', '财务指标标签')
  await expectFontSize(
    page,
    '.receivable-card [data-testid=cash-basis-return]',
    '22px',
    '权益仓主收益'
  )

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
  await expectFontSize(
    page,
    '.portfolio-overview .hero-value',
    '32px',
    '仪表盘市值主数值（移动端）'
  )
  await expectFontSize(
    page,
    '.composition-metric .metric-value',
    '22px',
    '仪表盘核对数值（移动端）'
  )
})
