/**
 * UI 视觉审计截图脚本（非回归测试）：UI_AUDIT=1 时运行。
 *
 * 种子代表性数据后，对全部路由在 1440/900/393 三档宽度全页截图，
 * 并打开主要对话框截图，输出到 UI_AUDIT_DIR（默认 scratchpad/ui-audit）。
 */

import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const AUDIT = !!process.env.UI_AUDIT
const OUT = process.env.UI_AUDIT_DIR || '/tmp/ui-audit'

test.skip(!AUDIT, '仅在 UI_AUDIT=1 时运行')

// 截图采集本质是顺序流程：串行执行，杜绝两条用例同时探空、双重灌种的竞态
test.describe.configure({ mode: 'serial' })

// 审计使用专属用户：与其他测试的 demo 数据完全隔离；自愈=删用户级联清场
const AUDIT_USERNAME = 'ui_audit'
const AUDIT_PASSWORD = 'ui-audit-password'
// 种子完成哨兵：user 级 security_rules 行（唯一键保护），随用户级联删除——
// 全局表（如汇率）不能作哨兵：删用户后仍在，会误判种子完整
const SENTINEL_RULE = {
  rule_type: 'NAME_OVERRIDE',
  symbol: 'AUDITOK',
  market: '美股',
  payload: { name: '审计种子完成哨兵' }
}
const admin = { username: 'admin', password: 'e2e-admin-password' }
const authCookieName = 'investment_session'
const csrfCookieName = 'investment_csrf'

async function login(
  request: APIRequestContext,
  credentials: { username: string; password: string }
) {
  const response = await request.post('http://127.0.0.1:18000/api/auth/token', {
    data: credentials
  })
  expect(response.ok()).toBeTruthy()
  return (await response.json()).access_token as string
}

async function setSession(page: Page, token: string, info: Record<string, unknown>) {
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
      value: 'audit-csrf',
      url: 'http://127.0.0.1:18000',
      httpOnly: false,
      secure: false,
      sameSite: 'Strict'
    }
  ])
  await page.addInitScript((currentUser) => {
    window.localStorage.setItem('user', JSON.stringify(currentUser))
  }, info)
}

/**
 * 幂等入口（末位哨兵 + 用户级联自愈）：
 * - 哨兵（audit 用户名下的 security_rule）在 → 种子确定完整，直接复用；
 * - 哨兵不在（从未种过 / 上次半途而废）→ 删除 audit 用户（ON DELETE CASCADE
 *   连带清掉其账户/交易/现金事件/快照/规则）后重建并全量重种。
 *   账户不会累积，也绝不触碰其他用户的数据。
 * 返回可用的 audit 用户 token。
 */
async function ensureSeeded(request: APIRequestContext): Promise<string> {
  const adminToken = await login(request, admin)
  const adminHeaders = { Authorization: `Bearer ${adminToken}` }

  const usersResp = await request.get('http://127.0.0.1:18000/api/users?limit=200', {
    headers: adminHeaders
  })
  const users = usersResp.ok()
    ? ((await usersResp.json()) as Array<{ id: number; username: string }>)
    : []
  const existing = users.find((row) => row.username === AUDIT_USERNAME)

  if (existing) {
    const token = await login(request, { username: AUDIT_USERNAME, password: AUDIT_PASSWORD })
    const probe = await request.get(
      'http://127.0.0.1:18000/api/security-rules?rule_type=NAME_OVERRIDE',
      { headers: { Authorization: `Bearer ${token}` } }
    )
    if (probe.ok()) {
      const rules = (await probe.json()) as Array<{ symbol: string }>
      if (rules.some((rule) => rule.symbol === SENTINEL_RULE.symbol)) return token
    }
    // 半成品：级联删除整个 audit 用户，下面重建
    const deleted = await request.delete(`http://127.0.0.1:18000/api/users/${existing.id}`, {
      headers: adminHeaders
    })
    expect(deleted.ok()).toBeTruthy()
  }

  const created = await request.post('http://127.0.0.1:18000/api/users', {
    headers: adminHeaders,
    data: { username: AUDIT_USERNAME, password: AUDIT_PASSWORD, is_active: true, is_admin: false }
  })
  expect(created.ok()).toBeTruthy()
  const token = await login(request, { username: AUDIT_USERNAME, password: AUDIT_PASSWORD })
  await seed(request, token)
  return token
}

async function seed(request: APIRequestContext, token: string) {
  const headers = { Authorization: `Bearer ${token}` }
  const post = async (path: string, data: Record<string, unknown>) => {
    const response = await request.post(`http://127.0.0.1:18000${path}`, { headers, data })
    // 幂等重跑：唯一键冲突可容忍
    expect([200, 201, 409]).toContain(response.status())
    return response
  }

  for (const [from, rate] of [
    ['USD', 7.2],
    ['HKD', 0.92],
    ['SGD', 5.3]
  ] as const) {
    await post('/api/exchange-rates', {
      from_currency: from,
      to_currency: 'CNY',
      rate,
      effective_date: '2026-01-01'
    })
  }

  const txns: Array<Record<string, unknown>> = [
    // 多市场持仓 + 长名称 + 盈利/亏损并存 + 大数字
    {
      symbol: '600519',
      name: '贵州茅台',
      market: 'A股',
      transaction_type: 'BUY',
      quantity: 1200,
      price: 1580.55,
      fee: 120,
      transaction_date: '2025-03-05',
      currency: 'CNY'
    },
    {
      symbol: '000651',
      name: '格力电器超长名称测试格力电器',
      market: 'A股',
      transaction_type: 'BUY',
      quantity: 8800,
      price: 38.2,
      fee: 25,
      transaction_date: '2025-05-11',
      currency: 'CNY'
    },
    {
      symbol: '00700',
      name: '腾讯控股',
      market: '港股',
      transaction_type: 'BUY',
      quantity: 500,
      price: 388.6,
      fee: 90,
      transaction_date: '2025-06-20',
      currency: 'HKD'
    },
    {
      symbol: '00700',
      name: '腾讯控股',
      market: '港股',
      transaction_type: 'SELL',
      quantity: 200,
      price: 425.2,
      fee: 80,
      transaction_date: '2026-01-15',
      currency: 'HKD'
    },
    {
      symbol: 'PDD',
      name: '拼多多',
      market: '美股',
      transaction_type: 'BUY',
      quantity: 300,
      price: 145.3,
      fee: 3,
      transaction_date: '2025-09-02',
      currency: 'USD'
    },
    {
      symbol: 'PCT',
      name: '柏能集团',
      market: '新加坡股',
      transaction_type: 'BUY',
      quantity: 6000,
      price: 1.05,
      fee: 8,
      transaction_date: '2025-10-30',
      currency: 'SGD'
    },
    {
      symbol: 'PCT',
      name: '柏能集团',
      market: '新加坡股',
      transaction_type: 'SELL',
      quantity: 6000,
      price: 0.92,
      fee: 8,
      transaction_date: '2026-04-02',
      currency: 'SGD'
    },
    {
      symbol: '159307',
      name: '增强红利ETF',
      market: 'A股',
      transaction_type: 'BUY',
      quantity: 57800,
      price: 0.956,
      fee: 5,
      transaction_date: '2026-06-26',
      currency: 'CNY'
    }
  ]
  for (const txn of txns) await post('/api/transactions', txn)

  await post('/api/corporate-actions/cash-dividend', {
    symbol: '600519',
    name: '贵州茅台',
    market: 'A股',
    ex_date: '2026-06-30',
    dividend_per_share: 30.876,
    total_dividend: 37051.2,
    tax_rate: 0.1,
    currency: 'CNY'
  })

  const accountResp = await post('/api/broker-accounts', {
    broker: '招商证券',
    account_name: '审计基线账户',
    account_number_masked: '****1234',
    base_currency: 'CNY'
  })
  let accountId: number | null = null
  if (accountResp.status() === 201) accountId = (await accountResp.json()).id
  else {
    const list = await request.get('http://127.0.0.1:18000/api/broker-accounts', { headers })
    accountId = (await list.json())[0]?.id ?? null
  }
  if (accountId) {
    await post('/api/cash-events', {
      broker_account_id: accountId,
      event_type: 'DEPOSIT',
      amount: 500000,
      currency: 'CNY',
      event_date: '2025-01-02',
      notes: '审计基线入金'
    })
    await post('/api/reconciliation-snapshots', {
      broker_account_id: accountId,
      snapshot_date: '2026-06-30',
      cash_balances: { CNY: 1308.02 },
      positions: [{ symbol: '600519', market: 'A股', quantity: 1200, currency: 'CNY' }]
    })
  }

  await post('/api/security-rules', {
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
  await post('/api/security-rules', {
    rule_type: 'EXCLUDE',
    symbol: '511880',
    market: 'A股',
    note: '货币基金'
  })

  // 末位哨兵：user 级规则行，seed 完整跑完才存在
  await post('/api/security-rules', SENTINEL_RULE)
}

const WIDTHS: Array<[string, number, number]> = [
  ['w1440', 1440, 900],
  ['w900', 900, 900],
  ['w393', 393, 851]
]

const ROUTES: Array<[string, string]> = [
  ['dashboard', '/'],
  ['account-data', '/account-data'],
  ['transactions', '/transactions'],
  ['corporate-actions', '/corporate-actions'],
  ['holdings', '/holdings'],
  ['statistics', '/statistics'],
  ['reports', '/reports'],
  ['exchange-rates', '/exchange-rates']
]

test('seeding is deterministic across reruns and half-seeded states', async ({ request }) => {
  test.setTimeout(120_000)
  const token = await ensureSeeded(request)
  const headers = { Authorization: `Bearer ${token}` }
  const count = async (path: string) => {
    const response = await request.get(`http://127.0.0.1:18000${path}`, { headers })
    return ((await response.json()) as unknown[]).length
  }

  const accounts = await count('/api/broker-accounts?limit=100')
  const txns = await count('/api/transactions?symbol=600519&limit=100')

  // 完整状态重跑：全部计数不变
  await ensureSeeded(request)
  expect(await count('/api/broker-accounts?limit=100')).toBe(accounts)
  expect(await count('/api/transactions?symbol=600519&limit=100')).toBe(txns)

  // 半成品模拟（检视场景：账户已建、哨兵未写）：删哨兵后重跑，
  // 级联自愈重建，计数仍不增长
  const rules = (await (
    await request.get('http://127.0.0.1:18000/api/security-rules?rule_type=NAME_OVERRIDE', {
      headers
    })
  ).json()) as Array<{ id: number; symbol: string }>
  const sentinel = rules.find((rule) => rule.symbol === SENTINEL_RULE.symbol)
  expect(sentinel).toBeTruthy()
  await request.delete(`http://127.0.0.1:18000/api/security-rules/${sentinel!.id}`, { headers })

  const healedToken = await ensureSeeded(request)
  const healedHeaders = { Authorization: `Bearer ${healedToken}` }
  const healedCount = async (path: string) => {
    const response = await request.get(`http://127.0.0.1:18000${path}`, {
      headers: healedHeaders
    })
    return ((await response.json()) as unknown[]).length
  }
  expect(await healedCount('/api/broker-accounts?limit=100')).toBe(accounts)
  expect(await healedCount('/api/transactions?symbol=600519&limit=100')).toBe(txns)
})

test('capture route screenshots', async ({ page, request }) => {
  test.setTimeout(300_000)
  const token = await ensureSeeded(request)
  await setSession(page, token, { username: AUDIT_USERNAME, is_active: true, is_admin: false })

  for (const [wname, width, height] of WIDTHS) {
    await page.setViewportSize({ width, height })
    for (const [name, route] of ROUTES) {
      await page.goto(route)
      await page.waitForLoadState('networkidle')
      await page.waitForTimeout(600)
      await page.screenshot({ path: `${OUT}/${name}-${wname}.png`, fullPage: true })
    }
  }
})

test('capture admin screenshots', async ({ page, request }) => {
  test.setTimeout(120_000)
  const token = await login(request, admin)
  await setSession(page, token, { id: 1, username: 'admin', is_active: true, is_admin: true })
  for (const [wname, width, height] of [WIDTHS[0], WIDTHS[2]]) {
    await page.setViewportSize({ width, height })
    for (const [name, route] of [
      ['admin-users', '/admin/users'],
      ['admin-holdings', '/admin/holdings']
    ] as Array<[string, string]>) {
      await page.goto(route)
      await page.waitForLoadState('networkidle')
      await page.waitForTimeout(500)
      await page.screenshot({ path: `${OUT}/${name}-${wname}.png`, fullPage: true })
    }
  }
})

test('capture dialog screenshots', async ({ page, request }) => {
  test.setTimeout(300_000)
  const token = await ensureSeeded(request)
  await setSession(page, token, { username: AUDIT_USERNAME, is_active: true, is_admin: false })

  const dialogs: Array<{ name: string; route: string; open: (p: Page) => Promise<void> }> = [
    {
      name: 'dlg-transaction-create',
      route: '/transactions',
      open: async (p) => p.getByRole('button', { name: '新增交易' }).click()
    },
    {
      name: 'dlg-transaction-import',
      route: '/transactions',
      open: async (p) => p.getByRole('button', { name: '导入' }).first().click()
    },
    {
      name: 'dlg-corporate-action',
      route: '/corporate-actions',
      open: async (p) => p.getByRole('button', { name: /新增/ }).first().click()
    },
    {
      name: 'dlg-price-input',
      route: '/statistics',
      open: async (p) =>
        p
          .getByRole('button', { name: /输入价格|手动价格/ })
          .first()
          .click()
    },
    {
      name: 'dlg-exchange-rate',
      route: '/exchange-rates',
      open: async (p) =>
        p
          .getByRole('button', { name: /手动添加|添加汇率/ })
          .first()
          .click()
    },
    {
      name: 'dlg-account-create',
      route: '/account-data',
      open: async (p) =>
        p
          .getByRole('button', { name: /新增账户/ })
          .first()
          .click()
    },
    {
      name: 'dlg-cash-event',
      route: '/account-data',
      open: async (p) => {
        await p.getByRole('tab', { name: '现金事件' }).click()
        await p
          .getByRole('button', { name: /新增现金事件|新增/ })
          .first()
          .click()
      }
    },
    {
      name: 'dlg-snapshot',
      route: '/account-data',
      open: async (p) => {
        await p.getByRole('tab', { name: /月末核对|对账/ }).click()
        await p.getByRole('button', { name: /新增/ }).first().click()
      }
    },
    {
      name: 'dlg-security-rule',
      route: '/account-data',
      open: async (p) => {
        await p.getByRole('tab', { name: '特例规则' }).click()
        await p.getByRole('button', { name: '新增规则' }).click()
      }
    }
  ]

  const failures: string[] = []
  for (const [wname, width, height] of [WIDTHS[0], WIDTHS[2]]) {
    await page.setViewportSize({ width, height })
    for (const dialog of dialogs) {
      await page.goto(dialog.route)
      await page.waitForLoadState('networkidle')
      try {
        await dialog.open(page)
        await page.waitForTimeout(400)
        await page.screenshot({ path: `${OUT}/${dialog.name}-${wname}.png`, fullPage: false })
        await page.keyboard.press('Escape')
      } catch (error) {
        // 先拍完其余对话框，最后统一失败——漏图的审计不许显示成功
        failures.push(`${dialog.name}@${wname}: ${String(error).split('\n')[0]}`)
      }
    }
  }
  expect(failures, `以下对话框截图失败:\n${failures.join('\n')}`).toEqual([])
})
