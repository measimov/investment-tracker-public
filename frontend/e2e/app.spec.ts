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

  await page.getByPlaceholder('请输入用户名').fill(user.username)
  await page.getByPlaceholder('请输入密码').fill(user.password)
  await page.getByRole('button', { name: '登录' }).click()

  await expect(page).toHaveURL(/\/$/)
  await expect(page.getByText('总市值')).toBeVisible()
  await expect(page.getByText('未实现盈亏')).toBeVisible()
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
