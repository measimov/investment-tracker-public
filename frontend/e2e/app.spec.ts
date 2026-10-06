import { expect, test } from '@playwright/test'
import {
  user,
  authCookieName,
  csrfCookieName,
  type ApiRow,
  loginThroughApi,
  setAuthenticatedSession,
  createTemporaryUser,
  deleteTemporaryUser,
  ibkrCsv,
  createIbkrBrokerAccount,
  ensureSecurityRule,
  importIbkrCsv
} from './helpers'

test('redirects anonymous users to login and supports login', async ({ page }) => {
  await page.goto('/holdings')
  await expect(page).toHaveURL(/\/login/)
  await expect(
    page.locator('.login-card').getByRole('heading', { name: /投资追踪系统/ })
  ).toBeVisible()

  // 守卫带上了 ?redirect=（#142：401/未登录不再丢页面）
  await expect(page).toHaveURL(/redirect=(%2F|\/)holdings/)
  // 只由守卫跳转一次：此前拦截器同时整页跳转，页面刷两次、这条提示一闪即逝（#219）
  await expect(page.locator('.el-message').filter({ hasText: '请先登录' })).toBeVisible()

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

for (const width of [1440, 641, 393]) {
  test(`login success feedback follows dashboard navigation without browser errors at ${width}px`, async ({
    page,
    request
  }) => {
    await page.setViewportSize({ width, height: 1000 })
    const token = await loginThroughApi(request)
    const response = await request.get('http://127.0.0.1:18000/api/statistics/portfolio-snapshot', {
      headers: { Authorization: `Bearer ${token}` }
    })
    expect(response.ok()).toBeTruthy()
    const snapshot = await response.json()
    await page.route('**/api/statistics/portfolio-snapshot', (route) =>
      route.fulfill({ json: snapshot })
    )
    const browserErrors: string[] = []
    page.on('pageerror', (error) => browserErrors.push(error.message))
    await page.addInitScript(() => {
      const successPaths: string[] = []
      Object.assign(window, { loginSuccessPaths: successPaths })
      // Observe the real success message's first appearance, rather than mock its implementation.
      new MutationObserver(() => {
        if (document.querySelector('.el-message--success') && !successPaths.length) {
          successPaths.push(location.pathname)
        }
      }).observe(document, { childList: true, subtree: true })
    })
    await page.goto('/login')
    await page.getByPlaceholder('请输入用户名').fill(user.username)
    await page.getByPlaceholder('请输入密码').fill(user.password)
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await expect(page).toHaveURL(/\/$/)
    await expect(page.getByRole('heading', { name: '仪表盘', exact: true })).toBeVisible()
    const success = page.locator('.el-message--success').filter({ hasText: '登录成功' })
    await expect(success).toBeVisible()
    expect(
      await page.evaluate(
        () => (window as unknown as { loginSuccessPaths: string[] }).loginSuccessPaths
      )
    ).toEqual(['/'])
    await expect(success).toBeHidden()
    expect(browserErrors).toEqual([])
  })
}

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
  await expect(page.getByRole('heading', { name: '交易记录', exact: true })).toBeVisible()
  await expect(page.getByText('E2E001')).toBeVisible()
  await expect(page.getByText('端到端测试资产')).toBeVisible()
  await expect(page.getByTestId('transactions-table').getByText('买入').first()).toBeVisible()

  // 编辑保存回归：后端 schema 是 extra="forbid"，若 payload 混入 id 等
  // 多余字段会 422（曾导致所有编辑保存静默失败，备注永远存不上）
  await page
    .locator('.n-data-table-tbody tr', { hasText: 'E2E001' })
    .getByRole('button', { name: /^编辑/ })
    .click()
  await expect(page.getByText('编辑交易')).toBeVisible()
  await page.getByPlaceholder('备注信息').fill('playwright e2e edited')
  await page.getByRole('dialog').getByRole('button', { name: '确定' }).click()
  await expect(page.getByText('交易记录已更新')).toBeVisible()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.locator('.transactions-page')).toHaveAttribute('aria-busy', 'false')
  await page.getByRole('button', { name: '查看 E2E001 2026/05/13 的交易备注', exact: true }).click()
  await expect(
    page.getByTestId('transactions-table').getByText('playwright e2e edited')
  ).toBeVisible()

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
  // 商业画像/财报摘要/利润质量三区块按 A股 capabilities 渲染（空态不隐藏）；
  // 详情页按「分析 / 基本面 / 报表 / 观点」分 tab（懒渲染），先切 tab 再断言
  await expect(page.getByTestId('business-profile-section')).toBeVisible()
  await expect(page.getByText('暂无商业画像', { exact: false })).toBeVisible()
  await page.getByRole('tab', { name: '报表' }).click()
  await expect(page.getByTestId('report-digest-section')).toBeVisible()
  await expect(page.getByTestId('backfill-digests-button')).toBeVisible()
  await page.getByRole('tab', { name: '基本面' }).click()
  await expect(page.getByTestId('earnings-quality-section')).toBeVisible()
  await page.goBack()

  await page.goto('/statistics')
  await expect(page.getByRole('main').getByText('已实现收益（含股息）：')).toBeVisible()
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
    await expect(
      page.getByRole('main').getByRole('heading', { name: '区间收益', exact: true })
    ).toBeVisible()
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

    // 全局汇率表写入一个辨识度高的 SGD 汇率（幂等 upsert；写汇率仅管理员，#277）
    const rateResponse = await request.post('http://127.0.0.1:18000/api/exchange-rates', {
      headers: { Authorization: `Bearer ${adminToken}` },
      data: {
        from_currency: 'SGD',
        to_currency: 'CNY',
        rate: 5.5,
        effective_date: '2026-01-01'
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
    const rankingCard = page.getByRole('region', { name: '持仓排行 按人民币成本', exact: true })
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
  await expect(page.getByRole('button', { name: '展开导航' })).toBeVisible()
  await expect(page.locator('.desktop-sidebar')).toHaveCSS('width', '56px')
  await expect(page.locator('.desktop-data-table')).toBeHidden()
  // 定位用 data-testid 而非 .mobile-card：后者是全站共用的外观 class，
  // 改样式就会连带改断言对象（PR #99 重命名时正是这里断的）
  await expect(page.getByTestId('holding-card').filter({ hasText: 'MOB001' })).toBeVisible()
  await expect(page.getByTestId('holding-card').filter({ hasText: '移动端测试资产' })).toBeVisible()

  const holdingsOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth
  )
  expect(holdingsOverflow).toBeFalsy()

  await page.getByRole('button', { name: '展开导航' }).click()
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
    ['FBK001', '获得股数: 12'],
    ['FBK002', '拆后股数: 400']
  ]) {
    const row = page.locator('tr', { hasText: symbol }).first()
    await expect(row, `${symbol} 桌面行未渲染`).toContainText(expected)
    await expect(row, `${symbol} 桌面详情出现 literal null`).not.toContainText('null')
  }

  await page.setViewportSize({ width: 393, height: 851 })
  await page.reload()
  await page.waitForLoadState('networkidle')

  for (const [symbol, expected] of [
    ['FBK001', '获得股数: 12'],
    ['FBK002', '拆后股数: 400']
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
  const pctRow = page.locator('[data-testid=holding-row]', { hasText: 'PCT' })
  await expect(pctRow).toBeVisible()
  await expect(pctRow.getByText('柏能集团')).toBeVisible()
  await expect(pctRow.getByText('新加坡股')).toBeVisible()
  await expect(pctRow.locator('.price-line .price-currency')).toHaveText('SGD')
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
    await expect(page.locator('.n-data-table-tbody tr', { hasText: 'TRF001' })).toHaveCount(1)

    await page.goto('/holdings')
    // 持仓默认按标的合并；账户与转仓在「按账户」视图（或合并行的展开行）里
    await page.getByTestId('holdings-view-mode').getByText('按账户').click()
    const row = page.locator('[data-testid=holding-row]', { hasText: 'TRF001' })
    await expect(row).toContainText('转仓测试-CMB')

    // 打开转仓对话框
    await row.getByRole('button', { name: '转仓', exact: true }).click()
    const dialog = page.locator('[data-testid=transfer-dialog]', { hasText: '账户间转仓' })
    await expect(dialog).toBeVisible()

    // 未选择转入账户时不能提交（null 不再兼作默认值）
    await dialog.getByRole('button', { name: '确认转仓' }).click()
    await expect(page.locator('.el-message--warning')).toContainText('请选择转入账户')
    await expect(dialog).toBeVisible()

    // 选择目标账户：转 40 股到第二个账户
    await page.keyboard.press('Escape')
    await expect(dialog).toBeHidden()
    await expect(row.getByRole('button', { name: '转仓', exact: true })).toBeFocused()
    await row.getByRole('button', { name: '转仓', exact: true }).press('Enter')
    await expect(dialog).toBeVisible()
    await dialog.getByTestId('transfer-target').click()
    await page.locator('.n-base-select-option', { hasText: '转仓测试-IBKR' }).click()
    const quantityInput = dialog.getByRole('textbox', { name: '转仓数量' })
    await quantityInput.fill('40')
    await dialog.getByRole('button', { name: '确认转仓' }).click()
    await expect(page.locator('.el-message--success')).toContainText('转仓成功')

    // 两行持仓：CMB 60 / IBKR 40
    const rows = page.locator('[data-testid=holding-row]', { hasText: 'TRF001' })
    await expect(rows).toHaveCount(2)
    await expect(
      page.locator('[data-testid=holding-row]', { hasText: '转仓测试-IBKR' })
    ).toContainText('40')

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
    await expect(page.locator('.n-data-table-tbody tr', { hasText: '转出' })).toHaveCount(1)
    await expect(page.locator('.n-data-table-tbody tr', { hasText: '转入' })).toHaveCount(1)
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

test('transaction symbol links deep-link into holdings with the row highlighted (#235)', async ({
  page,
  request
}) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  const token = await loginThroughApi(request, { username: createdUser.username, password })
  const headers = { Authorization: `Bearer ${token}` }

  async function trade(symbol: string, type: 'BUY' | 'SELL', date: string) {
    const response = await request.post('http://127.0.0.1:18000/api/transactions', {
      headers,
      data: {
        symbol,
        name: `深链${symbol}`,
        market: 'A股',
        transaction_type: type,
        quantity: 100,
        price: 10,
        fee: 0,
        transaction_date: date,
        currency: 'CNY'
      }
    })
    expect(response.ok()).toBeTruthy()
  }

  try {
    // DLK001 / DLK003 在持；DLK002 买入后全部卖出（已清仓）
    await trade('DLK001', 'BUY', '2026-01-05')
    await trade('DLK003', 'BUY', '2026-01-06')
    await trade('DLK002', 'BUY', '2026-01-07')
    await trade('DLK002', 'SELL', '2026-01-08')

    await setAuthenticatedSession(page, token, createdUser)
    await page.goto('/transactions')
    await page.getByTestId('transaction-symbol-link').filter({ hasText: 'DLK001' }).click()

    // 落到持仓页：关键词填入代码、目标行高亮、其他标的被筛掉
    await expect(page).toHaveURL(/\/holdings\?.*symbol=DLK001/)
    await expect(page.getByTestId('holdings-search')).toHaveValue('DLK001')
    const focused = page.locator('[data-testid=holding-row].holding-focus-row')
    await expect(focused).toHaveCount(1)
    await expect(focused).toContainText('DLK001')
    await expect(page.locator('[data-testid=holding-row]', { hasText: 'DLK003' })).toHaveCount(0)
    await expect(page.getByTestId('holdings-filter-count')).toContainText('1 / 2')

    // 清空关键词 = 放弃定位：去高亮、query 从地址栏拿掉、全部持仓回来
    await page.getByTestId('holdings-search').fill('')
    await expect(page).not.toHaveURL(/symbol=/)
    await expect(page.locator('[data-testid=holding-row].holding-focus-row')).toHaveCount(0)
    await expect(page.locator('[data-testid=holding-row]', { hasText: 'DLK003' })).toHaveCount(1)

    // 已清仓的标的：提示未持有并给出标的档案入口
    await page.goto('/transactions')
    await page.getByTestId('transaction-symbol-link').filter({ hasText: 'DLK002' }).first().click()
    const notHeld = page.getByTestId('holdings-not-held')
    await expect(notHeld).toContainText('当前未持有 DLK002')
    await expect(page.getByTestId('holdings-not-held-link')).toHaveAttribute(
      'href',
      /\/securities\/A%E8%82%A1\/DLK002$/
    )
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

test('account view: editing one account row of a multi-account holding mounts a single focused editor (#226 P2)', async ({
  page,
  request
}) => {
  // 按账户视图里同一标的有多行、共用 symbol:market 价格键。编辑态此前也按价格键，
  // 点第一行会让每一行都挂输入框、焦点落到最后一行；再点回第一行时第二个框失焦提交，
  // 两个框一起消失，点选的那行没法编辑
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  const token = await loginThroughApi(request, { username: createdUser.username, password })
  const headers = { Authorization: `Bearer ${token}` }

  try {
    const accounts = await Promise.all(
      ['改价测试-A', '改价测试-B'].map(async (name) => {
        const response = await request.post('http://127.0.0.1:18000/api/broker-accounts', {
          headers,
          data: { broker: name, account_name: name, base_currency: 'CNY' }
        })
        expect(response.ok()).toBeTruthy()
        return response.json()
      })
    )
    for (const account of accounts) {
      const buy = await request.post('http://127.0.0.1:18000/api/transactions', {
        headers,
        data: {
          broker_account_id: account.id,
          symbol: 'EDT001',
          name: '改价标的',
          market: 'A股',
          transaction_type: 'BUY',
          quantity: 100,
          price: 10,
          fee: 0,
          transaction_date: '2026-01-05',
          currency: 'CNY'
        }
      })
      expect(buy.ok()).toBeTruthy()
    }

    await setAuthenticatedSession(page, token, createdUser)
    await page.goto('/holdings')
    await page.getByTestId('holdings-view-mode').getByText('按账户').click()
    const rows = page.locator('[data-testid=holding-row]', { hasText: 'EDT001' })
    await expect(rows).toHaveCount(2)

    const firstRow = rows.filter({ hasText: '改价测试-A' })
    await firstRow.getByTestId('price-display').click()

    // 只挂一个输入框，且焦点就在被点的那一行
    const editors = page.getByTestId('price-input')
    await expect(editors).toHaveCount(1)
    const input = firstRow.getByTestId('price-input').locator('input')
    await expect(input).toBeFocused()

    // 再点一次同一个框不会退出编辑
    await input.click()
    await expect(editors).toHaveCount(1)
    await expect(input).toBeFocused()

    await input.fill('12.3456')
    const priceSaved = page.waitForResponse(
      (response) =>
        response.request().method() === 'PUT' && /\/api\/holdings\/\d+\/price$/.test(response.url())
    )
    await input.press('Enter')
    expect((await priceSaved).ok()).toBeTruthy()
    await expect(editors).toHaveCount(0)
    // 价格按标的共享：两个账户行都显示新价
    await expect(rows.filter({ hasText: '12.3456' })).toHaveCount(2)
    await expect(firstRow.getByTestId('price-display')).toBeFocused()
    const saved = await request.get('http://127.0.0.1:18000/api/holdings', { headers })
    const edited = (await saved.json()).filter((row: ApiRow) => row.symbol === 'EDT001')
    expect(edited.map((row: ApiRow) => Number(row.current_price))).toEqual([12.3456, 12.3456])
    expect(edited.every((row: ApiRow) => row.price_source === 'manual')).toBeTruthy()
    // Esc 取消仍回原按钮，且不能改变共享价格。
    await firstRow.getByTestId('price-display').press('Enter')
    await input.fill('99')
    await input.press('Escape')
    await expect(firstRow.getByTestId('price-display')).toBeFocused()
    await expect(rows.filter({ hasText: '12.3456' })).toHaveCount(2)
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
      await page.getByTestId('holdings-view-mode').getByText('按账户').click()
      const row = page.locator('[data-testid=holding-row]', { hasText: 'TZ0001' })
      await row.getByRole('button', { name: '转仓', exact: true }).click()
      const dialog = page.locator('[data-testid=transfer-dialog]', { hasText: '账户间转仓' })
      await expect(dialog).toBeVisible()
      await expect(dialog.getByRole('textbox', { name: '转仓日期' })).toHaveValue('2026/03/10')
    } finally {
      await deleteTemporaryUser(request, adminToken, createdUser.id)
    }
  })
})
