import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const user = {
  username: 'demo',
  password: 'e2e-user-password'
}

const adminUser = {
  username: 'admin',
  password: 'e2e-admin-password'
}
const authCookieName = 'investment_session'
const csrfCookieName = 'investment_csrf'

// API 响应行的宽松类型：E2E 断言只关心少数字段，其余按原样透传
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type ApiRow = Record<string, any>

async function loginThroughApi(
  request: APIRequestContext,
  credentials: { username: string; password: string } = user
): Promise<string> {
  const response = await request.post('http://127.0.0.1:18000/api/auth/token', {
    data: credentials
  })
  expect(response.ok()).toBeTruthy()
  const body = await response.json()
  return body.access_token
}

async function setAuthenticatedSession(
  page: Page,
  token: string,
  userInfo: Record<string, unknown> | null = null
) {
  const storedUser = userInfo || {
    id: 2,
    username: 'demo',
    email: null,
    is_active: true,
    is_admin: false
  }

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
    ({ currentUser }) => {
      window.localStorage.setItem('user', JSON.stringify(currentUser))
    },
    { currentUser: storedUser }
  )
}

async function createTemporaryUser(request: APIRequestContext) {
  const adminToken = await loginThroughApi(request, adminUser)
  // fullyParallel 下多个用例可能同毫秒建用户，仅靠 Date.now() 会撞名
  const username = `analytics_e2e_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
  const password = 'analytics-e2e-password'
  const createResponse = await request.post('http://127.0.0.1:18000/api/users', {
    headers: {
      Authorization: `Bearer ${adminToken}`
    },
    data: {
      username,
      password,
      is_active: true,
      is_admin: false
    }
  })
  expect(createResponse.ok()).toBeTruthy()
  const createdUser = await createResponse.json()
  return { adminToken, createdUser, password }
}

async function deleteTemporaryUser(request: APIRequestContext, adminToken: string, userId: number) {
  const response = await request.delete(`http://127.0.0.1:18000/api/users/${userId}`, {
    headers: {
      Authorization: `Bearer ${adminToken}`
    }
  })
  expect(response.ok()).toBeTruthy()
}

function ibkrCsv(rows: string[]) {
  return [
    'Statement,Header,域名称,域值',
    'Statement,Data,Title,Transaction History',
    '总结,Header,域名称,域值',
    '总结,Data,基础货币,USD',
    'Transaction History,Header,日期,账户,说明,交易类型,代码,数量,价格,Price Currency,总额,佣金,净额',
    ...rows
  ].join('\n')
}

async function createIbkrBrokerAccount(request: APIRequestContext, token: string) {
  const response = await request.post('http://127.0.0.1:18000/api/broker-accounts', {
    headers: {
      Authorization: `Bearer ${token}`
    },
    data: {
      broker: 'IBKR',
      account_name: 'IBKR E2E',
      account_number_masked: 'U***67968',
      base_currency: 'USD'
    }
  })
  expect(response.ok()).toBeTruthy()
  return response.json()
}

// 特例规则如今是用户数据（issue #82）：迁移只为迁移时点已存在的用户播种，
// 全新数据库先迁移后建用户，因此测试自备所需规则；共享用户重复运行返回 409
async function ensureSecurityRule(request: APIRequestContext, token: string, data: ApiRow) {
  const response = await request.post('http://127.0.0.1:18000/api/security-rules', {
    headers: { Authorization: `Bearer ${token}` },
    data
  })
  expect([201, 409]).toContain(response.status())
}

async function importIbkrCsv(
  request: APIRequestContext,
  token: string,
  brokerAccountId: number | string,
  csv: string,
  filename = 'ibkr-e2e.csv'
) {
  return request.post('http://127.0.0.1:18000/api/import/ibkr-activity', {
    headers: {
      Authorization: `Bearer ${token}`
    },
    multipart: {
      broker_account_id: String(brokerAccountId),
      file: {
        name: filename,
        mimeType: 'text/csv',
        buffer: Buffer.from(csv, 'utf8')
      }
    }
  })
}

test('redirects anonymous users to login and supports login', async ({ page }) => {
  await page.goto('/holdings')
  await expect(page).toHaveURL(/\/login/)
  await expect(
    page.locator('.login-card').getByRole('heading', { name: /投资追踪系统/ })
  ).toBeVisible()

  // 守卫带上了 ?redirect=（#142：401/未登录不再丢页面）
  await expect(page).toHaveURL(/redirect=%2Fholdings/)

  await page.getByPlaceholder('请输入用户名').fill(user.username)
  await page.getByPlaceholder('请输入密码').fill(user.password)
  await page.getByRole('button', { name: '登录' }).click()

  // 登录成功后回跳到最初想去的持仓页，而不是首页
  await expect(page).toHaveURL(/\/holdings$/)
  await expect(page.getByRole('main')).toContainText('当前持仓')
  expect(await page.evaluate(() => window.localStorage.getItem('token'))).toBeNull()
  const cookies = await page.context().cookies()
  expect(cookies.find((cookie) => cookie.name === authCookieName)?.httpOnly).toBeTruthy()
  expect(cookies.find((cookie) => cookie.name === csrfCookieName)?.httpOnly).toBeFalsy()
})

test('opens the account data foundation', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  await page.goto('/account-data')
  await expect(page.getByRole('heading', { name: '账户数据' })).toBeVisible()
  await expect(page.getByText('券商账户', { exact: true }).first()).toBeVisible()
  await page.getByRole('tab', { name: '现金事件' }).click()
  await expect(page.getByText('收益统计为权益仓口径')).toBeVisible()
})

test('cookie-authenticated writes require a matching CSRF header', async ({ request }) => {
  const loginResponse = await request.post('http://127.0.0.1:18000/api/auth/login', {
    data: user
  })
  expect(loginResponse.ok()).toBeTruthy()
  const storageState = await request.storageState()
  const csrfCookie = storageState.cookies.find((cookie) => cookie.name === csrfCookieName)
  expect(csrfCookie).toBeTruthy()

  const transaction = {
    symbol: 'CSRF001',
    name: 'CSRF Test',
    market: 'A股',
    transaction_type: 'BUY',
    quantity: 1,
    price: 1,
    fee: 0,
    transaction_date: '2026-07-11',
    currency: 'CNY'
  }
  const rejected = await request.post('http://127.0.0.1:18000/api/transactions', {
    data: transaction
  })
  expect(rejected.status()).toBe(403)

  const accepted = await request.post('http://127.0.0.1:18000/api/transactions', {
    headers: { 'X-CSRF-Token': csrfCookie!.value },
    data: transaction
  })
  expect(accepted.status()).toBe(201)
})

test('shows created transactions, holdings, and total realized return', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)

  const createResponse = await request.post('http://127.0.0.1:18000/api/transactions', {
    headers: {
      Authorization: `Bearer ${token}`
    },
    data: {
      symbol: 'E2E001',
      name: '端到端测试资产',
      market: 'A股',
      transaction_type: 'BUY',
      quantity: 100,
      price: 12.34,
      fee: 1.5,
      transaction_date: '2026-05-13',
      currency: 'CNY',
      notes: 'playwright e2e'
    }
  })
  expect(createResponse.ok()).toBeTruthy()

  await setAuthenticatedSession(page, token)

  await page.goto('/transactions')
  await expect(page.getByText('交易记录管理')).toBeVisible()
  await expect(page.getByText('E2E001')).toBeVisible()
  await expect(page.getByText('端到端测试资产')).toBeVisible()
  await expect(page.locator('.el-table').getByText('买入').first()).toBeVisible()

  // 编辑保存回归：后端 schema 是 extra="forbid"，若 payload 混入 id 等
  // 多余字段会 422（曾导致所有编辑保存静默失败，备注永远存不上）
  await page
    .locator('.el-table__row', { hasText: 'E2E001' })
    .getByRole('button', { name: '编辑' })
    .click()
  await expect(page.getByText('编辑交易')).toBeVisible()
  await page.getByPlaceholder('备注信息').fill('playwright e2e edited')
  await page.getByRole('dialog').getByRole('button', { name: '确定' }).click()
  await expect(page.getByText('更新成功')).toBeVisible()
  await expect(page.locator('.el-table').getByText('playwright e2e edited')).toBeVisible()

  await page.goto('/holdings')
  await expect(page.getByRole('main').getByText('当前持仓')).toBeVisible()
  await expect(page.getByText('E2E001')).toBeVisible()
  await expect(page.getByText('端到端测试资产')).toBeVisible()
  await expect(page.locator('body')).not.toContainText('NaN')

  // 标的详情页：点击名称跳转，空档案渲染空态与生成入口（不触发真实 LLM）
  await page.getByText('端到端测试资产').click()
  await expect(page).toHaveURL(/\/securities\//)
  await expect(page.getByText('暂无 AI 分析')).toBeVisible()
  await expect(page.getByTestId('generate-analysis-button')).toBeVisible()
  // 商业画像/财报摘要/利润质量三区块按 A股 capabilities 渲染（空态不隐藏）
  await expect(page.getByTestId('business-profile-section')).toBeVisible()
  await expect(page.getByText('暂无商业画像', { exact: false })).toBeVisible()
  await expect(page.getByTestId('report-digest-section')).toBeVisible()
  await expect(page.getByTestId('backfill-digests-button')).toBeVisible()
  await expect(page.getByTestId('earnings-quality-section')).toBeVisible()
  await page.goBack()

  await page.goto('/statistics')
  await expect(page.getByRole('main').getByText('含股息已实现收益：')).toBeVisible()
  // 基准对比选择器（E2E 库无指数数据，空态即向后兼容路径）
  await expect(page.getByTestId('benchmark-select')).toBeVisible()
  await expect(page.locator('body')).not.toContainText('NaN')
})

test('creates a temporary user, verifies analytics curve, and deletes the user', async ({
  page,
  request
}) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  try {
    const token = await loginThroughApi(request, {
      username: createdUser.username,
      password
    })

    const buyResponse = await request.post('http://127.0.0.1:18000/api/transactions', {
      headers: {
        Authorization: `Bearer ${token}`
      },
      data: {
        symbol: 'ANA001',
        name: '统计分析测试资产',
        market: 'A股',
        transaction_type: 'BUY',
        quantity: 100,
        price: 10,
        fee: 0,
        transaction_date: '2026-01-02',
        currency: 'CNY',
        notes: 'analytics e2e buy'
      }
    })
    expect(buyResponse.ok()).toBeTruthy()

    const sellResponse = await request.post('http://127.0.0.1:18000/api/transactions', {
      headers: {
        Authorization: `Bearer ${token}`
      },
      data: {
        symbol: 'ANA001',
        name: '统计分析测试资产',
        market: 'A股',
        transaction_type: 'SELL',
        quantity: 40,
        price: 12,
        fee: 0,
        transaction_date: '2026-02-03',
        currency: 'CNY',
        notes: 'analytics e2e sell'
      }
    })
    expect(sellResponse.ok()).toBeTruthy()

    const holdingsResponse = await request.get('http://127.0.0.1:18000/api/holdings', {
      headers: {
        Authorization: `Bearer ${token}`
      }
    })
    expect(holdingsResponse.ok()).toBeTruthy()
    const holdings = await holdingsResponse.json()
    const holding = holdings.find((row: ApiRow) => row.symbol === 'ANA001')
    expect(holding).toBeTruthy()

    const priceResponse = await request.put(
      `http://127.0.0.1:18000/api/holdings/${holding.id}/price`,
      {
        headers: {
          Authorization: `Bearer ${token}`
        },
        data: {
          current_price: 13
        }
      }
    )
    expect(priceResponse.ok()).toBeTruthy()

    await setAuthenticatedSession(page, token, createdUser)
    await page.goto('/statistics')
    await expect(page.getByRole('main').getByText('证券组合 TTWR 与风险指标')).toBeVisible()
    await expect(page.getByText('实验指标', { exact: true })).toBeVisible()
    await expect(page.getByText('夏普率', { exact: true })).toBeVisible()
    await expect(page.getByText('卡玛率', { exact: true })).toBeVisible()
    await expect(page.locator('.chart-performance canvas')).toBeVisible()
    await expect(page.locator('body')).not.toContainText('NaN')
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

test('foreign-currency holding converts with loaded rates in the ranking table', async ({
  page,
  request
}) => {
  // [PR #177 复审回归] useExchangeRates 状态必须全局共享：拆分后加载与换算
  // 曾分属不同实例，SGD 成本在持仓排行里按原币数值伪装成 CNY（≈ S$1000 显示
  // 成 ¥1,000）。断言 ≈ 行是按已加载汇率折算后的金额。
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  try {
    const token = await loginThroughApi(request, {
      username: createdUser.username,
      password
    })

    // 全局汇率表写入一个辨识度高的 SGD 汇率（幂等 upsert）
    const rateResponse = await request.post('http://127.0.0.1:18000/api/exchange-rates/', {
      headers: { Authorization: `Bearer ${token}` },
      data: {
        from_currency: 'SGD',
        to_currency: 'CNY',
        rate: 5.5,
        effective_date: '2026-01-01',
        source: 'manual'
      }
    })
    expect(rateResponse.ok()).toBeTruthy()

    const buyResponse = await request.post('http://127.0.0.1:18000/api/transactions', {
      headers: { Authorization: `Bearer ${token}` },
      data: {
        symbol: 'FX001',
        name: '外币折算测试资产',
        market: '新加坡股',
        transaction_type: 'BUY',
        quantity: 100,
        price: 10,
        fee: 0,
        transaction_date: '2026-01-02',
        currency: 'SGD',
        notes: 'fx conversion e2e buy'
      }
    })
    expect(buyResponse.ok()).toBeTruthy()

    await setAuthenticatedSession(page, token, createdUser)
    await page.goto('/statistics')

    // 持仓排行：总成本原币 S$1,000，≈ 行必须是已折算的 ¥5,500（而不是把
    // 原币数值当 CNY 的 ¥1,000）
    const rankingCard = page.locator('.el-card', { hasText: '持仓排行' })
    const row = rankingCard.locator('tr', { hasText: 'FX001' })
    await expect(row).toContainText('S$1,000')
    await expect(row).toContainText('≈ ¥5,500', { timeout: 10000 })
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

test('mobile layout uses drawer navigation and card lists', async ({ page, request }) => {
  const token = await loginThroughApi(request)

  const createResponse = await request.post('http://127.0.0.1:18000/api/transactions', {
    headers: {
      Authorization: `Bearer ${token}`
    },
    data: {
      symbol: 'MOB001',
      name: '移动端测试资产',
      market: '美股',
      transaction_type: 'BUY',
      quantity: 8,
      price: 25.5,
      fee: 1,
      transaction_date: '2026-05-14',
      currency: 'USD',
      notes: 'mobile layout'
    }
  })
  expect(createResponse.ok()).toBeTruthy()

  await page.setViewportSize({ width: 375, height: 667 })
  await setAuthenticatedSession(page, token)

  await page.goto('/holdings')
  await expect(page.getByRole('button', { name: '打开导航' })).toBeVisible()
  await expect(page.locator('.header-menu')).toBeHidden()
  await expect(page.locator('.desktop-data-table')).toBeHidden()
  // 定位用 data-testid 而非 .mobile-card：后者是全站共用的外观 class，
  // 改样式就会连带改断言对象（PR #99 重命名时正是这里断的）
  await expect(page.getByTestId('holding-card').filter({ hasText: 'MOB001' })).toBeVisible()
  await expect(page.getByTestId('holding-card').filter({ hasText: '移动端测试资产' })).toBeVisible()

  const holdingsOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth
  )
  expect(holdingsOverflow).toBeFalsy()

  await page.getByRole('button', { name: '打开导航' }).click()
  await expect(page.locator('.mobile-nav-drawer').getByText('交易记录')).toBeVisible()
  await page.locator('.mobile-nav-drawer').getByText('交易记录').click()
  await expect(page).toHaveURL(/\/transactions/)

  await expect(page.locator('.desktop-data-table')).toBeHidden()
  await expect(page.getByTestId('transaction-card').filter({ hasText: 'MOB001' })).toBeVisible()

  const transactionsOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth
  )
  expect(transactionsOverflow).toBeFalsy()
})

// 送股与拆股都允许"比例或绝对数量"二选一（CorporateActionCreate 的
// validate_quantity_fields）。桌面表格与移动卡片共用同一个 actionDetail()，
// 一旦回到模板插值，两端会一起把缺失字段显示成 literal null 并丢掉有效值。
test('corporate action detail renders both fallback shapes on desktop and mobile', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  const headers = { Authorization: `Bearer ${token}` }

  // 只填绝对股数的送股：比例缺失
  const bonus = await request.post('http://127.0.0.1:18000/api/corporate-actions', {
    headers,
    data: {
      symbol: 'FBK001',
      name: '送股绝对数量',
      market: 'A股',
      action_type: 'BONUS_ISSUE',
      ex_date: '2026-04-01',
      shares_received: 12
    }
  })
  expect(bonus.ok(), `送股种子失败: ${bonus.status()} ${await bonus.text()}`).toBeTruthy()

  // 只填拆后股数的拆股：比例缺失，new_shares 是唯一有效值
  const split = await request.post('http://127.0.0.1:18000/api/corporate-actions', {
    headers,
    data: {
      symbol: 'FBK002',
      name: '拆股绝对数量',
      market: 'A股',
      action_type: 'STOCK_SPLIT',
      ex_date: '2026-04-02',
      new_shares: 400
    }
  })
  expect(split.ok(), `拆股种子失败: ${split.status()} ${await split.text()}`).toBeTruthy()

  await setAuthenticatedSession(page, token)
  await page.goto('/corporate-actions')
  await page.waitForLoadState('networkidle')

  for (const [symbol, expected] of [
    ['FBK001', '获得股数: 12.00'],
    ['FBK002', '拆后股数: 400.00']
  ]) {
    const row = page.locator('tr', { hasText: symbol }).first()
    await expect(row, `${symbol} 桌面行未渲染`).toContainText(expected)
    await expect(row, `${symbol} 桌面详情出现 literal null`).not.toContainText('null')
  }

  await page.setViewportSize({ width: 393, height: 851 })
  await page.reload()
  await page.waitForLoadState('networkidle')

  for (const [symbol, expected] of [
    ['FBK001', '获得股数: 12.00'],
    ['FBK002', '拆后股数: 400.00']
  ]) {
    const card = page.getByTestId('corporate-action-card').filter({ hasText: symbol }).first()
    await expect(card, `${symbol} 移动卡片未渲染`).toContainText(expected)
    await expect(card, `${symbol} 移动详情出现 literal null`).not.toContainText('null')
  }
})

test('imports IBKR relisting activity without corrupting holdings', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  // 与旧硬编码 KNOWN_RELISTINGS / KNOWN_SECURITY_NAMES 等价的表驱动规则
  await ensureSecurityRule(request, token, {
    rule_type: 'RELISTING',
    symbol: '01263',
    market: '港股',
    payload: {
      new_symbol: 'PCT',
      new_market: '新加坡股',
      new_currency: 'SGD',
      old_currency: 'HKD',
      name: '柏能集团'
    }
  })
  await ensureSecurityRule(request, token, {
    rule_type: 'NAME_OVERRIDE',
    symbol: '01263',
    market: '港股',
    payload: { name: '柏能集团' }
  })
  await ensureSecurityRule(request, token, {
    rule_type: 'NAME_OVERRIDE',
    symbol: 'PCT',
    market: '新加坡股',
    payload: { name: '柏能集团' }
  })
  const brokerAccount = await createIbkrBrokerAccount(request, token)
  const csv = ibkrCsv([
    'Transaction History,Data,2026-05-04,U***67968,PC PARTNER GROUP LTD,卖,PCT,-1000.0,1.91,SGD,1495.3963,-1.957325,1493.438975',
    'Transaction History,Data,2025-12-09,U***67968,PC PARTNER GROUP LTD,买,1263,2000.0,5.41,HKD,-1390.3700000000001,-2.313,-1394.1361255450001',
    'Transaction History,Data,2025-10-30,U***67968,PC PARTNER GROUP LTD,买,1263,2000.0,6.31,HKD,-1624.1940000000002,-2.3166,-1628.229989529',
    'Transaction History,Data,2025-10-09,U***67968,PC PARTNER GROUP LTD,买,1263,2000.0,7.09,HKD,-1822.2718000000002,-2.31318,-1826.5645647463002',
    'Transaction History,Data,2026-04-14,U***67968,PYPL 17APR26 40 P,买,PYPL  260417P00040000,1.0,0.01,USD,-1.0,-0.56795,-1.56795',
    'Transaction History,Data,2026-03-20,U***67968,卖 -100 INVESCO CURRENCYSHARES EURO (行使),行权,FXE,-100.0,107.0,USD,10700.0,-0.0195,10699.9805'
  ])

  const importResponse = await importIbkrCsv(request, token, brokerAccount.id, csv)
  expect(importResponse.ok()).toBeTruthy()
  const importResult = await importResponse.json()
  expect(importResult.eligible_trade_rows).toBe(4)
  expect(importResult.skipped_option_rows).toBe(2)
  expect(importResult.imported_transactions).toBe(6)
  expect(importResult.errors).toEqual([])

  const holdingsResponse = await request.get('http://127.0.0.1:18000/api/holdings', {
    headers: {
      Authorization: `Bearer ${token}`
    }
  })
  expect(holdingsResponse.ok()).toBeTruthy()
  const holdings = await holdingsResponse.json()
  const pct = holdings.find((row: ApiRow) => row.symbol === 'PCT' && row.market === '新加坡股')
  const oldHk = holdings.find((row: ApiRow) => row.symbol === '01263' && row.market === '港股')

  expect(oldHk).toBeUndefined()
  expect(pct).toBeTruthy()
  expect(pct.name).toBe('柏能集团')
  expect(Number(pct.quantity)).toBe(5000)
  expect(pct.currency).toBe('SGD')
  expect(Number(pct.avg_cost)).toBeGreaterThan(1)
  expect(Number(pct.avg_cost)).toBeLessThan(1.1)

  await setAuthenticatedSession(page, token)
  await page.goto('/holdings')
  await expect(page.getByRole('main').getByText('当前持仓')).toBeVisible()
  const pctRow = page.locator('.el-table__body tr', { hasText: 'PCT' })
  await expect(pctRow).toBeVisible()
  await expect(pctRow.getByText('柏能集团')).toBeVisible()
  await expect(pctRow.getByText('新加坡股')).toBeVisible()
  await expect(pctRow.getByText('SGD')).toBeVisible()
  await expect(page.locator('body')).not.toContainText('01263')
  await expect(page.locator('body')).not.toContainText('NaN')
})

test('IBKR preview reports bookable cash rows separately from skips', async ({ request }) => {
  const token = await loginThroughApi(request)
  const brokerAccount = await createIbkrBrokerAccount(request, token)
  const csv = ibkrCsv([
    'Transaction History,Data,2025-01-22,U***67968,电子资金转账,存款,-,-,-,-,50000.0,-,50000.0',
    'Transaction History,Data,2026-05-05,U***67968,USD 贷方利息- 四月-2026,贷方利息,-,-,-,-,5.14,-,5.14',
    'Transaction History,Data,2026-05-12,U***67968,FX Translations P&L,调整,-,-,-,-,-30200.61,-,-30200.61',
    'Transaction History,Data,2026-02-03,U***67968,"外汇交易基础货币净额: 10,000 USD.HKD",外汇交易组成部分,USD.HKD,10000.0,7.81201,HKD,-0.59,-2.0,-0.59'
  ])

  const previewResponse = await request.post(
    'http://127.0.0.1:18000/api/import/ibkr-activity/preview',
    {
      headers: { Authorization: `Bearer ${token}` },
      multipart: {
        broker_account_id: String(brokerAccount.id),
        file: {
          name: 'ibkr-cash-preview.csv',
          mimeType: 'text/csv',
          buffer: Buffer.from(csv, 'utf8')
        }
      }
    }
  )
  expect(previewResponse.ok()).toBeTruthy()
  const preview = await previewResponse.json()
  // 响应契约回归：这两个字段曾被响应模型静默丢弃
  expect(preview.eligible_cash_event_rows).toBe(2)
  expect(preview.eligible_fx_rows).toBe(1)
  // 预览语义回归：可入账行不得再显示为"跳过"，仅剩「调整」
  expect(preview.skipped_cash_rows).toBe(1)
  expect(preview.skipped_fx_rows).toBe(0)
  expect(preview.expected_archived_rows).toBe(1)

  const importResponse = await importIbkrCsv(request, token, brokerAccount.id, csv, 'ibkr-cash.csv')
  expect(importResponse.ok()).toBeTruthy()
  const importResult = await importResponse.json()
  // 存款 + 利息 + 外汇两腿 + 外汇佣金 = 5 个现金事件
  expect(importResult.imported_cash_events).toBe(5)
  expect(importResult.batch_status).toBe('COMPLETED')
})

test('transfers a holding between broker accounts through the UI', async ({ page, request }) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  const token = await loginThroughApi(request, { username: createdUser.username, password })
  const headers = { Authorization: `Bearer ${token}` }

  try {
    // 两个券商账户 + 一笔买入（归属第一个账户）
    const accountResponses = await Promise.all(
      ['转仓测试-CMB', '转仓测试-IBKR'].map((name) =>
        request.post('http://127.0.0.1:18000/api/broker-accounts', {
          headers,
          data: { broker: name, account_name: name, base_currency: 'CNY' }
        })
      )
    )
    for (const response of accountResponses) expect(response.ok()).toBeTruthy()
    const [fromAccount, toAccount] = await Promise.all(accountResponses.map((r) => r.json()))

    const buyResponse = await request.post('http://127.0.0.1:18000/api/transactions', {
      headers,
      data: {
        broker_account_id: fromAccount.id,
        symbol: 'TRF001',
        name: '转仓标的',
        market: 'A股',
        transaction_type: 'BUY',
        quantity: 100,
        price: 10,
        fee: 0,
        transaction_date: '2026-01-05',
        currency: 'CNY'
      }
    })
    expect(buyResponse.ok()).toBeTruthy()

    await setAuthenticatedSession(page, token, createdUser)

    // 先访问交易页预热 Pinia 缓存：转仓后返回必须看到新交易（缓存失效路径）
    await page.goto('/transactions')
    await expect(page.locator('.el-table__row', { hasText: 'TRF001' })).toHaveCount(1)

    await page.goto('/holdings')
    const row = page.locator('.el-table__row', { hasText: 'TRF001' })
    await expect(row).toContainText('转仓测试-CMB')

    // 打开转仓对话框
    await row.getByRole('button', { name: '转仓' }).click()
    const dialog = page.locator('.el-dialog', { hasText: '账户间转仓' })
    await expect(dialog).toBeVisible()

    // 未选择转入账户时不能提交（null 不再兼作默认值）
    await dialog.getByRole('button', { name: '确认转仓' }).click()
    await expect(page.locator('.el-message--warning')).toContainText('请选择转入账户')
    await expect(dialog).toBeVisible()

    // 选择目标账户：转 40 股到第二个账户
    await dialog.locator('.el-select').click()
    await page
      .locator('.el-select-dropdown:visible .el-select-dropdown__item', {
        hasText: '转仓测试-IBKR'
      })
      .click()
    const quantityInput = dialog.locator('.el-input-number input')
    await quantityInput.fill('40')
    await dialog.getByRole('button', { name: '确认转仓' }).click()
    await expect(page.locator('.el-message--success')).toContainText('转仓成功')

    // 两行持仓：CMB 60 / IBKR 40
    const rows = page.locator('.el-table__row', { hasText: 'TRF001' })
    await expect(rows).toHaveCount(2)
    await expect(page.locator('.el-table__row', { hasText: '转仓测试-IBKR' })).toContainText('40')

    // 后端校验：两条账户级持仓，成本跟随迁移
    const holdingsResponse = await request.get('http://127.0.0.1:18000/api/holdings', { headers })
    const holdings = await holdingsResponse.json()
    const transferRows = holdings.filter((h: ApiRow) => h.symbol === 'TRF001')
    expect(transferRows).toHaveLength(2)
    const byAccount = Object.fromEntries(transferRows.map((h: ApiRow) => [h.broker_account_id, h]))
    expect(parseFloat(byAccount[fromAccount.id].quantity)).toBe(60)
    expect(parseFloat(byAccount[toAccount.id].quantity)).toBe(40)
    expect(parseFloat(byAccount[toAccount.id].avg_cost)).toBeCloseTo(10, 6)

    // 交易列表出现互指转仓对
    const txnResponse = await request.get(
      'http://127.0.0.1:18000/api/transactions?transaction_type=TRANSFER_OUT',
      { headers }
    )
    const outLegs = await txnResponse.json()
    expect(outLegs).toHaveLength(1)
    expect(outLegs[0].linked_transaction_id).not.toBeNull()

    // 返回交易页：Pinia 缓存已失效，UI 能看到新的转仓腿
    await page.goto('/transactions')
    await expect(page.locator('.el-table__row', { hasText: '转出' })).toHaveCount(1)
    await expect(page.locator('.el-table__row', { hasText: '转入' })).toHaveCount(1)
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

test.describe('positive-UTC timezone', () => {
  test.use({ timezoneId: 'Asia/Shanghai' })

  test('transfer dialog defaults to the local date, not the UTC date', async ({
    page,
    request
  }) => {
    const { adminToken, createdUser, password } = await createTemporaryUser(request)
    const token = await loginThroughApi(request, {
      username: createdUser.username,
      password
    })
    const headers = { Authorization: `Bearer ${token}` }

    try {
      const accountResponse = await request.post('http://127.0.0.1:18000/api/broker-accounts', {
        headers,
        data: { broker: '时区测试', account_name: '时区测试', base_currency: 'CNY' }
      })
      expect(accountResponse.ok()).toBeTruthy()
      const account = await accountResponse.json()

      const buyResponse = await request.post('http://127.0.0.1:18000/api/transactions', {
        headers,
        data: {
          broker_account_id: account.id,
          symbol: 'TZ0001',
          name: '时区标的',
          market: 'A股',
          transaction_type: 'BUY',
          quantity: 10,
          price: 10,
          fee: 0,
          transaction_date: '2026-01-05',
          currency: 'CNY'
        }
      })
      expect(buyResponse.ok()).toBeTruthy()

      await setAuthenticatedSession(page, token, createdUser)
      // UTC 2026-03-09 23:30 = 上海 2026-03-10 07:30：toISOString 会错取 03-09
      await page.clock.setFixedTime(new Date('2026-03-09T23:30:00Z'))
      await page.goto('/holdings')
      const row = page.locator('.el-table__row', { hasText: 'TZ0001' })
      await row.getByRole('button', { name: '转仓' }).click()
      const dialog = page.locator('.el-dialog', { hasText: '账户间转仓' })
      await expect(dialog).toBeVisible()
      await expect(dialog.locator('.el-date-editor input')).toHaveValue('2026-03-10')
    } finally {
      await deleteTemporaryUser(request, adminToken, createdUser.id)
    }
  })
})

test('reconciliation snapshots auto-compare and expose diff details', async ({ page, request }) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  const token = await loginThroughApi(request, {
    username: createdUser.username,
    password
  })
  const headers = { Authorization: `Bearer ${token}` }

  try {
    const accountResponse = await request.post('http://127.0.0.1:18000/api/broker-accounts', {
      headers,
      data: { broker: '对账测试', account_name: '对账测试', base_currency: 'CNY' }
    })
    expect(accountResponse.ok()).toBeTruthy()
    const account = await accountResponse.json()

    // 入金 2000（现金参与整体判定：现金未闭合的账户不能整体绿灯）
    const depositResponse = await request.post('http://127.0.0.1:18000/api/cash-events', {
      headers,
      data: {
        broker_account_id: account.id,
        event_type: 'DEPOSIT',
        amount: 2000,
        currency: 'CNY',
        event_date: '2026-01-02'
      }
    })
    expect(depositResponse.ok()).toBeTruthy()

    const buyResponse = await request.post('http://127.0.0.1:18000/api/transactions', {
      headers,
      data: {
        broker_account_id: account.id,
        symbol: 'REC001',
        name: '对账标的',
        market: 'A股',
        transaction_type: 'BUY',
        quantity: 100,
        price: 10,
        fee: 0,
        transaction_date: '2026-01-05',
        currency: 'CNY'
      }
    })
    expect(buyResponse.ok()).toBeTruthy()

    // 快照 A：持仓与现金（2000−1000=1000）都一致 → 创建即 MATCHED
    // 快照 B：券商实际 120 股 / 现金 800，系统漏录 20 股 → MISMATCHED
    const matchedSnapshot = await request.post(
      'http://127.0.0.1:18000/api/reconciliation-snapshots',
      {
        headers,
        data: {
          broker_account_id: account.id,
          snapshot_date: '2026-01-31',
          positions: [{ symbol: 'REC001', market: 'A股', quantity: 100 }],
          cash_balances: { CNY: 1000 }
        }
      }
    )
    expect(matchedSnapshot.ok()).toBeTruthy()
    expect((await matchedSnapshot.json()).status).toBe('MATCHED')

    const mismatchedSnapshot = await request.post(
      'http://127.0.0.1:18000/api/reconciliation-snapshots',
      {
        headers,
        data: {
          broker_account_id: account.id,
          snapshot_date: '2026-02-28',
          positions: [{ symbol: 'REC001', market: 'A股', quantity: 120 }],
          cash_balances: { CNY: 800 }
        }
      }
    )
    expect(mismatchedSnapshot.ok()).toBeTruthy()
    const mismatchedBody = await mismatchedSnapshot.json()
    expect(mismatchedBody.status).toBe('MISMATCHED')
    expect(mismatchedBody.diff_detail.summary.position_mismatches).toBe(1)

    // UI：月末核对 tab 显示红绿标记，点击红色状态打开 diff 明细
    await setAuthenticatedSession(page, token, createdUser)
    await page.goto('/account-data')
    await page.getByRole('tab', { name: '月末核对' }).click()
    await expect(page.locator('.el-tag', { hasText: '比对一致' })).toBeVisible()
    const dangerTag = page.locator('.el-tag', { hasText: '有差异' })
    await expect(dangerTag).toBeVisible()
    await dangerTag.click()
    const dialog = page.locator('.el-dialog', { hasText: '对账比对详情' })
    await expect(dialog).toBeVisible()
    await expect(dialog).toContainText('REC001')
    await expect(dialog).toContainText('数量差')
    await dialog.locator('.el-dialog__headerbtn').click()

    // 补录缺失的 20 股后点"重新比对" → 变绿
    const fixResponse = await request.post('http://127.0.0.1:18000/api/transactions', {
      headers,
      data: {
        broker_account_id: account.id,
        symbol: 'REC001',
        name: '对账标的',
        market: 'A股',
        transaction_type: 'BUY',
        quantity: 20,
        price: 10,
        fee: 0,
        transaction_date: '2026-02-10',
        currency: 'CNY'
      }
    })
    expect(fixResponse.ok()).toBeTruthy()

    const mismatchedRow = page.locator('.el-table__row', { hasText: '2026/02/28' })
    await mismatchedRow.getByRole('button', { name: '重新比对' }).click()
    await expect(page.locator('.el-message--success')).toContainText('比对一致')
    // 关闭的 dialog 仍留在 DOM（保留旧 row 引用），只断言可见标签
    await expect(page.locator('.el-tag:visible', { hasText: '有差异' })).toHaveCount(0)

    // issue #58：分范围快照的重新比对 toast 须显示"持仓一致"而非"比对一致"。
    // scoped 快照只能由对账单导入器创建，这里拦截 compare 响应注入 statement_scope
    // 来验证前端 toast 复用 snapshotStatusLabel 的分支。
    await page.route('**/api/reconciliation-snapshots/*/compare', async (route) => {
      const response = await route.fetch()
      const body = await response.json()
      await route.fulfill({ response, json: { ...body, statement_scope: 'stock' } })
    })
    const matchedRow = page.locator('.el-table__row', { hasText: '2026/01/31' })
    await matchedRow.getByRole('button', { name: '重新比对' }).click()
    await expect(page.locator('.el-message--success').last()).toContainText('持仓一致')
    await page.unroute('**/api/reconciliation-snapshots/*/compare')
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

test('dividend suggestion accept failure refreshes list to matched state', async ({
  page,
  request
}) => {
  // 后端在迟到入账重判重命中时返回 409 并把建议转为 MATCHED；前端必须
  // 刷新列表反映真实状态并关闭弹窗，而不是留着一个可重试的 NEW 行。
  // 建议只能由 Tushare 同步产生，这里用路由桩模拟后端状态转换。
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  const token = await loginThroughApi(request, {
    username: createdUser.username,
    password
  })
  try {
    await setAuthenticatedSession(page, token, createdUser)
    const baseRow = {
      id: 9901,
      broker_account_id: 4242,
      symbol: '600036',
      name: '招商银行',
      market: 'A股',
      action_type: 'CASH_DIVIDEND',
      ann_date: null,
      record_date: null,
      ex_date: '2026-07-01',
      pay_date: '2026-07-02',
      currency: 'CNY',
      cash_div_pre_tax: '1.0',
      cash_div_after_tax: '0.9',
      stk_div_per_share: null,
      record_date_quantity: '1000',
      quantity_basis: 'per_account',
      estimated_total_dividend: '1000',
      status: 'NEW',
      matched_corporate_action_id: null,
      created_corporate_action_id: null,
      match_detail: null,
      source: 'tushare-dividend',
      created_at: '2026-07-01T00:00:00Z',
      updated_at: '2026-07-01T00:00:00Z'
    }
    let accepted = false
    let acceptBody: Record<string, unknown> | null = null
    await page.route('**/api/broker-accounts*', async (route) => {
      await route.fulfill({
        json: [{ id: 4242, account_name: '桩账户', broker: '桩券商', base_currency: 'CNY' }]
      })
    })
    await page.route('**/api/corporate-actions/suggestions?*', async (route) => {
      await route.fulfill({
        json: [
          accepted ? { ...baseRow, status: 'MATCHED', matched_corporate_action_id: 777 } : baseRow
        ]
      })
    })
    await page.route('**/api/corporate-actions/suggestions/count', async (route) => {
      await route.fulfill({ json: { total: accepted ? 0 : 1 } })
    })
    await page.route('**/api/corporate-actions/suggestions/9901/accept', async (route) => {
      accepted = true
      acceptBody = route.request().postDataJSON()
      await route.fulfill({
        status: 409,
        json: {
          detail: '账本中已存在匹配的分红记录（公司行动 #777），未重复入账；建议已标记为已匹配。'
        }
      })
    })

    await page.goto('/corporate-actions')
    await page.getByRole('tab', { name: /分红建议/ }).click()
    await page.getByRole('button', { name: '接受' }).click()
    const dialog = page.locator('.el-dialog', { hasText: '接受分红建议' })
    await expect(dialog).toBeVisible()
    // 弹窗按建议行预填账户；用户清空选择（el-select 清空把 model 置为
    // undefined）——请求体必须显式携带 broker_account_id: null，而不是丢键
    // 让后端沿用原账户
    const accountSelect = dialog.locator('.el-select')
    await expect(accountSelect).toContainText('桩账户')
    await accountSelect.hover()
    await accountSelect.locator('.el-select__caret').click()
    await dialog.getByRole('button', { name: '确认入账' }).click()

    await expect(page.locator('.el-message--error')).toContainText('未重复入账')
    // 真实请求体回归：键存在且值为 null（undefined 会被 JSON 序列化丢键）
    expect(acceptBody).not.toBeNull()
    expect(acceptBody).toHaveProperty('broker_account_id', null)
    // 列表已刷新为 MATCHED：弹窗关闭、行内显示"已在账"、不再有接受入口
    await expect(dialog).not.toBeVisible()
    await expect(page.getByText('已在账')).toBeVisible()
    await expect(page.getByRole('button', { name: '接受' })).toHaveCount(0)
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

test('leaving corporate actions mid dividend-sync stops follow-up requests', async ({
  page,
  request
}) => {
  // [PR #171 复审回归] 挂起 job 轮询 → 客户端路由离开 → 放行响应：
  // pollJobUntilDone 被卸载守卫取消后必须直接收手——不得再请求建议列表/
  // 计数（卸载后的请求与状态写入），也不得在别的页面弹迟到消息。
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  let suggestionRequests = 0
  await page.route(/\/api\/corporate-actions\/suggestions(\/count)?(\?|$)/, async (route) => {
    suggestionRequests += 1
    const url = route.request().url()
    await route.fulfill({ json: url.includes('/count') ? { total: 0 } : [] })
  })
  await page.route('**/api/corporate-actions/dividend-sync-jobs', (route) =>
    route.fulfill({ json: { id: 'sync-race', status: 'queued' } })
  )
  let releasePoll: () => void = () => {}
  const pollGate = new Promise<void>((resolve) => {
    releasePoll = resolve
  })
  await page.route('**/api/corporate-actions/dividend-sync-jobs/sync-race', async (route) => {
    await pollGate
    await route.fulfill({ json: { id: 'sync-race', status: 'running' } })
  })

  await page.goto('/corporate-actions')
  await page.getByRole('tab', { name: /分红建议/ }).click()
  const firstPoll = page.waitForRequest('**/api/corporate-actions/dividend-sync-jobs/sync-race')
  await page.getByTestId('dividend-sync-button').click()
  await firstPoll // 轮询响应此刻挂在闸上

  const requestsBeforeLeave = suggestionRequests
  // 客户端路由切走（SPA 存活、CorporateActions 卸载）——整页 goto 会销毁
  // JS 上下文，复现不了这个竞态
  await page.locator('.el-menu-item', { hasText: '交易记录' }).first().click()
  await expect(page).toHaveURL(/\/transactions/)

  releasePoll() // 放行挂起的响应 → 轮询以取消（null）收场
  await page.waitForTimeout(800)

  expect(suggestionRequests).toBe(requestsBeforeLeave)
  await expect(page.locator('.el-message')).toHaveCount(0)
})

test('security rules API round-trip and the 特例规则 tab lists rules', async ({
  page,
  request
}) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  const token = await loginThroughApi(request, { username: createdUser.username, password })
  const headers = { Authorization: `Bearer ${token}` }
  const relistingRule = {
    rule_type: 'RELISTING',
    symbol: '01263',
    market: '港股',
    payload: {
      new_symbol: 'PCT',
      new_market: '新加坡股',
      new_currency: 'SGD',
      old_currency: 'HKD',
      name: '柏能集团'
    },
    note: 'e2e 转板映射'
  }

  try {
    // 创建 RELISTING 规则：201 且 payload 原样回显
    const createResponse = await request.post('http://127.0.0.1:18000/api/security-rules', {
      headers,
      data: relistingRule
    })
    expect(createResponse.status()).toBe(201)
    const created = await createResponse.json()
    expect(created.rule_type).toBe('RELISTING')
    expect(created.market).toBe('港股')
    expect(created.payload).toEqual(relistingRule.payload)

    // rule_type 过滤列表包含刚创建的规则
    const listResponse = await request.get(
      'http://127.0.0.1:18000/api/security-rules?rule_type=RELISTING',
      { headers }
    )
    expect(listResponse.ok()).toBeTruthy()
    const rows = await listResponse.json()
    expect(rows.some((row: ApiRow) => row.id === created.id)).toBeTruthy()

    // 同类型同键重复创建 → 409
    const duplicateResponse = await request.post('http://127.0.0.1:18000/api/security-rules', {
      headers,
      data: relistingRule
    })
    expect(duplicateResponse.status()).toBe(409)

    // UI：特例规则 tab 显示该规则行及其摘要
    await setAuthenticatedSession(page, token, createdUser)
    await page.goto('/account-data')
    await page.getByRole('tab', { name: '特例规则' }).click()
    const ruleRow = page.locator('.el-table__row', { hasText: '01263' })
    await expect(ruleRow).toBeVisible()
    await expect(ruleRow).toContainText('转板映射')
    await expect(ruleRow).toContainText('→ PCT 新加坡股 SGD')

    // 删除 → 204
    const deleteResponse = await request.delete(
      `http://127.0.0.1:18000/api/security-rules/${created.id}`,
      { headers }
    )
    expect(deleteResponse.status()).toBe(204)
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

test('security rules dialog builds per-type payloads and filter ignores stale responses', async ({
  page,
  request
}) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  const token = await loginThroughApi(request, { username: createdUser.username, password })

  // el-select 选项渲染在 body 传送门里；表单项必须按 label 元素精确匹配
  // （hasText 子串匹配会被提示文案里的"新市场"等词误中）
  const dialog = () => page.getByRole('dialog')
  const pickSelect = async (itemLabel: string, optionName: string | RegExp) => {
    await dialog()
      .locator('.el-form-item')
      .filter({
        has: page.locator('.el-form-item__label', { hasText: new RegExp(`^${itemLabel}$`) })
      })
      .first()
      .locator('.el-select')
      .first()
      .click()
    await page.getByRole('option', { name: optionName }).first().click()
  }

  try {
    await setAuthenticatedSession(page, token, createdUser)
    await page.goto('/account-data')
    await page.getByRole('tab', { name: '特例规则' }).click()

    // ---- 动态表单 1：RELISTING（含五个 payload 字段的组装） ----
    await page.getByRole('button', { name: '新增规则' }).click()
    await pickSelect('规则类型', /转板映射/)
    await dialog().getByPlaceholder('如 511880').fill('01263')
    // 市场选项必须与后端 VALID_MARKETS 对齐（检视回归：曾缺"加密货币"）
    await dialog()
      .locator('.el-form-item')
      .filter({ has: page.locator('.el-form-item__label', { hasText: /^市场$/ }) })
      .first()
      .locator('.el-select')
      .first()
      .click()
    await expect(
      page.locator('.el-select-dropdown:visible').getByRole('option', { name: '加密货币' })
    ).toBeVisible()
    await page
      .locator('.el-select-dropdown:visible')
      .getByRole('option', { name: /^港股$/ })
      .click()
    await pickSelect('旧币种', /^HKD$/)
    await dialog().getByPlaceholder('如 PCT').fill('PCT')
    await pickSelect('新市场', /^新加坡股$/)
    await pickSelect('新币种', /^SGD$/)
    await dialog().getByPlaceholder('转板后标的名称，如 柏能集团').fill('柏能集团')
    const relistingRequest = page.waitForRequest(
      (req) => req.url().includes('/api/security-rules') && req.method() === 'POST'
    )
    await dialog().getByRole('button', { name: '保存' }).click()
    const relistingPayload = (await relistingRequest).postDataJSON()
    expect(relistingPayload).toMatchObject({
      rule_type: 'RELISTING',
      symbol: '01263',
      market: '港股',
      payload: {
        new_symbol: 'PCT',
        new_market: '新加坡股',
        new_currency: 'SGD',
        old_currency: 'HKD',
        name: '柏能集团'
      }
    })
    await expect(page.locator('.el-table__row', { hasText: '01263' })).toContainText(
      '→ PCT 新加坡股 SGD'
    )

    // ---- 动态表单 2：CMB 业务映射（market 必须为 null、event_type 组装） ----
    await page.getByRole('button', { name: '新增规则' }).click()
    await pickSelect('规则类型', /招商现金业务/)
    await dialog().getByPlaceholder('如 招现宝收益').fill('银行转存')
    await pickSelect('事件类型', /入金（DEPOSIT）/)
    const cmbRequest = page.waitForRequest(
      (req) => req.url().includes('/api/security-rules') && req.method() === 'POST'
    )
    await dialog().getByRole('button', { name: '保存' }).click()
    const cmbPayload = (await cmbRequest).postDataJSON()
    expect(cmbPayload).toMatchObject({
      rule_type: 'CMB_CASH_BUSINESS',
      symbol: '银行转存',
      payload: { event_type: 'DEPOSIT' }
    })
    expect(cmbPayload.market ?? null).toBeNull()
    await expect(page.locator('.el-table__row', { hasText: '银行转存' })).toContainText('→ DEPOSIT')

    // ---- 竞态回归：慢的未过滤初始加载不得覆盖随后切换的筛选结果 ----
    await page.route('**/api/security-rules*', async (route) => {
      if (!route.request().url().includes('rule_type=')) {
        await new Promise((resolve) => setTimeout(resolve, 4000))
      }
      await route.continue()
    })
    await page.reload()
    await page.getByRole('tab', { name: '特例规则' }).click()
    // 未过滤请求（含两条规则）被延迟 4s，在途时切筛选
    const filterSelect = page
      .locator('.compact-filter .el-form-item', { hasText: '规则类型' })
      .locator('.el-select')
    await filterSelect.click()
    await page
      .locator('.el-select-dropdown:visible')
      .getByRole('option', { name: /招商现金业务/ })
      .click()
    await expect(page.locator('.el-table__row', { hasText: '银行转存' })).toBeVisible()
    // 慢的未过滤响应落地后，不得把 01263（RELISTING）行覆盖回表格
    await page.waitForTimeout(4500)
    await expect(page.locator('.el-table__row', { hasText: '银行转存' })).toBeVisible()
    await expect(page.locator('.el-table__row', { hasText: '01263' })).toHaveCount(0)
    await page.unroute('**/api/security-rules*')
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

test('standard CSV import attributes rows to the selected broker account', async ({
  page,
  request
}) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  const token = await loginThroughApi(request, { username: createdUser.username, password })
  const headers = { Authorization: `Bearer ${token}` }

  try {
    const accountResponse = await request.post('http://127.0.0.1:18000/api/broker-accounts', {
      headers,
      data: { broker: 'HSBC', account_name: 'HSBC 标准导入', base_currency: 'HKD' }
    })
    expect(accountResponse.ok()).toBeTruthy()
    const account = await accountResponse.json()

    await setAuthenticatedSession(page, token, createdUser)
    await page.goto('/transactions')
    await page.getByRole('button', { name: '导入', exact: true }).click()

    // 标准交易 tab（默认）应展示"归属账户"选择器
    const dialog = page.locator('.el-dialog', { hasText: '导入数据' })
    await expect(dialog.getByText('归属账户')).toBeVisible()
    await dialog.locator('.import-account-field .el-select').click()
    await page
      .locator('.el-select-dropdown:visible .el-select-dropdown__item', {
        hasText: 'HSBC 标准导入'
      })
      .click()

    const csv = [
      'symbol,name,market,transaction_type,quantity,price,fee,transaction_date,currency',
      'STDACC1,标准归属标的,港股,BUY,500,4.20,6.30,2026-02-03,HKD'
    ].join('\n')
    await dialog
      .locator('input[type="file"]')
      .setInputFiles({ name: 'std-account.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) })
    await dialog.getByRole('button', { name: '导入', exact: true }).click()

    await expect(page.locator('.el-table__row', { hasText: 'STDACC1' })).toHaveCount(1)

    // 后端校验：交易与重算出的持仓都归属所选账户
    const txnResponse = await request.get(
      'http://127.0.0.1:18000/api/transactions?symbol=STDACC1',
      { headers }
    )
    expect(txnResponse.ok()).toBeTruthy()
    const txnPayload = await txnResponse.json()
    const txns = Array.isArray(txnPayload) ? txnPayload : txnPayload.items
    expect(txns.length).toBe(1)
    expect(txns[0].broker_account_id).toBe(account.id)

    const holdingsResponse = await request.get('http://127.0.0.1:18000/api/holdings', { headers })
    expect(holdingsResponse.ok()).toBeTruthy()
    const holdings = await holdingsResponse.json()
    const holding = (Array.isArray(holdings) ? holdings : holdings.items).find(
      (row: ApiRow) => row.symbol === 'STDACC1'
    )
    expect(holding).toBeTruthy()
    expect(holding.broker_account_id).toBe(account.id)
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

test('slow responses from a previous security never leak into the current one', async ({
  page,
  request
}) => {
  // [评审回归] 同业跳转复用同一路由组件实例：A 的 analysis 响应晚于 B 返回时，
  // 若没有请求身份守卫，A 的分析会写进 B 的页面（标题是 B、内容是 A）。
  // 必须走点击同业的客户端路由——整页 goto 会被浏览器中止在途请求，复现不了。
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  let releaseSlowA: (() => void) | null = null
  const slowAReleased = new Promise<void>((resolve) => {
    releaseSlowA = resolve
  })

  const profileBody = (symbol: string) => ({
    symbol,
    market: 'A股',
    supported: true,
    capabilities: { structured: true, report_digest: true, risk_signals: true },
    datasets: {},
    latest_periods: {},
    events: [],
    report_digests: [],
    digest_progress: { digested: 0, failed_capped: 0 },
    statement_progress: null,
    business: {
      profile: {
        商业模式: `${symbol} 的商业模式说明`,
        业务分部: [],
        上游依赖: [],
        下游需求: [],
        供应商集中度: '未披露',
        客户集中度: '未披露',
        行业与竞争: '—',
        估值观察因子: []
      },
      peers: [{ symbol: 'RACEB', name: '同业标的B', industry: '银行' }],
      industry: '银行'
    },
    earnings_quality: { status: 'no_data' }
  })

  // A 的档案立刻返回（页面渲染出同业链接），A 的分析挂起
  await page.route('**/api/securities/**/profile', async (route) => {
    const isA = route.request().url().includes('RACEA')
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(profileBody(isA ? 'RACEA' : 'RACEB'))
    })
  })

  await page.route('**/api/securities/**/analysis', async (route) => {
    const isA = route.request().url().includes('RACEA')
    if (isA) await slowAReleased
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: isA ? 1 : 2,
        symbol: isA ? 'RACEA' : 'RACEB',
        market: 'A股',
        name: isA ? '标的A' : '标的B',
        tags: [isA ? '业绩下滑' : '业绩增长'],
        risk_level: isA ? 'high' : 'low',
        summary: isA ? 'A的摘要不得出现在B页面' : 'B的摘要',
        content: isA ? '## 财务质量趋势\nA的全文正文' : '## 财务质量趋势\nB的全文正文',
        model: 'deepseek-v4-pro'
      })
    })
  })

  await page.goto('/securities/A股/RACEA')
  await expect(page.getByTestId('peer-list')).toContainText('同业标的B')

  // 点击同业 → 客户端路由切到 B；B 的两个请求都正常返回
  await page.getByTestId('peer-list').getByText('同业标的B').click()
  await expect(page).toHaveURL(/RACEB/)
  await expect(page.getByText('B的摘要')).toBeVisible()
  await expect(page.getByTestId('business-profile-section')).toContainText('RACEB 的商业模式')

  // 放行 A 的迟到响应：不得覆盖 B 的页面
  releaseSlowA!()
  await page.waitForTimeout(1000)

  await expect(page).toHaveURL(/RACEB/)
  await expect(page.getByText('B的摘要')).toBeVisible()
  await expect(page.getByText('A的摘要不得出现在B页面')).toHaveCount(0)
  await expect(page.locator('body')).not.toContainText('A的全文正文')
  await expect(page.getByTestId('risk-level-tag')).toContainText('低')
})

test('graham criteria card is cleared on peer navigation while the new profile is pending', async ({
  page,
  request
}) => {
  // [评审 P2 回归] 同业跳转复用组件实例：路由切到 B 后、B 的 profile 返回前，
  // A 的格雷厄姆准则卡不得继续显示在 B 的标题下；B 的 profile 失败时也不得残留。
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  let releaseB: (() => void) | null = null
  const bReleased = new Promise<void>((resolve) => {
    releaseB = resolve
  })

  const profileBody = (symbol: string, withGraham: boolean) => ({
    symbol,
    market: 'A股',
    supported: true,
    capabilities: { structured: true, report_digest: true, risk_signals: true },
    datasets: {},
    latest_periods: {},
    events: [],
    report_digests: [],
    digest_progress: { digested: 0, failed_capped: 0 },
    statement_progress: null,
    business: {
      profile: null,
      peers: [{ symbol: 'GRMB', name: '同业标的B', industry: '银行' }],
      industry: '银行'
    },
    earnings_quality: { status: 'no_data' },
    graham_screen: withGraham
      ? {
          status: 'ok',
          as_of_year: '2025',
          passed: 1,
          failed: 0,
          indeterminate: 0,
          criteria: [
            { criterion: 'pe', verdict: 'pass', reason: 'A 的准则依据不得残留', value: 9 }
          ],
          fragility: {}
        }
      : { status: 'no_data' }
  })

  await page.route('**/api/securities/**/profile', async (route) => {
    const isA = route.request().url().includes('GRMA')
    if (!isA) {
      await bReleased
      // B 的 profile 失败：残留检查在失败路径同样成立
      await route.fulfill({ status: 500, contentType: 'application/json', body: '{}' })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(profileBody('GRMA', true))
    })
  })
  await page.route('**/api/securities/**/analysis', async (route) => {
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })

  await page.goto('/securities/A股/GRMA')
  await expect(page.getByTestId('graham-screen-section')).toContainText('A 的准则依据不得残留')

  await page.getByTestId('peer-list').getByText('同业标的B').click()
  await expect(page).toHaveURL(/GRMB/)
  // B 的 profile 仍挂起：A 的准则卡必须已经清空
  await expect(page.getByTestId('graham-screen-section')).toHaveCount(0)

  releaseB!()
  await page.waitForTimeout(500)
  await expect(page).toHaveURL(/GRMB/)
  await expect(page.getByTestId('graham-screen-section')).toHaveCount(0)
})

test('watch state ignores an out-of-order response after peer navigation', async ({
  page,
  request
}) => {
  // [评审 P2 回归] A 的 membership 响应（watching=true）挂起 → 点同业跳 B
  // （B 返回 not-watching）→ 放行 A 的迟到响应：不得把 B 页面标成"已在观察清单"。
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  let releaseA: (() => void) | null = null
  const aReleased = new Promise<void>((resolve) => {
    releaseA = resolve
  })

  const profileBody = (symbol: string) => ({
    symbol,
    market: 'A股',
    supported: true,
    capabilities: { structured: true, report_digest: true, risk_signals: true },
    datasets: {},
    latest_periods: {},
    events: [],
    report_digests: [],
    digest_progress: { digested: 0, failed_capped: 0 },
    statement_progress: null,
    business: {
      profile: null,
      peers: [{ symbol: 'WSB', name: '同业标的B', industry: '银行' }],
      industry: '银行'
    },
    earnings_quality: { status: 'no_data' },
    graham_screen: { status: 'no_data' }
  })
  await page.route('**/api/securities/**/profile', async (route) => {
    const isA = route.request().url().includes('WSA')
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(profileBody(isA ? 'WSA' : 'WSB'))
    })
  })
  await page.route('**/api/securities/**/analysis', async (route) => {
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })
  await page.route('**/api/watchlist/contains**', async (route) => {
    const isA = route.request().url().includes('WSA')
    if (isA) await aReleased
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ watching: isA, item_id: isA ? 1 : null })
    })
  })

  await page.goto('/securities/A股/WSA')
  await expect(page.getByTestId('peer-list')).toContainText('同业标的B')
  await page.getByTestId('peer-list').getByText('同业标的B').click()
  await expect(page).toHaveURL(/WSB/)
  await expect(page.getByTestId('add-to-watchlist-button')).toBeVisible()

  releaseA!()
  await page.waitForTimeout(500)
  await expect(page).toHaveURL(/WSB/)
  await expect(page.getByTestId('add-to-watchlist-button')).toBeVisible()
  await expect(page.getByText('已在观察清单', { exact: true })).toHaveCount(0)
})

test('confirming the watch prompt after navigating away does not add the wrong security', async ({
  page,
  request
}) => {
  // [评审 P2 二轮回归] 在 A 页打开「加入观察」prompt → 浏览器 back 到同组件的 B 页
  // → 再点确认：不得把为 A 输入的理由加到 B（也不得给 A 加）。
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  const profileBody = (symbol: string) => ({
    symbol,
    market: 'A股',
    supported: true,
    capabilities: { structured: true, report_digest: true, risk_signals: true },
    datasets: {},
    latest_periods: {},
    events: [],
    report_digests: [],
    digest_progress: { digested: 0, failed_capped: 0 },
    statement_progress: null,
    business: {
      profile: null,
      // B 页挂 A 为同业：用客户端路由跳 A，history 里 B→A 都在同一 SPA 会话内，
      // 之后 history.back() 才是客户端回退（page.goBack 跨硬导航会整页重载、弹窗消失）
      peers: symbol === 'PRB' ? [{ symbol: 'PRA', name: '同业标的A', industry: '银行' }] : [],
      industry: '银行'
    },
    earnings_quality: { status: 'no_data' },
    graham_screen: { status: 'no_data' }
  })
  await page.route('**/api/securities/**/profile', async (route) => {
    const symbol = route.request().url().includes('PRA') ? 'PRA' : 'PRB'
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(profileBody(symbol))
    })
  })
  await page.route('**/api/securities/**/analysis', async (route) => {
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })
  await page.route('**/api/watchlist/contains**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ watching: false, item_id: null })
    })
  })
  const posted: string[] = []
  await page.route('**/api/watchlist', async (route) => {
    if (route.request().method() === 'POST') {
      posted.push(route.request().postDataJSON().symbol)
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ id: 1, symbol: 'X', market: 'A股' })
      })
      return
    }
    await route.continue()
  })

  // 先到 B，再经同业链接客户端跳到 A（同组件复用、同一 SPA history）
  await page.goto('/securities/A股/PRB')
  await expect(page.getByTestId('peer-list')).toContainText('同业标的A')
  await page.getByTestId('peer-list').getByText('同业标的A').click()
  await expect(page).toHaveURL(/PRA/)
  await expect(page.getByTestId('add-to-watchlist-button')).toBeVisible()

  await page.getByTestId('add-to-watchlist-button').click()
  const promptBox = page.locator('.el-message-box', { hasText: '加入观察' })
  await promptBox.locator('textarea').fill('为 A 写的理由')

  // 客户端 history 回退到 B：弹窗独立于路由仍挂着
  await page.evaluate(() => window.history.back())
  await expect(page).toHaveURL(/PRB/)
  await expect(promptBox).toBeVisible()
  // 弹窗仍挂着（ElMessageBox 独立于路由）：此时确认
  await promptBox.getByRole('button', { name: '加入' }).click()
  await page.waitForTimeout(500)

  expect(posted).toEqual([]) // 既不加 B，也不代 A 加
  await expect(page.getByText('已在观察清单', { exact: true })).toHaveCount(0)
})

test('an older request for the same security cannot overwrite a newer one (ABA)', async ({
  page,
  request
}) => {
  // [评审回归] 请求身份不能只用 market/symbol：A₁（慢）→ 切 B → 切回 A 发起
  // A₂（快），A₂ 渲染后 A₁ 才到，业务 key 又相等，旧 A₁ 会覆盖更新的 A₂。
  // 守卫必须是单调递增的请求代次。
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  let releaseA1: (() => void) | null = null
  const a1Released = new Promise<void>((resolve) => {
    releaseA1 = resolve
  })
  let aRequestCount = 0

  const profileBody = (symbol: string, peer: string) => ({
    symbol,
    market: 'A股',
    supported: true,
    capabilities: { structured: true, report_digest: true, risk_signals: true },
    datasets: {},
    latest_periods: {},
    events: [],
    report_digests: [],
    digest_progress: { digested: 0, failed_capped: 0 },
    statement_progress: null,
    business: {
      profile: {
        商业模式: `${symbol} 的商业模式说明`,
        业务分部: [],
        上游依赖: [],
        下游需求: [],
        供应商集中度: '未披露',
        客户集中度: '未披露',
        行业与竞争: '—',
        估值观察因子: []
      },
      peers: [{ symbol: peer, name: `同业${peer}`, industry: '银行' }],
      industry: '银行'
    },
    earnings_quality: { status: 'no_data' }
  })

  await page.route('**/api/securities/**/profile', async (route) => {
    const isA = route.request().url().includes('ABAA')
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(isA ? profileBody('ABAA', 'ABAB') : profileBody('ABAB', 'ABAA'))
    })
  })

  await page.route('**/api/securities/**/analysis', async (route) => {
    const isA = route.request().url().includes('ABAA')
    if (!isA) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 2,
          symbol: 'ABAB',
          market: 'A股',
          name: '标的B',
          tags: ['业绩增长'],
          risk_level: 'medium',
          summary: 'B的摘要',
          content: '## 财务质量趋势\nB的全文',
          model: 'deepseek-v4-pro'
        })
      })
      return
    }
    aRequestCount += 1
    const generation = aRequestCount
    if (generation === 1) await a1Released // A₁ 挂起，A₂ 立即返回
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 1,
        symbol: 'ABAA',
        market: 'A股',
        name: '标的A',
        tags: [generation === 1 ? '业绩下滑' : '业绩增长'],
        risk_level: generation === 1 ? 'high' : 'low',
        summary: generation === 1 ? 'A第一代陈旧摘要' : 'A第二代最新摘要',
        content: `## 财务质量趋势\nA第${generation}代全文`,
        model: 'deepseek-v4-pro'
      })
    })
  })

  // A₁ 发起并挂起
  await page.goto('/securities/A股/ABAA')
  await expect(page.getByTestId('peer-list')).toContainText('同业ABAB')

  // 切到 B（客户端路由），B 正常返回
  await page.getByTestId('peer-list').getByText('同业ABAB').click()
  await expect(page).toHaveURL(/ABAB/)
  await expect(page.getByText('B的摘要')).toBeVisible()

  // 切回 A，发起 A₂ 并立即完成
  await page.getByTestId('peer-list').getByText('同业ABAA').click()
  await expect(page).toHaveURL(/ABAA/)
  await expect(page.getByText('A第二代最新摘要')).toBeVisible()

  // 放行 A₁：同一标的的旧代次请求不得覆盖 A₂
  releaseA1!()
  await page.waitForTimeout(1000)

  await expect(page.getByText('A第二代最新摘要')).toBeVisible()
  await expect(page.getByText('A第一代陈旧摘要')).toHaveCount(0)
  await expect(page.locator('body')).not.toContainText('A第1代全文')
  await expect(page.getByTestId('risk-level-tag')).toContainText('低')
})

test('shows staged progress while generating an AI analysis', async ({ page, request }) => {
  // 进度全部走 route mock，不触发真实 LLM
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  const profileBody = {
    symbol: 'PROG01',
    market: 'A股',
    supported: true,
    capabilities: { structured: true, report_digest: true, risk_signals: true },
    datasets: {},
    latest_periods: {},
    events: [],
    report_digests: [],
    digest_progress: { digested: 0, failed_capped: 0 },
    statement_progress: null,
    business: { profile: null, peers: [], industry: null },
    earnings_quality: { status: 'no_data' }
  }
  await page.route('**/api/securities/**/profile', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(profileBody)
    })
  )
  await page.route('**/api/securities/**/analysis', (route) =>
    route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"无"}' })
  )
  await page.route('**/PROG01/analysis-jobs', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'job-prog',
        status: 'queued',
        stage_label: '排队中',
        total: 6,
        completed: 0,
        progress_percent: 0
      })
    })
  )

  // 阶段脚本：同步基本面 → 生成分析（LLM）→ 成功
  const script = [
    { status: 'running', stage_label: '同步基本面档案', completed: 1, progress_percent: 16.67 },
    { status: 'running', stage_label: '生成分析（LLM）', completed: 5, progress_percent: 83.33 },
    { status: 'succeeded', stage_label: '已完成', completed: 6, progress_percent: 100 }
  ]
  let tick = 0
  await page.route('**/api/securities/analysis-jobs/job-prog', (route) => {
    const payload = script[Math.min(tick, script.length - 1)]
    tick += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ id: 'job-prog', total: 6, ...payload })
    })
  })

  await page.goto('/securities/A股/PROG01')
  await page.getByTestId('generate-analysis-button').click()

  const progress = page.getByTestId('analysis-progress')
  await expect(progress).toContainText('同步基本面档案', { timeout: 15000 })
  await expect(progress).toContainText('1/6')
  await expect(progress).toContainText('生成分析（LLM）', { timeout: 15000 })
  // 成功后进度块收起（分析正文本身即完成证据）
  await expect(page.getByText('分析已生成')).toBeVisible({ timeout: 15000 })
  await expect(progress).toHaveCount(0)
})

test('a failed analysis keeps the progress block with its error', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  await page.route('**/api/securities/**/profile', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        symbol: 'FAIL01',
        market: 'A股',
        supported: true,
        capabilities: { structured: true, report_digest: true, risk_signals: true },
        datasets: {},
        latest_periods: {},
        events: [],
        report_digests: [],
        digest_progress: { digested: 0, failed_capped: 0 },
        statement_progress: null,
        business: { profile: null, peers: [], industry: null },
        earnings_quality: { status: 'no_data' }
      })
    })
  )
  await page.route('**/api/securities/**/analysis', (route) =>
    route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"无"}' })
  )
  await page.route('**/FAIL01/analysis-jobs', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ id: 'job-fail', status: 'queued', total: 6, completed: 0 })
    })
  )
  await page.route('**/api/securities/analysis-jobs/job-fail', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'job-fail',
        status: 'failed',
        stage_label: '生成分析（LLM）',
        total: 6,
        completed: 5,
        progress_percent: 83.33,
        error: 'LLM 输出解析失败：tags 含白名单外标签'
      })
    })
  )

  await page.goto('/securities/A股/FAIL01')
  await page.getByTestId('generate-analysis-button').click()

  const progress = page.getByTestId('analysis-progress')
  await expect(progress).toBeVisible({ timeout: 15000 })
  await expect(progress).toContainText('LLM 输出解析失败')
  await expect(progress.locator('.el-progress.is-exception')).toHaveCount(1)
  // 按钮恢复可用（不再 loading）
  await expect(page.getByTestId('generate-analysis-button')).toBeEnabled()
})

test('analysis progress of a previous security never leaks into the current one', async ({
  page,
  request
}) => {
  // [评审回归] pollJobUntilDone 的 onUpdate 是每轮无条件调用的（取消检查在
  // fetch 之前），所以 onUpdate 内部必须自己判代次。泄漏窗口只在"响应已在
  // 途中时发生了导航"，因此这里必须挂起 A 的轮询响应、切到 B 之后再放行。
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  let releaseA: (() => void) | null = null
  const aReleased = new Promise<void>((resolve) => {
    releaseA = resolve
  })

  const profileBody = (symbol: string, peer: string) => ({
    symbol,
    market: 'A股',
    supported: true,
    capabilities: { structured: true, report_digest: true, risk_signals: true },
    datasets: {},
    latest_periods: {},
    events: [],
    report_digests: [],
    digest_progress: { digested: 0, failed_capped: 0 },
    statement_progress: null,
    business: {
      profile: null,
      peers: [{ symbol: peer, name: `同业${peer}`, industry: '银行' }],
      industry: '银行'
    },
    earnings_quality: { status: 'no_data' }
  })
  await page.route('**/api/securities/**/profile', (route) => {
    const isA = route.request().url().includes('LEAKA')
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(isA ? profileBody('LEAKA', 'LEAKB') : profileBody('LEAKB', 'LEAKA'))
    })
  })
  await page.route('**/api/securities/**/analysis', (route) =>
    route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"无"}' })
  )
  await page.route('**/LEAKA/analysis-jobs', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ id: 'job-leak', status: 'queued', total: 6, completed: 0 })
    })
  )
  // A 的第一次轮询响应挂起，直到 B 已经渲染完成才放行
  await page.route('**/api/securities/analysis-jobs/job-leak', async (route) => {
    await aReleased
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'job-leak',
        status: 'running',
        stage_label: 'A标的的分析阶段',
        total: 6,
        completed: 3,
        progress_percent: 50
      })
    })
  })

  await page.goto('/securities/A股/LEAKA')
  await page.getByTestId('generate-analysis-button').click()
  // 启动响应已渲染出「排队中」，此时第一次轮询正挂起
  await expect(page.getByTestId('analysis-progress')).toContainText('排队中')

  await page.getByTestId('peer-list').getByText('同业LEAKB').click()
  await expect(page).toHaveURL(/LEAKB/)
  await expect(page.getByTestId('analysis-progress')).toHaveCount(0)

  // 放行 A 的迟到响应：不得画进 B 的页面
  releaseA!()
  await page.waitForTimeout(1500)

  await expect(page).toHaveURL(/LEAKB/)
  await expect(page.getByTestId('analysis-progress')).toHaveCount(0)
  await expect(page.locator('body')).not.toContainText('A标的的分析阶段')
})

// 持仓页批量分析：全部走 route mock，不触发真实 LLM
const BATCH_HOLDINGS = [
  {
    id: 1,
    symbol: 'BAT001',
    name: '批量测试A',
    market: 'A股',
    broker_account_id: null,
    quantity: 100,
    avg_cost: 10,
    total_cost: 1000,
    currency: 'CNY',
    current_price: 12,
    price_updated_at: null
  },
  {
    id: 2,
    symbol: 'BAT002',
    name: '批量测试B',
    market: 'A股',
    broker_account_id: null,
    quantity: 50,
    avg_cost: 20,
    total_cost: 1000,
    currency: 'CNY',
    current_price: 22,
    price_updated_at: null
  },
  {
    id: 3,
    symbol: 'BATBTC',
    name: '不支持市场',
    market: '加密货币',
    broker_account_id: null,
    quantity: 1,
    avg_cost: 100,
    total_cost: 100,
    currency: 'USD',
    current_price: 110,
    price_updated_at: null
  }
]

async function mockHoldingsPage(page: Page, holdings: ApiRow[] = BATCH_HOLDINGS) {
  await page.route('**/api/holdings*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(holdings)
    })
  )
  await page.route('**/api/broker-accounts*', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )
  await page.route('**/api/corporate-actions/security-events*', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )
}

// 后端目标预览：已排除清仓/EXCLUDE/现金管理标的，前端确认框必须用这个数
async function mockBatchTargets(page: Page, total: number) {
  await page.route('**/api/securities/analysis-batch-targets', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        total,
        targets: Array.from({ length: total }, (_, index) => ({
          symbol: `T${index}`,
          market: 'A股'
        }))
      })
    })
  )
}

test('one-click batch analysis asks for confirmation and shows progress', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await mockHoldingsPage(page)
  // [评审回归] 后端排除了现金管理标的：持仓有 2 只 A股，但真实目标只有 1 只
  await mockBatchTargets(page, 1)

  let analysesCall = 0
  await page.route('**/api/securities/analyses', (route) => {
    analysesCall += 1
    // 第一次没有分析，任务推进后返回一条（标签列应随之亮起）
    const body =
      analysesCall <= 1
        ? []
        : [
            {
              symbol: 'BAT001',
              market: 'A股',
              tags: ['业绩增长'],
              risk_level: 'low',
              summary: 'B 摘要'
            }
          ]
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body)
    })
  })
  await page.route('**/api/securities/active-analysis-jobs', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )

  let startCalls = 0
  await page.route('**/api/securities/analysis-batch-jobs', (route) => {
    startCalls += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'batch-1',
        type: 'security_analysis_batch',
        status: 'queued',
        total: 2,
        completed: 0,
        progress_percent: 0
      })
    })
  })

  const script = [
    {
      status: 'running',
      completed: 1,
      progress_percent: 50,
      success_count: 1,
      failed_count: 0,
      skipped_count: 0,
      current_symbol: 'BAT002',
      current_market: 'A股',
      current_stage: '生成分析（LLM）',
      results: [{ symbol: 'BAT001', market: 'A股', status: 'succeeded' }]
    },
    {
      status: 'succeeded',
      completed: 2,
      progress_percent: 100,
      success_count: 2,
      failed_count: 0,
      skipped_count: 0,
      current_symbol: null,
      current_market: null,
      results: [
        { symbol: 'BAT001', market: 'A股', status: 'succeeded' },
        { symbol: 'BAT002', market: 'A股', status: 'succeeded' }
      ]
    }
  ]
  let tick = 0
  await page.route('**/api/securities/analysis-batch-jobs/batch-1', (route) => {
    const payload = script[Math.min(tick, script.length - 1)]
    tick += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ id: 'batch-1', total: 2, ...payload })
    })
  })

  await page.goto('/holdings')
  await expect(page.getByTestId('analyze-all-button')).toBeVisible()

  // 二次确认必须给出数量与耗时量级；取消则不发请求
  await page.getByTestId('analyze-all-button').click()
  const dialog = page.locator('.el-message-box')
  // 数量来自后端预览（1 只），不是前端按市场本地估算的 2 只
  await expect(dialog).toContainText('1 只')
  await expect(dialog).toContainText('预计耗时')
  await expect(dialog).toContainText('自动跳过')
  await dialog.getByRole('button', { name: '取消' }).click()
  expect(startCalls).toBe(0)

  await page.getByTestId('analyze-all-button').click()
  await page.locator('.el-message-box').getByRole('button', { name: '开始分析' }).click()
  // 确认后到 POST 之间隔着预览 resolve 与 axios 往返，用 poll 而不是即刻断言
  await expect.poll(() => startCalls, { timeout: 15000 }).toBe(1)

  const progress = page.getByTestId('batch-analysis-progress')
  await expect(progress).toContainText('1/2', { timeout: 15000 })
  // [评审回归] 启动后按钮只禁用、不转圈（轮询要跑数十分钟）
  await expect(page.getByTestId('analyze-all-button')).toBeDisabled()
  await expect(page.getByTestId('analyze-all-button').locator('.is-loading')).toHaveCount(0)
  await expect(progress).toContainText('BAT002 A股')
  await expect(progress).toContainText('成功 1 · 跳过 0 · 失败 0')
  await expect(page.getByText('批量分析完成：成功 2 只')).toBeVisible({ timeout: 15000 })
  // 完成后标签列刷新
  await expect(page.getByTestId('ai-tags').first()).toBeVisible()
})

test('batch analysis progress is restored when returning to the holdings page', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await mockHoldingsPage(page)
  await mockBatchTargets(page, 5)
  await page.route('**/api/securities/analyses', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )
  await page.route('**/api/securities/active-analysis-jobs', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        {
          id: 'batch-live',
          type: 'security_analysis_batch',
          status: 'running',
          total: 5,
          completed: 2,
          progress_percent: 40,
          success_count: 2,
          failed_count: 0,
          skipped_count: 0,
          current_symbol: 'BAT002',
          current_market: 'A股'
        }
      ])
    })
  )
  await page.route('**/api/securities/analysis-batch-jobs/batch-live', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'batch-live',
        status: 'running',
        total: 5,
        completed: 2,
        progress_percent: 40,
        success_count: 2,
        failed_count: 0,
        skipped_count: 0,
        current_symbol: 'BAT002',
        current_market: 'A股'
      })
    })
  )

  // 不点任何按钮，直接进入页面即应接上进行中的任务
  await page.goto('/holdings')
  const progress = page.getByTestId('batch-analysis-progress')
  await expect(progress).toBeVisible({ timeout: 15000 })
  await expect(progress).toContainText('2/5')
  // 任务活跃时按钮禁用（避免重复发起）
  await expect(page.getByTestId('analyze-all-button')).toBeDisabled()
})

test('digest backfill button previews gaps and shows generated counts', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await mockHoldingsPage(page)
  await mockBatchTargets(page, 2)
  await page.route('**/api/securities/analyses', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )
  await page.route('**/api/securities/active-analysis-jobs', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )
  await page.route('**/api/securities/digest-backfill-preview', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        targets_total: 2,
        targets_without_digest: 1,
        digests_existing: 4,
        per_symbol_budget: 4
      })
    })
  )
  let startCalls = 0
  await page.route('**/api/securities/digest-backfill-jobs', (route) => {
    startCalls += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'digest-1',
        type: 'report_digest_batch',
        status: 'queued',
        total: 2,
        completed: 0,
        progress_percent: 0
      })
    })
  })
  const script = [
    {
      status: 'running',
      completed: 1,
      progress_percent: 50,
      success_count: 1,
      failed_count: 0,
      digests_generated: 4,
      symbols_with_remaining: 1,
      current_symbol: 'BAT002',
      current_market: 'A股'
    },
    {
      status: 'succeeded',
      completed: 2,
      progress_percent: 100,
      success_count: 2,
      failed_count: 0,
      digests_generated: 7,
      symbols_with_remaining: 1,
      current_symbol: null,
      current_market: null
    }
  ]
  let tick = 0
  await page.route('**/api/securities/digest-backfill-jobs/digest-1', (route) => {
    const payload = script[Math.min(tick, script.length - 1)]
    tick += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ id: 'digest-1', total: 2, ...payload })
    })
  })

  await page.goto('/holdings')
  const button = page.getByTestId('digest-backfill-button')
  await expect(button).toBeVisible()

  // 确认框数字来自纯 DB 预览；取消不发请求
  await button.click()
  const dialog = page.locator('.el-message-box')
  await expect(dialog).toContainText('2 只持仓标的')
  await expect(dialog).toContainText('1 只目前一份摘要都没有')
  await expect(dialog).toContainText('最多补 4 份')
  await dialog.getByRole('button', { name: '取消' }).click()
  expect(startCalls).toBe(0)

  await button.click()
  await page.locator('.el-message-box').getByRole('button', { name: '开始回填' }).click()
  await expect.poll(() => startCalls, { timeout: 15000 }).toBe(1)

  const progress = page.getByTestId('digest-backfill-progress')
  await expect(progress).toContainText('1/2', { timeout: 15000 })
  await expect(progress).toContainText('已生成摘要 4 份')
  // 回填活跃时一键分析禁用（互斥，后端 409 兜底、前端体验先行）
  await expect(page.getByTestId('analyze-all-button')).toBeDisabled()
  // 完成提示要说清"还有更早年份可续跑"——这是加深十年的唯一入口
  await expect(page.getByText(/新生成 7 份；1 只标的还有更早年份可补/)).toBeVisible({
    timeout: 15000
  })
})

test('stopping the batch view halts polling but says the job keeps running', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await mockHoldingsPage(page)
  await page.route('**/api/securities/analyses', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )
  await page.route('**/api/securities/active-analysis-jobs', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        {
          id: 'batch-stop',
          type: 'security_analysis_batch',
          status: 'running',
          total: 9,
          completed: 1
        }
      ])
    })
  )
  let polls = 0
  await page.route('**/api/securities/analysis-batch-jobs/batch-stop', (route) => {
    polls += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'batch-stop',
        status: 'running',
        total: 9,
        completed: 1,
        progress_percent: 11
      })
    })
  })

  await page.goto('/holdings')
  const progress = page.getByTestId('batch-analysis-progress')
  await expect(progress).toBeVisible({ timeout: 15000 })
  // 文案必须诚实：这不是"取消"
  await expect(progress).toContainText('后台任务会继续运行')
  await expect(progress).toContainText('继续消耗 token')

  await page.getByTestId('stop-watching-batch').click()
  await expect(progress).toHaveCount(0)
  const pollsAfterStop = polls
  await page.waitForTimeout(6000) // 跨过一个轮询间隔
  expect(polls).toBe(pollsAfterStop)
})

test('the batch button is disabled when no holding is analyzable', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await mockHoldingsPage(page, [BATCH_HOLDINGS[2]]) // 只有加密货币
  await mockBatchTargets(page, 0)
  await page.route('**/api/securities/analyses', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )
  await page.route('**/api/securities/active-analysis-jobs', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )

  await page.goto('/holdings')
  await expect(page.getByTestId('analyze-all-button')).toBeDisabled()
})

test('the confirm dialog waits for the server target preview', async ({ page, request }) => {
  // [评审回归] 预览在途期间不得用本地估算弹确认框：batchTargetCount 初值为 null
  // 时若直接回退本地数，用户在预览慢时点按钮就会看到错误的目标数与 token 预期。
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await mockHoldingsPage(page) // 本地可见 2 只 A股
  await page.route('**/api/securities/analyses', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )
  await page.route('**/api/securities/active-analysis-jobs', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )

  let releasePreview: (() => void) | null = null
  const previewReleased = new Promise<void>((resolve) => {
    releasePreview = resolve
  })
  await page.route('**/api/securities/analysis-batch-targets', async (route) => {
    await previewReleased
    // 服务端真实目标只有 1 只（另一只被现金管理规则排除）
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ total: 1, targets: [{ symbol: 'BAT001', market: 'A股' }] })
    })
  })

  await page.goto('/holdings')
  await expect(page.getByTestId('analyze-all-button')).toBeVisible()

  // 预览仍挂起：点击后不得出现任何确认框（更不能显示本地估算的 2 只）
  await page.getByTestId('analyze-all-button').click()
  await page.waitForTimeout(1000)
  await expect(page.locator('.el-message-box')).toHaveCount(0)

  // 放行预览：确认框出现且用服务端数字
  releasePreview!()
  const dialog = page.locator('.el-message-box')
  await expect(dialog).toBeVisible({ timeout: 15000 })
  await expect(dialog).toContainText('1 只')
  await expect(dialog).not.toContainText('2 只')
})

test('a preview that resolves to zero explains instead of opening the confirm dialog', async ({
  page,
  request
}) => {
  // 预览已 ready 且为 0 时按钮本来就是禁用的；这里覆盖的是另一条路径——
  // 预览**在途时**按钮按本地估算可点，点下去后预览返回 0，必须如实告知而
  // 不是弹一个"将对 2 只标的"的确认框。
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await mockHoldingsPage(page) // 本地看起来有 2 只可分析
  await page.route('**/api/securities/analyses', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )
  await page.route('**/api/securities/active-analysis-jobs', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )

  let releasePreview: (() => void) | null = null
  const previewReleased = new Promise<void>((resolve) => {
    releasePreview = resolve
  })
  await page.route('**/api/securities/analysis-batch-targets', async (route) => {
    await previewReleased
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ total: 0, targets: [] }) // 后端排除后一只不剩
    })
  })

  await page.goto('/holdings')
  await page.getByTestId('analyze-all-button').click()
  releasePreview!()

  await expect(page.getByText('当前没有可分析的持仓标的', { exact: false })).toBeVisible({
    timeout: 15000
  })
  await expect(page.locator('.el-message-box')).toHaveCount(0)
})

// ---------------------------------------------------------------------------
// #142 核心 CRUD 盲区回归：汇率增删改、交易编辑/删除、统计页价格 what-if
// ---------------------------------------------------------------------------

test('exchange rate add, edit and delete through the UI', async ({ page, request }) => {
  // 汇率是全局表：用 GBP + 旧生效日期（2020-01-02），不影响其他用例依赖的
  // USD/HKD/SGD 最新汇率；POST 端点是 upsert，上次运行残留同键行也不会翻车。
  // 「从API更新汇率」按钮走外部接口，E2E 不外呼——数据联动由当前汇率卡片断言。
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await page.goto('/exchange-rates')

  await page.getByRole('button', { name: '手动添加汇率' }).click()
  const addDialog = page.locator('.el-dialog', { hasText: '添加汇率' })
  await expect(addDialog).toBeVisible()

  // 目标币种默认 CNY、来源默认 manual（showAddDialog），只需点开源币种一个
  // 下拉——多个 Element Plus popper 连开会撞上前一个的淡出期，选项定位到
  // 正在关闭的浮层上
  await addDialog.locator('.el-form-item', { hasText: '源币种' }).locator('.el-select').click()
  await page
    .locator('.el-select-dropdown:visible')
    .getByRole('option', { name: '英镑 (GBP)' })
    .click()
  const rateInput = addDialog.locator('.el-form-item', { hasText: '汇率' }).locator('input')
  await rateInput.fill('9.1234')
  await rateInput.blur()
  const dateInput = addDialog.locator('.el-form-item', { hasText: '生效日期' }).locator('input')
  await dateInput.fill('2020-01-02')
  await dateInput.press('Enter')
  await addDialog.getByRole('button', { name: '确定' }).click()

  const rateRow = page
    .locator('.rate-history tr')
    .filter({ hasText: 'GBP' })
    .filter({ hasText: '2020/01/02' })
  await expect(rateRow).toHaveCount(1)
  await expect(rateRow).toContainText('9.1234')
  // 当前汇率卡片联动：GBP 唯一一条即最新，卡片出现
  await expect(page.locator('.current-rates').getByText('GBP', { exact: true })).toBeVisible()

  // 编辑：只改汇率数值
  await rateRow.getByRole('button', { name: '编辑' }).click()
  const editDialog = page.locator('.el-dialog', { hasText: '编辑汇率' })
  await expect(editDialog).toBeVisible()
  const editRateInput = editDialog.locator('.el-form-item', { hasText: '汇率' }).locator('input')
  await editRateInput.fill('8.5')
  await editRateInput.blur()
  await editDialog.getByRole('button', { name: '确定' }).click()
  await expect(rateRow).toContainText('8.5000')

  // 删除：确认框后行消失，当前汇率卡片同步移除 GBP
  // （此处 ElMessageBox 未传 confirmButtonText，未配 locale 的默认按钮是 "OK"）
  await rateRow.getByRole('button', { name: '删除' }).click()
  await page
    .locator('.el-message-box')
    .getByRole('button', { name: /确定|OK/ })
    .click()
  await expect(rateRow).toHaveCount(0)
  await expect(page.locator('.current-rates').getByText('GBP', { exact: true })).toHaveCount(0)
})

test('transaction edit and delete through the dialog', async ({ page, request }) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  try {
    const token = await loginThroughApi(request, {
      username: createdUser.username,
      password
    })
    const buyResponse = await request.post('http://127.0.0.1:18000/api/transactions', {
      headers: { Authorization: `Bearer ${token}` },
      data: {
        symbol: 'EDT001',
        name: '编辑删除测试资产',
        market: 'A股',
        transaction_type: 'BUY',
        quantity: 100,
        price: 10,
        fee: 0,
        transaction_date: '2026-01-05',
        currency: 'CNY',
        notes: 'edit-delete e2e'
      }
    })
    expect(buyResponse.ok()).toBeTruthy()

    await setAuthenticatedSession(page, token, createdUser)
    // 评审 P2：交易写入（PUT/DELETE）后"我的标的"候选缓存必须失效
    let emptySearchCalls = 0
    await page.route('**/api/securities/search*', (route) => {
      if (!new URL(route.request().url()).searchParams.get('q')) emptySearchCalls += 1
      return route.fulfill({
        json: {
          items: [],
          catalog: {
            ready: false,
            stale: true,
            last_success_at: null,
            failing_sources: [],
            capabilities: {}
          }
        }
      })
    })
    await page.route('**/api/securities/resolve*', (route) =>
      route.fulfill({
        json: {
          symbol: 'X',
          market: 'A股',
          name: null,
          currency: null,
          security_type: 'unknown',
          list_status: 'unknown',
          in_catalog: false,
          resolved_from: null,
          error: 'stub'
        }
      })
    )
    const probeEmptySearch = async () => {
      await page.getByRole('button', { name: '新增交易' }).click()
      const probe = page.locator('.el-dialog', { hasText: '新增交易' })
      await probe.locator('.el-form-item', { hasText: '股票代码' }).locator('input').click()
      await page.waitForTimeout(400)
      await probe.getByRole('button', { name: '取消' }).click()
      await expect(probe).toBeHidden()
    }
    await page.goto('/transactions')

    const row = page.locator('tr', { hasText: 'EDT001' })
    await expect(row).toHaveCount(1)
    await probeEmptySearch()
    await probeEmptySearch()
    expect(emptySearchCalls).toBe(1) // 无写入：第二次命中缓存
    await row.getByRole('button', { name: '编辑' }).click()

    const dialog = page.locator('.el-dialog', { hasText: '编辑交易' })
    await expect(dialog).toBeVisible()
    const priceInput = dialog.locator('.el-form-item', { hasText: '价格' }).locator('input')
    await priceInput.fill('12.5')
    await priceInput.blur()
    await dialog.getByRole('button', { name: '确定' }).click()

    await expect(page.getByText('更新成功')).toBeVisible()
    await expect(row).toContainText('12.5000')
    await probeEmptySearch()
    expect(emptySearchCalls).toBe(2) // PUT 后失效

    await row.getByRole('button', { name: '删除' }).click()
    await page.locator('.el-message-box').getByRole('button', { name: '确定' }).click()
    await expect(page.getByText('删除成功')).toBeVisible()
    await expect(row).toHaveCount(0)
    await probeEmptySearch()
    expect(emptySearchCalls).toBe(3) // DELETE 后失效
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

test('statistics price dialog what-if updates the FIFO performance card', async ({
  page,
  request
}) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  try {
    const token = await loginThroughApi(request, {
      username: createdUser.username,
      password
    })
    const buyResponse = await request.post('http://127.0.0.1:18000/api/transactions', {
      headers: { Authorization: `Bearer ${token}` },
      data: {
        symbol: 'PRC001',
        name: '价格弹窗测试资产',
        market: 'A股',
        transaction_type: 'BUY',
        quantity: 100,
        price: 10,
        fee: 0,
        transaction_date: '2026-01-05',
        currency: 'CNY',
        notes: 'price-dialog e2e'
      }
    })
    expect(buyResponse.ok()).toBeTruthy()

    await setAuthenticatedSession(page, token, createdUser)
    await page.goto('/statistics')

    await page.getByRole('button', { name: '输入价格' }).click()
    const dialog = page.locator('.el-dialog', { hasText: '输入当前价格' })
    await expect(dialog).toBeVisible()
    const priceInput = dialog.locator('tr', { hasText: 'PRC001' }).locator('input')
    await priceInput.fill('12')
    await priceInput.blur()
    await dialog.getByRole('button', { name: '计算' }).click()

    await expect(page.getByText('计算完成')).toBeVisible()
    // 100 股 × (12 - 10)：当前市值 ¥1,200.00、浮盈率 +20.00%
    const fifoCard = page.locator('.el-card', { hasText: '当前持仓表现' })
    await expect(fifoCard).toContainText('¥1,200.00')
    await expect(fifoCard).toContainText('20.00')
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

// ---------------------------------------------------------------------------
// 观察清单（新 feature 轮 PR-B）：CRUD + 详情页「加入观察」闭环
// ---------------------------------------------------------------------------

test('watchlist add, edit, remove and detail-page integration', async ({ page, request }) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  try {
    const token = await loginThroughApi(request, {
      username: createdUser.username,
      password
    })
    await setAuthenticatedSession(page, token, createdUser)
    // 标的选择器会打 /securities/search（聚焦）与 /resolve（手输后按需解析→腾讯行情）：
    // 打桩保证 CI 零外呼；选择器本身另有专测
    // 空查询 = "我的标的"候选（账本派生、缓存 5 分钟）：计数用来验证账本写入后重新取数
    let emptySearchCalls = 0
    await page.route('**/api/securities/search*', (route) => {
      if (!new URL(route.request().url()).searchParams.get('q')) emptySearchCalls += 1
      return route.fulfill({
        json: {
          items: [],
          catalog: {
            ready: false,
            stale: true,
            last_success_at: null,
            failing_sources: [],
            capabilities: {}
          }
        }
      })
    })
    // 打开添加对话框并聚焦代码框：触发一次空查询；紧接着取消
    const probeEmptySearch = async () => {
      await page.getByTestId('add-watchlist-button').click()
      const probe = page.locator('.el-dialog', { hasText: '添加观察标的' })
      await probe.locator('.el-form-item', { hasText: '股票代码' }).locator('input').click()
      await page.waitForTimeout(400) // el-autocomplete 聚焦取数有 200ms debounce
      await probe.getByRole('button', { name: '取消' }).click()
      await expect(probe).toBeHidden()
    }
    await page.route('**/api/securities/resolve*', (route) =>
      route.fulfill({
        json: {
          symbol: 'WCH001',
          market: 'A股',
          name: null,
          name_en: null,
          currency: null,
          security_type: 'unknown',
          list_status: 'unknown',
          in_catalog: false,
          resolved_from: null,
          error: 'stub'
        }
      })
    )

    // 1) 观察清单页添加
    await page.goto('/watchlist')
    await page.getByTestId('add-watchlist-button').click()
    const dialog = page.locator('.el-dialog', { hasText: '添加观察标的' })
    await expect(dialog).toBeVisible()
    await dialog.locator('.el-form-item', { hasText: '股票代码' }).locator('input').fill('WCH001')
    await dialog.locator('.el-form-item', { hasText: '市场' }).locator('.el-select').click()
    await page.locator('.el-select-dropdown:visible').getByRole('option', { name: 'A股' }).click()
    await dialog
      .locator('.el-form-item', { hasText: '观察理由' })
      .locator('textarea')
      .fill('等待 PB 回到 1.2 以下')
    await page.getByTestId('watchlist-submit').click()
    await expect(page.getByText('已加入观察清单')).toBeVisible()

    const row = page.locator('tr', { hasText: 'WCH001' })
    await expect(row).toHaveCount(1)
    await expect(row).toContainText('等待 PB 回到 1.2 以下')
    await expect(row).toContainText('未同步档案') // 无档案数据 → 不显示误导性 0/7

    // 重复添加 → 409 中文报错
    await page.getByTestId('add-watchlist-button').click()
    await dialog.locator('.el-form-item', { hasText: '股票代码' }).locator('input').fill('WCH001')
    await dialog.locator('.el-form-item', { hasText: '市场' }).locator('.el-select').click()
    await page.locator('.el-select-dropdown:visible').getByRole('option', { name: 'A股' }).click()
    await page.getByTestId('watchlist-submit').click()
    await expect(page.getByText('该标的已在观察清单中')).toBeVisible()
    await dialog.getByRole('button', { name: '取消' }).click()

    // 2) 编辑理由
    await row.getByRole('button', { name: '编辑' }).click()
    const editDialog = page.locator('.el-dialog', { hasText: '编辑观察标的' })
    await editDialog
      .locator('.el-form-item', { hasText: '观察理由' })
      .locator('textarea')
      .fill('修订后的观察理由')
    await page.getByTestId('watchlist-submit').click()
    await expect(row).toContainText('修订后的观察理由')
    // 评审 P2：编辑自选（PUT）后候选缓存必须失效——下次空查询重新取数；无写入则命中缓存
    const afterAdds = emptySearchCalls
    await probeEmptySearch()
    expect(emptySearchCalls).toBe(afterAdds + 1)
    await probeEmptySearch()
    expect(emptySearchCalls).toBe(afterAdds + 1)

    // 3) 代码链接进详情页 → 显示已在观察（el-link 无 href，不具 link role，按文本点）
    await row.locator('.el-link', { hasText: 'WCH001' }).click()
    await expect(page).toHaveURL(/securities/)
    await expect(page.getByText('已在观察清单', { exact: true })).toBeVisible()

    // 4) 另一标的从详情页加入观察（prompt 填理由）
    await page.goto(`/securities/${encodeURIComponent('A股')}/WCH002`)
    await page.getByTestId('add-to-watchlist-button').click()
    const promptBox = page.locator('.el-message-box', { hasText: '加入观察' })
    await promptBox.locator('textarea').fill('详情页加入的理由')
    await promptBox.getByRole('button', { name: '加入' }).click()
    await expect(page.getByText('已加入观察清单')).toBeVisible()
    await expect(page.getByText('已在观察清单', { exact: true })).toBeVisible()

    // 5) 清单页可见两条；移除一条
    await page.goto('/watchlist')
    await expect(page.locator('tr', { hasText: 'WCH002' })).toContainText('详情页加入的理由')
    await page.locator('tr', { hasText: 'WCH002' }).getByRole('button', { name: '移除' }).click()
    await page.locator('.el-message-box').getByRole('button', { name: '确定' }).click()
    await expect(page.getByText('已移出观察清单')).toBeVisible()
    await expect(page.locator('tr', { hasText: 'WCH002' })).toHaveCount(0)
    await expect(page.locator('tr', { hasText: 'WCH001' })).toHaveCount(1)
    // 评审 P2：移除自选（DELETE）后候选缓存失效——空查询重新取数
    const beforeRemoveProbe = emptySearchCalls
    await probeEmptySearch()
    expect(emptySearchCalls).toBe(beforeRemoveProbe + 1)
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

// ---------------------------------------------------------------------------
// 雪球观点页：e2e 库没有 archiver 外部表——正好验证"数据源未接入"的显式降级
// （状态条 + 空表提示 + 批量按钮禁用），页面不得 5xx/白屏。
// ---------------------------------------------------------------------------

test('opinions page degrades explicitly without archiver source', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  await page.goto('/opinions')
  await expect(page.getByTestId('opinion-source-missing')).toBeVisible()
  await expect(page.getByTestId('opinion-batch-button')).toBeDisabled()
  await expect(page.getByTestId('opinion-symbols-table')).toBeVisible()

  // 详情页的观点 section：生成按钮点击后收到 409 预检（e2e 环境先命中
  // "未配置 LLM"，配了 key 的环境则是"数据源未接入"），以信息条呈现而非报错弹窗
  await page.goto('/securities/A股/600036')
  await expect(page.getByTestId('opinion-section')).toBeVisible()
  await page.getByTestId('generate-opinion-button').click()
  await expect(page.getByTestId('opinion-section').locator('.el-alert')).toContainText(
    /未接入|未配置 LLM/
  )
})

// ---------------------------------------------------------------------------
// 标的可搜索下拉（标的全集）：选中候选完整回填；手输新代码 + 选市场按需解析只填空；
// 用户手改过的名称不被再次覆盖。/search 与 /resolve 打桩——目录内容与排序归后端 pytest
// ---------------------------------------------------------------------------

test('security select fills the transaction form from the catalog and keeps user edits', async ({
  page,
  request
}) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  try {
    const token = await loginThroughApi(request, { username: createdUser.username, password })
    await setAuthenticatedSession(page, token, createdUser)

    const catalog = [
      {
        symbol: '00700',
        market: '港股',
        name: '腾讯控股',
        name_en: 'Tencent Holdings Ltd.',
        name_trad: '騰訊控股',
        pinyin: 'TXKG',
        currency: 'HKD',
        security_type: 'stock',
        board: '主板',
        exchange: 'HKEX',
        list_status: 'listed',
        in_catalog: true,
        origins: [],
        last_used: null
      },
      {
        symbol: '510300',
        market: 'A股',
        name: '沪深300ETF',
        name_en: null,
        name_trad: null,
        pinyin: 'HS300ETF',
        currency: 'CNY',
        security_type: 'etf',
        board: null,
        exchange: 'SSE',
        list_status: 'listed',
        in_catalog: true,
        origins: [],
        last_used: null
      }
    ]
    await page.route('**/api/securities/search*', async (route) => {
      const q = (new URL(route.request().url()).searchParams.get('q') || '').toUpperCase()
      const items = q
        ? catalog.filter((c) => c.symbol.includes(q) || c.pinyin.includes(q) || c.name.includes(q))
        : []
      await route.fulfill({
        json: {
          items,
          catalog: {
            ready: true,
            stale: false,
            last_success_at: null,
            failing_sources: [],
            capabilities: { pinyin: true, simplified: true }
          }
        }
      })
    })
    const resolveCalls: string[] = []
    await page.route('**/api/securities/resolve*', async (route) => {
      const url = new URL(route.request().url())
      const symbol = url.searchParams.get('symbol') || ''
      const market = url.searchParams.get('market') || ''
      resolveCalls.push(`${market}|${symbol}`)
      const hit = symbol === 'D05' && market === '新加坡股'
      await route.fulfill({
        json: {
          symbol,
          market,
          name: hit ? 'DBS Group' : null,
          name_en: null,
          currency: hit ? 'SGD' : null,
          security_type: 'unknown',
          list_status: 'unknown',
          in_catalog: hit,
          resolved_from: hit ? 'tencent-quote' : null,
          error: hit ? null : 'stub'
        }
      })
    })

    await page.goto('/transactions')
    await page.getByRole('button', { name: '新增交易' }).click()
    const dialog = page.locator('.el-dialog', { hasText: '新增交易' })
    await expect(dialog).toBeVisible()
    const formItem = (label: RegExp) =>
      dialog
        .locator('.el-form-item')
        .filter({ has: page.locator('.el-form-item__label', { hasText: label }) })
        .first()
    // data-testid 经 el-autocomplete → el-input 透传落在原生 input 上，按表单项取 textbox 更稳
    const symbolInput = formItem(/^股票代码$/).getByRole('textbox')
    const nameInput = dialog.getByPlaceholder('资产名称')

    // 1) 拼音检索 → 选中候选 → 名称/市场/币种一起回填
    await symbolInput.fill('txkg')
    const popper = page.locator('.security-select-popper:visible')
    await expect(popper.getByRole('option', { name: /00700/ })).toBeVisible()
    await expect(popper).toContainText('腾讯控股')
    await expect(popper).toContainText('港股')
    await popper.getByRole('option', { name: /00700/ }).click()
    await expect(symbolInput).toHaveValue('00700')
    await expect(nameInput).toHaveValue('腾讯控股')
    await expect(formItem(/^市场$/).locator('.el-select')).toContainText('港股')
    await expect(formItem(/^币种$/).locator('.el-select')).toContainText('HKD')

    // 2) 手输新代码：自动带出的名称清空；选定市场后按需解析只填空的名称与推断币种
    await symbolInput.fill('D05')
    await symbolInput.press('Tab')
    await expect(nameInput).toHaveValue('')
    await formItem(/^市场$/)
      .locator('.el-select')
      .click()
    await page
      .locator('.el-select-dropdown:visible')
      .getByRole('option', { name: '新加坡股' })
      .click()
    await expect(nameInput).toHaveValue('DBS Group')
    await expect(formItem(/^币种$/).locator('.el-select')).toContainText('SGD')
    expect(resolveCalls).toContain('新加坡股|D05')

    // 3) 用户手改名称后再换代码：名称保留，不被清空也不被解析结果覆盖
    await nameInput.fill('DBS')
    await symbolInput.fill('D06')
    await symbolInput.press('Tab')
    await expect(symbolInput).toHaveValue('D06')
    await expect(nameInput).toHaveValue('DBS')
    expect(resolveCalls).toContain('新加坡股|D06')

    // 4) ETF 候选带类型标签
    await symbolInput.fill('510300')
    await expect(popper.getByRole('option', { name: /510300/ })).toContainText('ETF')

    // 5) 评审 P1：币种仍是自动推导值时切到推不出的市场必须清空——否则 BTC 会以 CNY 入账
    await symbolInput.fill('BTC')
    await symbolInput.press('Tab')
    await formItem(/^市场$/)
      .locator('.el-select')
      .click()
    await page
      .locator('.el-select-dropdown:visible')
      .getByRole('option', { name: '加密货币' })
      .click()
    await expect(formItem(/^币种$/).locator('.el-select')).not.toContainText(/CNY|HKD|USD|SGD/)
    await dialog.getByRole('button', { name: '确定' }).click()
    await expect(dialog.getByText('请选择币种')).toBeVisible()
    await dialog.getByRole('button', { name: '取消' }).click()
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})
