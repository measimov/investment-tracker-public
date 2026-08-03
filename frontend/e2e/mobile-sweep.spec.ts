// 移动端兼容性全页扫描（阶段 4）：Pixel 5 视口（393×851，Chromium 系）逐页断言无横向溢出。
// 横向滚动是移动端布局破损的最可靠信号——任何页面 body 都不得横向滚动。
import { devices, expect, test, type APIRequestContext, type Page } from '@playwright/test'

const user = { username: 'demo', password: 'e2e-user-password' }
const adminUser = { username: 'admin', password: 'e2e-admin-password' }
const authCookieName = 'investment_session'
const csrfCookieName = 'investment_csrf'

test.use({ ...devices['Pixel 5'] })

async function loginThroughApi(request: APIRequestContext, credentials = user): Promise<string> {
  const response = await request.post('http://127.0.0.1:18000/api/auth/token', {
    data: credentials
  })
  expect(response.ok()).toBeTruthy()
  return (await response.json()).access_token
}

async function seedData(request: APIRequestContext, token: string) {
  const headers = { Authorization: `Bearer ${token}` }

  // 每个 seed 请求都必须成功：接口失败会让后续页面在空态下测量，产生假阳性
  async function seedPost(path: string, data: Record<string, unknown>) {
    const response = await request.post(`http://127.0.0.1:18000${path}`, { headers, data })
    expect(
      response.ok(),
      `seed ${path} 失败: ${response.status()} ${await response.text()}`
    ).toBeTruthy()
  }

  // 券商账户：账户数据页的"新增事件/新增核对"在无账户时是禁用的，
  // 没有它对话框覆盖会卡在等待一个永远点不动的按钮上
  await seedPost('/api/broker-accounts', {
    broker: '招商证券',
    account_name: '移动端扫描账户',
    account_number_masked: '****4321',
    base_currency: 'CNY'
  })

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
const SEEDED_CONTENT: Record<string, string> = {
  '/transactions': '移动端扫描标的',
  '/holdings': '600000',
  '/corporate-actions': '移动端扫描标的',
  '/exchange-rates': 'USD'
}

async function setSession(page: Page, token: string, isAdmin = false) {
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
  await page.addInitScript(
    ({ admin }) => {
      window.localStorage.setItem(
        'user',
        JSON.stringify({
          id: admin ? 1 : 2,
          username: admin ? 'admin' : 'demo',
          email: null,
          is_active: true,
          is_admin: admin
        })
      )
    },
    { admin: isAdmin }
  )
}

const ROUTES: Array<[string, string]> = [
  ['/', '仪表盘'],
  ['/transactions', '交易记录'],
  ['/holdings', '持仓'],
  ['/statistics', '统计分析'],
  ['/account-data', '账户数据'],
  ['/corporate-actions', '公司行动'],
  ['/exchange-rates', '汇率'],
  ['/reports', 'AI 复盘']
]

// 管理员路由单独跑：普通用户会被 requiresAdmin 守卫重定向，
// 在登录页上测"无横向溢出"是假通过
const ADMIN_ROUTES: Array<[string, string]> = [
  ['/admin/users', '用户管理'],
  ['/admin/holdings', '全部持仓']
]

async function sweepRoutes(page: Page, routes: Array<[string, string]>): Promise<string[]> {
  const failures: string[] = []
  for (const [route, label] of routes) {
    await page.goto(route)
    await page.waitForLoadState('networkidle')
    // 守卫重定向会把断言变成"在登录页上测登录页"，先确认没被踢走
    expect(new URL(page.url()).pathname, `${label}(${route}) 被重定向到 ${page.url()}`).toBe(route)
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
      let worst: { right: number; tag: string; cls: string } | null = null
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
  return failures
}

test('mobile viewport has no horizontal overflow on any page', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await seedData(request, token)
  await setSession(page, token)

  const failures = await sweepRoutes(page, ROUTES)
  expect(failures, failures.join('\n')).toEqual([])
})

test('mobile admin pages have no horizontal overflow', async ({ page, request }) => {
  const token = await loginThroughApi(request, adminUser)
  await setSession(page, token, true)

  const failures = await sweepRoutes(page, ADMIN_ROUTES)
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

  async function assertDialogFits(label: string) {
    const dialog = page.locator('.el-dialog:visible').first()
    await expect(dialog, label).toBeVisible()
    const box = await dialog.boundingBox()
    const viewport = page.viewportSize()
    expect(box, `${label} 对话框未渲染`).not.toBeNull()
    expect(viewport, '视口尺寸不可用').not.toBeNull()
    expect(box!.width, `${label} 对话框宽度超出视口`).toBeLessThanOrEqual(viewport!.width)
    expect(box!.x, `${label} 对话框左缘越界`).toBeGreaterThanOrEqual(0)
    await page.keyboard.press('Escape')
    await expect(dialog).toBeHidden()
  }

  // 全部普通用户可达的对话框，按声明宽度三档（420 / 560 / 720）各有覆盖
  const dialogs: Array<[string, string, string?]> = [
    ['/transactions', '新增交易'],
    ['/transactions', '导入'],
    // 移动卡片里按钮写全称，桌面表格才是'转仓'
    ['/holdings', '转仓到其他账户'],
    ['/statistics', '输入价格'],
    ['/exchange-rates', '手动添加汇率'],
    ['/corporate-actions', '新增记录'],
    ['/account-data', '新增账户'],
    ['/account-data', '新增事件', '现金事件'],
    ['/account-data', '新增核对', '月末核对'],
    ['/account-data', '新增规则', '特例规则']
  ]

  let current = ''
  for (const [route, trigger, tab] of dialogs) {
    if (route !== current) {
      await page.goto(route)
      await page.waitForLoadState('networkidle')
      current = route
    }
    if (tab) {
      await page.getByRole('tab', { name: tab }).click()
    }
    await page.getByRole('button', { name: trigger, exact: true }).first().click()
    await assertDialogFits(`${route} · ${trigger}`)
  }
})

test('mobile admin dialogs are constrained within the viewport', async ({ page, request }) => {
  const token = await loginThroughApi(request, adminUser)
  await setSession(page, token, true)

  await page.goto('/admin/users')
  await page.waitForLoadState('networkidle')
  for (const trigger of ['添加用户', '重置密码']) {
    await page.getByRole('button', { name: trigger, exact: true }).first().click()
    const dialog = page.locator('.el-dialog:visible').first()
    await expect(dialog, trigger).toBeVisible()
    const box = await dialog.boundingBox()
    const viewport = page.viewportSize()
    expect(box!.width, `${trigger} 对话框宽度超出视口`).toBeLessThanOrEqual(viewport!.width)
    expect(box!.x, `${trigger} 对话框左缘越界`).toBeGreaterThanOrEqual(0)
    await page.keyboard.press('Escape')
    await expect(dialog).toBeHidden()
  }
})

// 合法长文本不得把卡片撑破：schema 上限的账户名（100）与无断点备注渲染后，
// 卡片与操作区的 bounding box 必须仍落在视口内。只查根节点 scrollWidth 会假
// 通过——外层容器把超宽内容裁掉了，根节点不横滚，但按钮已经点不到（评审实测）。
test('mobile cards keep long text and actions inside the viewport', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  const headers = { Authorization: `Bearer ${token}` }
  // 无空格 ASCII 是最坏的合法输入：中文可任意折行，掩盖不了断行约束的缺失
  const longName = 'B'.repeat(90)
  const longNote = 'A'.repeat(120)

  const created = await request.post('http://127.0.0.1:18000/api/broker-accounts', {
    headers,
    data: {
      broker: '招商证券',
      account_name: longName,
      account_number_masked: '****9999',
      base_currency: 'CNY',
      notes: longNote
    }
  })
  expect(created.ok(), `种子账户失败: ${created.status()} ${await created.text()}`).toBeTruthy()

  await setSession(page, token)
  await page.goto('/account-data')
  await page.waitForLoadState('networkidle')

  const card = page.getByTestId('account-card').filter({ hasText: longNote }).first()
  await expect(card, '长文本账户卡片未渲染').toBeVisible()

  const viewport = page.viewportSize()!
  const cardBox = (await card.boundingBox())!
  expect(
    Math.round(cardBox.x + cardBox.width),
    `卡片右缘 ${Math.round(cardBox.x + cardBox.width)} 超出视口 ${viewport.width}`
  ).toBeLessThanOrEqual(viewport.width)

  const actions = card.locator('.mobile-card-actions').first()
  const actionsBox = (await actions.boundingBox())!
  expect(
    Math.round(actionsBox.x + actionsBox.width),
    `操作区右缘 ${Math.round(actionsBox.x + actionsBox.width)} 超出视口 ${viewport.width}`
  ).toBeLessThanOrEqual(viewport.width)

  // 删除按钮必须真的可点：命中测试落在按钮自身上，而不是被裁到屏幕外
  const deleteButton = actions.getByRole('button', { name: '删除空账户' })
  await expect(deleteButton).toBeVisible()
  const buttonBox = (await deleteButton.boundingBox())!
  expect(buttonBox.x + buttonBox.width).toBeLessThanOrEqual(viewport.width)
})

// 工具栏在窄屏必须纵向堆叠：标题与操作区并排时按钮会被挤成一列图标，
// 这是横向溢出检查抓不到的破损（元素没出界，只是被压扁）
test('mobile page toolbars stack vertically', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await seedData(request, token)
  await setSession(page, token)

  const failures: string[] = []
  for (const [route, label] of ROUTES) {
    await page.goto(route)
    await page.waitForLoadState('networkidle')
    const header = page.locator('.page-header').first()
    if ((await header.count()) === 0) continue
    const actions = header.locator('.header-actions').first()
    if ((await actions.count()) === 0) continue
    const headerBox = await header.boundingBox()
    const actionsBox = await actions.boundingBox()
    if (!headerBox || !actionsBox) continue
    // 堆叠后操作区顶边应明显低于页头顶边（并排时二者几乎齐平）
    if (actionsBox.y - headerBox.y < 12) {
      failures.push(
        `${label}(${route}): 工具栏未堆叠，header.y=${Math.round(headerBox.y)} actions.y=${Math.round(actionsBox.y)}`
      )
    }
  }
  expect(failures, failures.join('\n')).toEqual([])
})
