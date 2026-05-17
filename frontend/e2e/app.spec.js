import { expect, test } from '@playwright/test'

const user = {
  username: 'demo',
  password: 'e2e-user-password'
}

async function loginThroughApi(request) {
  const response = await request.post('http://127.0.0.1:18000/api/auth/login', {
    data: user
  })
  expect(response.ok()).toBeTruthy()
  const body = await response.json()
  return body.access_token
}

async function setAuthenticatedSession(page, token) {
  await page.addInitScript(
    ({ accessToken }) => {
      window.localStorage.setItem('token', accessToken)
      window.localStorage.setItem(
        'user',
        JSON.stringify({
          id: 2,
          username: 'demo',
          email: null,
          is_active: true,
          is_admin: false
        })
      )
    },
    { accessToken: token }
  )
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

async function importIbkrCsv(request, token, csv, filename = 'ibkr-e2e.csv') {
  return request.post('http://127.0.0.1:18000/api/import/ibkr-activity', {
    headers: {
      Authorization: `Bearer ${token}`
    },
    multipart: {
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
  await expect(page.getByText('总投入')).toBeVisible()
  await expect(page.getByText('持仓数量')).toBeVisible()
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

  const holdingsOverflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)
  expect(holdingsOverflow).toBeFalsy()

  await page.getByRole('button', { name: '打开导航' }).click()
  await expect(page.locator('.mobile-nav-drawer').getByText('交易记录')).toBeVisible()
  await page.locator('.mobile-nav-drawer').getByText('交易记录').click()
  await expect(page).toHaveURL(/\/transactions/)

  await expect(page.locator('.desktop-data-table')).toBeHidden()
  await expect(page.locator('.mobile-transaction-card', { hasText: 'MOB001' })).toBeVisible()

  const transactionsOverflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)
  expect(transactionsOverflow).toBeFalsy()
})

test('imports IBKR relisting activity without corrupting holdings', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  const csv = ibkrCsv([
    'Transaction History,Data,2026-05-04,U***67968,PC PARTNER GROUP LTD,卖,PCT,-1000.0,1.91,SGD,1495.3963,-1.957325,1493.438975',
    'Transaction History,Data,2025-12-09,U***67968,PC PARTNER GROUP LTD,买,1263,2000.0,5.41,HKD,-1390.3700000000001,-2.313,-1394.1361255450001',
    'Transaction History,Data,2025-10-30,U***67968,PC PARTNER GROUP LTD,买,1263,2000.0,6.31,HKD,-1624.1940000000002,-2.3166,-1628.229989529',
    'Transaction History,Data,2025-10-09,U***67968,PC PARTNER GROUP LTD,买,1263,2000.0,7.09,HKD,-1822.2718000000002,-2.31318,-1826.5645647463002',
    'Transaction History,Data,2026-04-14,U***67968,PYPL 17APR26 40 P,买,PYPL  260417P00040000,1.0,0.01,USD,-1.0,-0.56795,-1.56795',
    'Transaction History,Data,2026-03-20,U***67968,卖 -100 INVESCO CURRENCYSHARES EURO (行使),行权,FXE,-100.0,107.0,USD,10700.0,-0.0195,10699.9805'
  ])

  const importResponse = await importIbkrCsv(request, token, csv)
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
