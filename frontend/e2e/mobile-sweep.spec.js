// 移动端兼容性全页扫描（阶段 4）：Pixel 5 视口（393×851，Chromium 系）逐页断言无横向溢出。
// 横向滚动是移动端布局破损的最可靠信号——任何页面 body 都不得横向滚动。
import { devices, expect, test } from '@playwright/test'

const user = { username: 'demo', password: 'e2e-user-password' }
const authCookieName = 'investment_session'
const csrfCookieName = 'investment_csrf'

test.use({ ...devices['Pixel 5'] })

async function loginThroughApi(request) {
  const response = await request.post('http://127.0.0.1:18000/api/auth/token', { data: user })
  expect(response.ok()).toBeTruthy()
  return (await response.json()).access_token
}

async function seedData(request, token) {
  const headers = { Authorization: `Bearer ${token}` }

  // 每个 seed 请求都必须成功：接口失败会让后续页面在空态下测量，产生假阳性
  async function seedPost(path, data) {
    const response = await request.post(`http://127.0.0.1:18000${path}`, { headers, data })
    expect(
      response.ok(),
      `seed ${path} 失败: ${response.status()} ${await response.text()}`
    ).toBeTruthy()
  }

  // 一笔持仓 + 一笔已平仓 + 汇率 + 现金股息，让各页有真实内容可渲染
  await seedPost('/api/exchange-rates', {
    from_currency: 'USD',
    to_currency: 'CNY',
    rate: 7.2,
    effective_date: '2026-01-01'
  })
  for (const txn of [
    {
      symbol: '600000',
      name: '移动端扫描标的',
      market: 'A股',
      transaction_type: 'BUY',
      quantity: 100,
      price: 10,
      fee: 1,
      transaction_date: '2026-01-05',
      currency: 'CNY'
    },
    {
      symbol: '600000',
      name: '移动端扫描标的',
      market: 'A股',
      transaction_type: 'SELL',
      quantity: 40,
      price: 12,
      fee: 1,
      transaction_date: '2026-02-05',
      currency: 'CNY'
    }
  ]) {
    await seedPost('/api/transactions', txn)
  }
  await seedPost('/api/corporate-actions/cash-dividend', {
    symbol: '600000',
    name: '移动端扫描标的',
    market: 'A股',
    ex_date: '2026-03-01',
    dividend_per_share: 0.3,
    total_dividend: 30,
    tax_rate: 0.1,
    currency: 'CNY'
  })
}

// 种子标记内容必须真的渲染出来，才证明页面不是在空态/重定向下测量的
const SEEDED_CONTENT = {
  '/transactions': '移动端扫描标的',
  '/holdings': '600000',
  '/corporate-actions': '移动端扫描标的',
  '/exchange-rates': 'USD'
}

async function setSession(page, token) {
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

const ROUTES = [
  ['/', '仪表盘'],
  ['/transactions', '交易记录'],
  ['/holdings', '持仓'],
  ['/statistics', '统计分析'],
  ['/account-data', '账户数据'],
  ['/corporate-actions', '公司行动'],
  ['/exchange-rates', '汇率']
]

test('mobile viewport has no horizontal overflow on any page', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await seedData(request, token)
  await setSession(page, token)

  const failures = []
  for (const [route, label] of ROUTES) {
    await page.goto(route)
    await page.waitForLoadState('networkidle')
    const marker = SEEDED_CONTENT[route]
    if (marker) {
      await expect(
        page.locator(`text=${marker}`).first(),
        `${label}(${route}) 未渲染出种子内容，页面可能处于空态或被重定向`
      ).toBeVisible()
    }
    const overflow = await page.evaluate(() => {
      const doc = document.documentElement
      const overflowPx = Math.max(doc.scrollWidth - doc.clientWidth, 0)
      // 找出最宽的溢出元素帮助定位
      let worst = null
      if (overflowPx > 1) {
        for (const el of document.querySelectorAll('body *')) {
          const rect = el.getBoundingClientRect()
          if (rect.right > doc.clientWidth + 1 && (!worst || rect.right > worst.right)) {
            worst = {
              right: Math.round(rect.right),
              tag: el.tagName,
              cls: String(el.className).slice(0, 80)
            }
          }
        }
      }
      return { overflowPx, worst }
    })
    if (overflow.overflowPx > 1) {
      failures.push(
        `${label}(${route}): 溢出 ${overflow.overflowPx}px ${JSON.stringify(overflow.worst)}`
      )
    }
  }
  expect(failures, failures.join('\n')).toEqual([])
})

test('mobile login page has no horizontal overflow', async ({ page }) => {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  const overflowPx = await page.evaluate(() =>
    Math.max(document.documentElement.scrollWidth - document.documentElement.clientWidth, 0)
  )
  expect(overflowPx).toBeLessThanOrEqual(1)
})

test('mobile dialogs are constrained within the viewport', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await setSession(page, token)

  async function assertDialogFits(label) {
    const dialog = page.locator('.el-dialog:visible').first()
    await expect(dialog, label).toBeVisible()
    const box = await dialog.boundingBox()
    const viewport = page.viewportSize()
    expect(box.width, `${label} 对话框宽度超出视口`).toBeLessThanOrEqual(viewport.width)
    expect(box.x, `${label} 对话框左缘越界`).toBeGreaterThanOrEqual(0)
    await page.keyboard.press('Escape')
    await expect(dialog).toBeHidden()
  }

  // 交易编辑（声明 600px）
  await page.goto('/transactions')
  await page.waitForLoadState('networkidle')
  await page.getByRole('button', { name: '新增交易' }).click()
  await assertDialogFits('新增交易')

  // 行情价格录入（声明 800px，全站最宽）
  await page.goto('/statistics')
  await page.waitForLoadState('networkidle')
  await page.getByRole('button', { name: '输入价格' }).click()
  await assertDialogFits('输入价格')

  // 汇率编辑（声明 500px）
  await page.goto('/exchange-rates')
  await page.waitForLoadState('networkidle')
  await page.getByRole('button', { name: '添加汇率' }).click()
  await assertDialogFits('添加汇率')
})
