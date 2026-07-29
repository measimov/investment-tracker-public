import { expect, test } from '@playwright/test'

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

async function loginThroughApi(request, credentials = user) {
  const response = await request.post('http://127.0.0.1:18000/api/auth/token', {
    data: credentials
  })
  expect(response.ok()).toBeTruthy()
  const body = await response.json()
  return body.access_token
}

async function setAuthenticatedSession(page, token, userInfo = null) {
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

async function createTemporaryUser(request) {
  const adminToken = await loginThroughApi(request, adminUser)
  const username = `analytics_e2e_${Date.now()}`
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

async function deleteTemporaryUser(request, adminToken, userId) {
  const response = await request.delete(`http://127.0.0.1:18000/api/users/${userId}`, {
    headers: {
      Authorization: `Bearer ${adminToken}`
    }
  })
  expect(response.ok()).toBeTruthy()
}

function ibkrCsv(rows) {
  return [
    'Statement,Header,域名称,域值',
    'Statement,Data,Title,Transaction History',
    '总结,Header,域名称,域值',
    '总结,Data,基础货币,USD',
    'Transaction History,Header,日期,账户,说明,交易类型,代码,数量,价格,Price Currency,总额,佣金,净额',
    ...rows
  ].join('\n')
}

async function createIbkrBrokerAccount(request, token) {
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

async function importIbkrCsv(request, token, brokerAccountId, csv, filename = 'ibkr-e2e.csv') {
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
  await expect(page.getByText('当前收益统计仍是估算口径')).toBeVisible()
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
    headers: { 'X-CSRF-Token': csrfCookie.value },
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

  await page.goto('/holdings')
  await expect(page.getByRole('main').getByText('当前持仓')).toBeVisible()
  await expect(page.getByText('E2E001')).toBeVisible()
  await expect(page.getByText('端到端测试资产')).toBeVisible()
  await expect(page.locator('body')).not.toContainText('NaN')

  await page.goto('/statistics')
  await expect(page.getByRole('main').getByText('含股息已实现收益：')).toBeVisible()
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
    const holding = holdings.find((row) => row.symbol === 'ANA001')
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
  await expect(page.locator('.mobile-holding-card', { hasText: 'MOB001' })).toBeVisible()
  await expect(page.locator('.mobile-holding-card', { hasText: '移动端测试资产' })).toBeVisible()

  const holdingsOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth
  )
  expect(holdingsOverflow).toBeFalsy()

  await page.getByRole('button', { name: '打开导航' }).click()
  await expect(page.locator('.mobile-nav-drawer').getByText('交易记录')).toBeVisible()
  await page.locator('.mobile-nav-drawer').getByText('交易记录').click()
  await expect(page).toHaveURL(/\/transactions/)

  await expect(page.locator('.desktop-data-table')).toBeHidden()
  await expect(page.locator('.mobile-transaction-card', { hasText: 'MOB001' })).toBeVisible()

  const transactionsOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth
  )
  expect(transactionsOverflow).toBeFalsy()
})

test('imports IBKR relisting activity without corrupting holdings', async ({ page, request }) => {
  const token = await loginThroughApi(request)
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
  const pct = holdings.find((row) => row.symbol === 'PCT' && row.market === '新加坡股')
  const oldHk = holdings.find((row) => row.symbol === '01263' && row.market === '港股')

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
    const transferRows = holdings.filter((h) => h.symbol === 'TRF001')
    expect(transferRows).toHaveLength(2)
    const byAccount = Object.fromEntries(transferRows.map((h) => [h.broker_account_id, h]))
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

    const mismatchedRow = page.locator('.el-table__row', { hasText: '2026/2/28' })
    await mismatchedRow.getByRole('button', { name: '重新比对' }).click()
    await expect(page.locator('.el-message--success')).toContainText('比对一致')
    // 关闭的 dialog 仍留在 DOM（保留旧 row 引用），只断言可见标签
    await expect(page.locator('.el-tag:visible', { hasText: '有差异' })).toHaveCount(0)
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
      (row) => row.symbol === 'STDACC1'
    )
    expect(holding).toBeTruthy()
    expect(holding.broker_account_id).toBe(account.id)
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})
