import { expect, test } from '@playwright/test'
import {
  type ApiRow,
  loginThroughApi,
  setAuthenticatedSession,
  createTemporaryUser,
  deleteTemporaryUser,
  settleAfter
} from './helpers'

test('dated dividend tax can be explicitly allocated without changing the cash fact', async ({
  page,
  request
}) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  const token = await loginThroughApi(request, { username: createdUser.username, password })
  const headers = { Authorization: `Bearer ${token}` }
  try {
    const accountResponse = await request.post('http://127.0.0.1:18000/api/broker-accounts', {
      headers,
      data: { broker: '股息税测试', account_name: '股息税测试', base_currency: 'CNY' }
    })
    expect(accountResponse.ok()).toBeTruthy()
    const account = await accountResponse.json()
    const dividend = await request.post('http://127.0.0.1:18000/api/corporate-actions', {
      headers,
      data: {
        broker_account_id: account.id,
        symbol: 'DTX001',
        market: 'A股',
        action_type: 'CASH_DIVIDEND',
        receipt_confirmed: true,
        ex_date: '2026-01-01',
        payment_date: '2026-01-05',
        total_dividend: 100,
        tax_withheld: 0,
        currency: 'CNY'
      }
    })
    expect(dividend.ok()).toBeTruthy()
    const taxResponse = await request.post('http://127.0.0.1:18000/api/cash-events', {
      headers,
      data: {
        broker_account_id: account.id,
        event_type: 'TAX',
        tax_kind: 'DIVIDEND',
        event_date: '2026-02-01',
        amount: 10,
        currency: 'CNY'
      }
    })
    expect(taxResponse.ok()).toBeTruthy()
    const tax = await taxResponse.json()
    await setAuthenticatedSession(page, token, createdUser)
    await page.goto('/account-data')
    await page.getByRole('tab', { name: '现金事件' }).click()
    await expect(page.getByText('待归属 ¥10.00', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: / 股息税归属$/ }).click()
    const dialog = page.getByRole('dialog', { name: '股息税归属' })
    await expect(dialog).toContainText('不改变扣款日期和总额')
    await dialog.getByRole('button', { name: '添加分摊' }).click()
    await dialog.locator('.el-select').click()
    await page.getByRole('option', { name: /DTX001/ }).click()
    await dialog.getByRole('spinbutton').fill('7')
    await dialog.getByRole('button', { name: '保存归属' }).click()
    await expect(dialog).toBeHidden()
    await expect(page.getByText('待归属 ¥3.00', { exact: true })).toBeVisible()
    const saved = await request.get(`http://127.0.0.1:18000/api/cash-events/${tax.id}`, { headers })
    expect((await saved.json()).event_date).toBe('2026-02-01')
    expect(Number((await saved.json()).amount)).toBe(10)
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
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
    await expect(page.locator('.n-tag', { hasText: '比对一致' })).toBeVisible()
    const dangerTag = page.getByRole('button', { name: '有差异，查看差异明细' })
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

    const mismatchedRow = page.locator('.n-data-table-tbody tr', { hasText: '2026/02/28' })
    await mismatchedRow.getByRole('button', { name: /^重新比对 / }).click()
    await expect(page.locator('.el-message--success')).toContainText('比对一致')
    // 关闭的 dialog 仍留在 DOM（保留旧 row 引用），只断言可见标签
    await expect(page.locator('.n-tag:visible', { hasText: '有差异' })).toHaveCount(0)

    // issue #58：分范围快照的重新比对 toast 须显示"持仓一致"而非"比对一致"。
    // scoped 快照只能由对账单导入器创建，这里拦截 compare 响应注入 statement_scope
    // 来验证前端 toast 复用 snapshotStatusLabel 的分支。
    await page.route('**/api/reconciliation-snapshots/*/compare', async (route) => {
      const response = await route.fetch()
      const body = await response.json()
      await route.fulfill({ response, json: { ...body, statement_scope: 'stock' } })
    })
    const matchedRow = page.locator('.n-data-table-tbody tr', { hasText: '2026/01/31' })
    await matchedRow.getByRole('button', { name: /^重新比对 / }).click()
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
      action_type: 'STOCK_DIVIDEND',
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
    await page.getByRole('tab', { name: /预计与待收股息/ }).click()
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
  // 先等分红建议 tab 自己的列表请求落地再计数：并行跑满负载时它可能晚于
  // 同步按钮的首轮轮询到达，被算成「离开后的请求」（假阳性）
  const listLoaded = page.waitForResponse((response) =>
    /\/api\/corporate-actions\/suggestions\?/.test(response.url())
  )
  await page.getByRole('tab', { name: /预计与待收股息/ }).click()
  await listLoaded
  const firstPoll = page.waitForRequest('**/api/corporate-actions/dividend-sync-jobs/sync-race')
  await page.getByTestId('dividend-sync-button').click()
  await firstPoll // 轮询响应此刻挂在闸上

  const requestsBeforeLeave = suggestionRequests
  // 客户端路由切走（SPA 存活、CorporateActions 卸载）——整页 goto 会销毁
  // JS 上下文，复现不了这个竞态
  await page
    .getByRole('navigation', { name: '主导航' })
    .getByRole('link', { name: '交易记录', exact: true })
    .click()
  await expect(page).toHaveURL(/\/transactions/)

  const latePoll = page.waitForResponse((r) => r.url().includes('/dividend-sync-jobs/sync-race'))
  releasePoll() // 放行挂起的响应 → 轮询以取消（null）收场
  await settleAfter(page, latePoll)

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
    const ruleRow = page.locator('.n-data-table-tbody tr', { hasText: '01263' })
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
    await page
      .locator('.el-select-dropdown:visible')
      .getByRole('option', { name: optionName, exact: true })
      .click()
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
    await expect(page.locator('.n-data-table-tbody tr', { hasText: '01263' })).toContainText(
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
    await expect(page.locator('.n-data-table-tbody tr', { hasText: '银行转存' })).toContainText(
      '→ 入金'
    )

    // ---- 竞态回归：慢的未过滤初始加载不得覆盖随后切换的筛选结果 ----
    await page.route('**/api/security-rules*', async (route) => {
      if (!route.request().url().includes('rule_type=')) {
        await new Promise((resolve) => setTimeout(resolve, 4000))
      }
      await route.continue()
    })
    const slowUnfiltered = page.waitForResponse(
      (r) => r.url().includes('/api/security-rules') && !r.url().includes('rule_type=')
    )
    await page.reload()
    await page.getByRole('tab', { name: '特例规则' }).click()
    // 未过滤请求（含两条规则）被延迟 4s，在途时切筛选
    await page.getByRole('combobox', { name: '筛选规则类型' }).selectOption('CMB_CASH_BUSINESS')
    await expect(page.locator('.n-data-table-tbody tr', { hasText: '银行转存' })).toBeVisible()
    // 慢的未过滤响应落地后，不得把 01263（RELISTING）行覆盖回表格
    await settleAfter(page, slowUnfiltered)
    await expect(page.locator('.n-data-table-tbody tr', { hasText: '银行转存' })).toBeVisible()
    await expect(page.locator('.n-data-table-tbody tr', { hasText: '01263' })).toHaveCount(0)
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

    await expect(page.locator('.n-data-table-tbody tr', { hasText: 'STDACC1' })).toHaveCount(1)

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

test('confirmed net-only dividend counts as received and keeps unknown tax visible', async ({
  page,
  request
}) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  const token = await loginThroughApi(request, { username: createdUser.username, password })
  const headers = { Authorization: `Bearer ${token}` }
  try {
    const accountResponse = await request.post('http://127.0.0.1:18000/api/broker-accounts', {
      headers,
      data: { broker: '到账测试', account_name: '到账测试', base_currency: 'CNY' }
    })
    const account = await accountResponse.json()
    const payload = {
      broker_account_id: account.id,
      symbol: 'DIV001',
      market: 'A股',
      action_type: 'CASH_DIVIDEND',
      ex_date: '2026-01-01',
      payment_date: '2026-01-05',
      amount_basis: 'NET_ONLY',
      net_dividend: 90,
      currency: 'CNY'
    }
    const unconfirmed = await request.post('http://127.0.0.1:18000/api/corporate-actions', {
      headers,
      data: payload
    })
    expect(unconfirmed.status()).toBe(422)
    const received = await request.post('http://127.0.0.1:18000/api/corporate-actions', {
      headers,
      data: { ...payload, receipt_confirmed: true }
    })
    expect(received.status()).toBe(201)
    const receipt = await received.json()
    expect(receipt.tax_withheld).toBeNull()
    expect(receipt.total_dividend).toBeNull()
    const summary = await request.get(
      'http://127.0.0.1:18000/api/corporate-actions/statistics/summary',
      {
        headers
      }
    )
    expect(summary.ok()).toBeTruthy()
    expect(Number((await summary.json()).cash_dividends.net_dividend)).toBe(90)
    await setAuthenticatedSession(page, token, createdUser)
    await page.goto('/corporate-actions')
    await expect(page.getByText(/仅净到账额已知/)).toBeVisible()
    await page.getByRole('tab', { name: /预计与待收股息/ }).click()
    await expect(page.getByRole('button', { name: /^接受 / })).toHaveCount(0)
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

test('dividend receipt review sends explicit completion and preserves cash amounts', async ({
  page,
  request
}) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  const token = await loginThroughApi(request, { username: createdUser.username, password })
  try {
    const receipt = {
      id: 991,
      symbol: 'DIV002',
      market: 'A股',
      action_type: 'CASH_DIVIDEND',
      payment_date: '2026-01-05',
      ex_date: '2026-01-01',
      currency: 'CNY',
      net_dividend: '90',
      total_dividend: null,
      tax_withheld: null,
      amount_basis: 'NET_ONLY',
      receipt_status: 'RECEIVED'
    }
    const suggestion = {
      id: 992,
      symbol: 'DIV002',
      market: 'A股',
      action_type: 'CASH_DIVIDEND',
      broker_account_id: 1,
      currency: 'CNY',
      ex_date: '2026-01-01',
      pay_date: '2026-01-05',
      status: 'NEW',
      receipt_state: 'PARTIAL',
      receipt_ids: [991],
      receipt_complete: false,
      received_by_currency: { CNY: 90 },
      source: 'tushare-dividend',
      updated_at: '2026-01-06T00:00:00Z'
    }
    let saved: Record<string, unknown> | null = null
    await setAuthenticatedSession(page, token, createdUser)
    await page.route('**/api/corporate-actions/suggestions?**', (route) =>
      route.fulfill({ json: saved ? [] : [suggestion] })
    )
    await page.route('**/api/corporate-actions/suggestions/count', (route) =>
      route.fulfill({ json: { total: saved ? 0 : 1 } })
    )
    await page.route('**/api/corporate-actions/suggestions/992/receipts', async (route) => {
      if (route.request().method() === 'PUT') {
        saved = route.request().postDataJSON()
        await route.fulfill({
          json: { ...suggestion, receipt_complete: true, receipt_state: 'RECEIVED' }
        })
      } else await route.fulfill({ json: [receipt] })
    })
    await page.goto('/corporate-actions')
    await page.getByRole('tab', { name: /预计与待收股息/ }).click()
    await expect(page.getByRole('button', { name: /^接受 / })).toHaveCount(0)
    await page.getByRole('button', { name: /^核对到账 / }).click()
    const dialog = page.getByRole('dialog', { name: '核对股息到账' })
    await expect(dialog).toContainText('税前及税额待核实')
    await dialog.getByText('我已核对，本次公告股息已全部到账', { exact: true }).click()
    await expect(
      dialog.getByRole('checkbox', { name: '我已核对，本次公告股息已全部到账' })
    ).toBeChecked()
    await dialog.getByText(/#991/).click()
    await expect(
      dialog.getByRole('checkbox', { name: '我已核对，本次公告股息已全部到账' })
    ).not.toBeChecked()
    await expect(dialog.getByRole('button', { name: '保存核对' })).toBeEnabled()
    await dialog.getByText(/#991/).click()
    await expect(
      dialog.getByRole('checkbox', { name: '我已核对，本次公告股息已全部到账' })
    ).not.toBeChecked()
    await dialog.getByText('我已核对，本次公告股息已全部到账', { exact: true }).click()
    await dialog.getByRole('button', { name: '保存核对' }).click()
    await expect(dialog).toBeHidden()
    expect(saved).toEqual({
      receipt_ids: [991],
      receipt_complete: true,
      expected_updated_at: suggestion.updated_at
    })
    await expect(page.getByRole('button', { name: /^核对到账 / })).toHaveCount(0)
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

test('dividend forecasts distinguish automatic statement completion, manual confirmation and unresolved evidence', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  const base = {
    symbol: 'DIVCHECK',
    market: '港股',
    action_type: 'CASH_DIVIDEND',
    currency: 'HKD',
    broker_account_id: 1,
    ex_date: '2026-01-01',
    pay_date: '2026-01-05',
    status: 'MATCHED',
    receipt_ids: [991],
    received_by_currency: { HKD: 90 },
    source: 'hkexnews-dividend',
    updated_at: '2026-01-06T00:00:00Z',
    completion_date: '2026-01-05',
    remaining_estimated_gross: null,
    receipt_complete: false,
    receipt_state: 'PARTIAL',
    overdue: true,
    completion_source: null,
    review_reason: null
  }
  const rows = [
    {
      ...base,
      id: 992,
      symbol: 'AUTO',
      receipt_state: 'RECEIVED',
      receipt_complete: true,
      completion_source: 'statement',
      remaining_estimated_gross: 0
    },
    {
      ...base,
      id: 993,
      symbol: 'MANUAL',
      receipt_state: 'RECEIVED',
      receipt_complete: true,
      completion_source: 'manual',
      remaining_estimated_gross: 0
    },
    { ...base, id: 994, symbol: 'CROSS', review_reason: 'receipt_currency_mismatch' },
    { ...base, id: 995, symbol: 'UNKNOWN', review_reason: 'currency_unverified' },
    { ...base, id: 996, symbol: 'MULTIPLE', review_reason: 'ambiguous_receipt' }
  ]
  await page.route('**/api/corporate-actions/suggestions?**', (route) =>
    route.fulfill({ json: rows })
  )
  await page.route('**/api/corporate-actions/suggestions/count', (route) =>
    route.fulfill({ json: { total: 3 } })
  )
  await page.route('**/api/corporate-actions/suggestions/992/receipts', (route) =>
    route.fulfill({ json: [] })
  )
  await setAuthenticatedSession(page, token)
  await page.goto('/corporate-actions')
  await page.getByRole('tab', { name: /预计与待收股息/ }).click()
  const automatic = page.locator('.suggestions-table tr', { hasText: 'AUTO' })
  await expect(automatic).toContainText('对账单自动核对完成')
  await expect(automatic).toContainText('末笔到账 2026/01/05')
  await expect(page.locator('.suggestions-table tr', { hasText: 'MANUAL' })).toContainText(
    '人工确认收齐'
  )
  await expect(page.locator('.suggestions-table tr', { hasText: 'CROSS' })).toContainText(
    '缺少可核对的原币金额或换汇依据'
  )
  await expect(page.locator('.suggestions-table tr', { hasText: 'UNKNOWN' })).toContainText(
    '实际派息币种待核实'
  )
  await expect(page.locator('.suggestions-table tr', { hasText: 'MULTIPLE' })).toContainText(
    '同一到账记录可能对应多份公告'
  )
  await expect(page.getByText('已过预计派息日·仍有待收余额')).toHaveCount(0)
  await automatic.getByRole('button', { name: /^核对到账 / }).click()
  const dialog = page.getByRole('dialog', { name: '核对股息到账' })
  await expect(dialog).toContainText('对账单已自动核对完成，无需再次人工确认')
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
})
