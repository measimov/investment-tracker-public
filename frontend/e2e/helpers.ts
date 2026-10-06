// E2E 公共件（#285）：登录、会话注入、临时用户与 IBKR 导入样板。此前 app.spec.ts 一个文件
// 3400 多行，其余 spec 各自再抄一份登录；拆分后按主题分文件，共用的东西只在这里定义。
import { expect, type APIRequestContext, type Page } from '@playwright/test'

export const user = {
  username: 'demo',
  password: 'e2e-user-password'
}

export const adminUser = {
  username: 'admin',
  password: 'e2e-admin-password'
}
export const authCookieName = 'investment_session'
export const csrfCookieName = 'investment_csrf'

// API 响应行的宽松类型：E2E 断言只关心少数字段，其余按原样透传
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type ApiRow = Record<string, any>

/** 每个虚构来源场景显式说明本地配置/历史；不全局默认可见。 */
export async function mockXueqiuCapabilities(
  page: Page,
  facts: { configured?: boolean; opinionHistory?: boolean; postHistory?: boolean }
) {
  const entry = (history: boolean | undefined) =>
    facts.configured
      ? { available: true, reason: 'configured' }
      : history
        ? { available: true, reason: 'history' }
        : { available: false, reason: 'unconfigured' }
  await page.route('**/api/capabilities', (route) =>
    route.fulfill({
      json: {
        opinions: entry(facts.opinionHistory),
        xueqiu_symbol_feed: entry(facts.postHistory)
      }
    })
  )
}

export async function loginThroughApi(
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

export async function setAuthenticatedSession(
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

export async function createTemporaryUser(request: APIRequestContext) {
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

export async function deleteTemporaryUser(
  request: APIRequestContext,
  adminToken: string,
  userId: number
) {
  const response = await request.delete(`http://127.0.0.1:18000/api/users/${userId}`, {
    headers: {
      Authorization: `Bearer ${adminToken}`
    }
  })
  expect(response.ok()).toBeTruthy()
}

export function ibkrCsv(rows: string[]) {
  return [
    'Statement,Header,域名称,域值',
    'Statement,Data,Title,Transaction History',
    '总结,Header,域名称,域值',
    '总结,Data,基础货币,USD',
    'Transaction History,Header,日期,账户,说明,交易类型,代码,数量,价格,Price Currency,总额,佣金,净额',
    ...rows
  ].join('\n')
}

export async function createIbkrBrokerAccount(request: APIRequestContext, token: string) {
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
export async function ensureSecurityRule(request: APIRequestContext, token: string, data: ApiRow) {
  const response = await request.post('http://127.0.0.1:18000/api/security-rules', {
    headers: { Authorization: `Bearer ${token}` },
    data
  })
  expect([201, 409]).toContain(response.status())
}

export async function importIbkrCsv(
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

// 持仓页桩数据：批量分析与公告徽标两组用例共用
export const BATCH_HOLDINGS = [
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

export async function mockHoldingsPage(page: Page, holdings: ApiRow[] = BATCH_HOLDINGS) {
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

/**
 * 负向断言前的收尾：等一个已放行的迟到响应真正到达浏览器，再让页面跑完两帧。
 * 取代「放行后 waitForTimeout(N)」——固定时长在慢机器上不够、在快机器上白等。
 */
export async function settleAfter(page: Page, response: Promise<unknown>) {
  await response
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
      )
  )
}
