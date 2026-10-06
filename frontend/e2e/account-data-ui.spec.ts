import { expect, test } from '@playwright/test'
import { loginThroughApi, setAuthenticatedSession } from './helpers'

// 明确验证UTC时间戳在上海时区跨日；CI宿主时区不影响该浏览器条件。
test.use({ timezoneId: 'Asia/Shanghai' })

// 明确虚构浏览器记录；仅显式mock税分摊写入，其余场景只读。
const account = {
  id: 92001,
  broker: 'UI虚构券商',
  account_name: 'UI虚构账户',
  base_currency: 'CNY',
  account_number_masked: '****1234',
  is_active: true,
  has_records: true,
  notes: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z'
}
const batch = (id: number, filename: string, count: number) => ({
  id,
  archived_count: count,
  broker: account.broker,
  broker_account_id: account.id,
  completed_at: '2026-10-03T00:00:00Z',
  created_at: '2026-10-02T23:30:00Z',
  duplicate_count: 0,
  error_count: 0,
  error_message: null,
  imported_count: count,
  parser_name: 'ui-browser-fixture',
  parser_version: '0',
  period_end: '2026-09-30',
  period_start: '2026-09-01',
  row_count: count,
  skipped_count: 0,
  source_filename: filename,
  source_sha256: (id === 92011 ? 'a' : 'b').repeat(64),
  source_type: 'csv',
  status: 'COMPLETED'
})
const batches = [batch(92011, 'UI明确虚构文件A.csv', 3), batch(92012, 'UI明确虚构文件B.csv', 5)]
const resources = [
  'broker-accounts',
  'cash-events',
  'import-batches',
  'reconciliation-snapshots',
  'security-rules'
]

test('账户数据首次读取失败不是零或空，已知旧数据保留，只有成功空响应显示真空', async ({
  page,
  request
}) => {
  await setAuthenticatedSession(page, await loginThroughApi(request))
  const counts = new Map<string, number>()
  for (const resource of resources)
    await page.route(`**/api/${resource}?*`, async (route) => {
      expect(route.request().method()).toBe('GET')
      const n = (counts.get(resource) || 0) + 1
      counts.set(resource, n)
      if (n === 1 || n === 3)
        return route.fulfill({ status: 503, json: { detail: '明确虚构读取失败' } })
      return route.fulfill({
        json:
          n === 2
            ? resource === 'broker-accounts'
              ? [account]
              : resource === 'import-batches'
                ? batches
                : []
            : []
      })
    })
  // 无参数规则请求也属于同一个实际读取。
  await page.route('**/api/security-rules', async (route) => {
    const n = (counts.get('security-rules') || 0) + 1
    counts.set('security-rules', n)
    return route.fulfill(
      n === 1 || n === 3 ? { status: 503, json: { detail: '明确虚构规则读取失败' } } : { json: [] }
    )
  })
  await page.goto('/account-data')
  const main = page.getByRole('main')
  await expect(main.locator('.summary-item strong')).toHaveText(['—', '—', '—', '—'])
  for (const [tab, retry] of [
    ['账户', '重试账户'],
    ['现金事件', '重试现金事件'],
    ['导入批次', '重试导入批次'],
    ['月末核对', '重试月末核对'],
    ['特例规则', '重试特例规则']
  ]) {
    await page.getByRole('tab', { name: tab, exact: true }).click()
    await expect(page.getByRole('tabpanel')).not.toContainText('暂无')
    await expect(main.getByRole('button', { name: '新增第一个账户' })).toHaveCount(0)
    await main.getByRole('button', { name: retry, exact: true }).click()
  }
  await expect(main.locator('.summary-item strong')).toHaveText(['1', '0', '2026/10/03', '0/1'])
  await main.getByRole('button', { name: '刷新账户数据', exact: true }).click()
  await expect(main).toContainText('上次成功数据')
  await expect(main.locator('.summary-item strong')).toHaveText(['1', '0', '2026/10/03', '0/1'])
  for (const [tab, retry] of [
    ['账户', '重试账户'],
    ['现金事件', '重试现金事件'],
    ['导入批次', '重试导入批次'],
    ['月末核对', '重试月末核对'],
    ['特例规则', '重试特例规则']
  ]) {
    await page.getByRole('tab', { name: tab, exact: true }).click()
    await main.getByRole('button', { name: retry, exact: true }).click()
  }
  await expect(main.locator('.summary-item strong')).toHaveText(['0', '0', '尚无', '0/0'])
  await page.getByRole('tab', { name: '账户', exact: true }).click()
  await expect(main.getByRole('button', { name: '新增第一个账户' })).toBeVisible()
})

test('导入批次A关闭后B详情不能被迟到A覆盖，当前详情失败可只读重试', async ({ page, request }) => {
  await setAuthenticatedSession(page, await loginThroughApi(request))
  for (const resource of resources)
    await page.route(`**/api/${resource}*`, (route) =>
      route.fulfill({
        json:
          resource === 'broker-accounts' ? [account] : resource === 'import-batches' ? batches : []
      })
    )
  let releaseA: () => void = () => {}
  const heldA = new Promise<void>((resolve) => {
    releaseA = resolve
  })
  let aStarted = false,
    bReads = 0
  await page.route('**/api/import-batches/92011', async (route) => {
    aStarted = true
    await heldA
    await route.fulfill({ json: batches[0] })
  })
  await page.route('**/api/import-batches/92012', (route) => {
    bReads++
    return route.fulfill(
      bReads === 2 ? { status: 503, json: { detail: '明确虚构B详情失败' } } : { json: batches[1] }
    )
  })
  await page.goto('/account-data')
  await page.getByRole('tab', { name: '导入批次', exact: true }).click()
  const a = page.getByRole('button', { name: '查看 UI明确虚构文件A.csv 导入批次详情' })
  const b = page.getByRole('button', { name: '查看 UI明确虚构文件B.csv 导入批次详情' })
  await a.click()
  const drawer = page.getByRole('dialog', { name: '导入批次详情', exact: true })
  await expect.poll(() => aStarted).toBe(true)
  await expect(drawer).toContainText('UI明确虚构文件A.csv')
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  await b.click()
  await expect(drawer).toContainText('UI明确虚构文件B.csv')
  releaseA()
  await page.waitForLoadState('networkidle')
  await expect(drawer).not.toContainText('UI明确虚构文件A.csv')
  await expect(drawer).toContainText('2026/09/01 至 2026/09/30')
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  await b.click()
  await expect(drawer).toContainText('此批次详情加载失败')
  await expect(drawer).toContainText('UI明确虚构文件B.csv')
  await drawer.getByRole('button', { name: '重试此批次详情' }).click()
  await expect(drawer.getByText('此批次详情加载失败', { exact: true })).toHaveCount(0)
  await expect.poll(() => bReads).toBe(3)
})

test('股息税弹窗保存、取消和Esc关闭后沿成熟生命周期返回原具名入口', async ({ page, request }) => {
  await setAuthenticatedSession(page, await loginThroughApi(request))
  const tax = {
    id: 92102,
    event_type: 'TAX',
    amount: '0.12345678',
    currency: 'HKD',
    broker_account_id: account.id,
    event_date: '2026-09-30',
    notes: 'UI明确虚构税款',
    read_only: true,
    tax_kind: 'DIVIDEND',
    tax_allocations: [],
    unallocated_tax_amount: '0.12345678',
    created_at: account.created_at,
    updated_at: account.updated_at
  }
  const candidate = {
    id: 92402,
    action_type: 'CASH_DIVIDEND',
    amount_basis: 'NET_ONLY',
    broker_account_id: account.id,
    created_at: account.created_at,
    updated_at: account.updated_at,
    currency: 'HKD',
    ex_date: '2026-09-01',
    payment_date: '2026-09-29',
    market: '港股',
    symbol: 'UIFICTION',
    name: 'UI明确虚构候选',
    net_dividend: '1.50',
    total_dividend: null,
    tax_withheld: null,
    receipt_status: 'RECEIVED',
    read_only: true
  }
  let writes = 0
  let releaseSave = () => {}
  let pendingSave: Promise<void>
  for (const resource of resources)
    await page.route(`**/api/${resource}*`, (route) =>
      route.fulfill({
        json: resource === 'broker-accounts' ? [account] : resource === 'cash-events' ? [tax] : []
      })
    )
  await page.route('**/api/corporate-actions?*', (route) => route.fulfill({ json: [candidate] }))
  await page.route('**/api/cash-events/92102/dividend-allocations', async (route) => {
    expect(route.request().method()).toBe('PUT')
    expect(route.request().postDataJSON()).toEqual({
      allocations: [{ corporate_action_id: 92402, amount: '0.12345678' }]
    })
    writes++
    await pendingSave
    return route.fulfill({ json: tax })
  })
  for (const width of [393, 1440]) {
    pendingSave = new Promise<void>((resolve) => {
      releaseSave = resolve
    })
    await page.setViewportSize({ width, height: 852 })
    await page.goto('/account-data')
    await page.getByRole('tab', { name: '现金事件', exact: true }).click()
    const opener = page.getByRole('button', { name: / 股息税归属$/ }),
      dialog = page.getByRole('dialog', { name: '股息税归属', exact: true })
    for (const close of ['取消', 'Escape']) {
      await opener.focus()
      await opener.press('Enter')
      await expect(dialog).toBeVisible()
      if (close === 'Escape') await page.keyboard.press('Escape')
      else await dialog.getByRole('button', { name: '取消', exact: true }).click()
      await expect(dialog).toBeHidden()
      await expect(opener).toBeFocused()
    }
    await opener.press('Enter')
    await dialog.getByRole('button', { name: '添加分摊', exact: true }).click()
    await dialog.getByRole('combobox', { name: '第 1 行归属股息', exact: true }).click()
    await page
      .locator('.el-select-dropdown:visible')
      .getByRole('option', { name: 'UIFICTION UI明确虚构候选 · 2026/09/29 · —', exact: true })
      .click()
    await dialog
      .getByRole('spinbutton', { name: '第 1 行股息税分摊金额', exact: true })
      .fill('0.12345678')
    const saveButton = dialog.getByRole('button', { name: '保存归属', exact: true })
    await saveButton.click()
    await expect(saveButton).toHaveAttribute('aria-busy', 'true')
    await expect(saveButton).toBeDisabled()
    await expect(saveButton).toBeFocused()
    await saveButton.click({ force: true })
    await saveButton.press('Enter')
    expect(writes).toBe(width === 393 ? 1 : 2)
    releaseSave()
    await expect(dialog).toBeHidden()
    await expect(opener).toBeFocused()
  }
  expect(writes).toBe(2)
})
