import { expect, test } from '@playwright/test'
import { loginThroughApi, setAuthenticatedSession } from './helpers'

const transaction = {
  id: 1,
  broker_account_id: 8,
  transaction_date: '2026-01-02',
  symbol: '00700',
  name: '腾讯控股',
  market: '港股',
  transaction_type: 'BUY',
  quantity: 100,
  price: 300,
  amount: 30000,
  currency: 'HKD',
  commission: 0
}

test('交易先返回而账户延迟、失败时不误报删除，重试成功后显示脱敏账户', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await page.route('**/api/transactions**', (route) =>
    route.fulfill({ json: route.request().url().includes('/count') ? { total: 1 } : [transaction] })
  )
  let release!: () => void
  const delayed = new Promise<void>((resolve) => (release = resolve))
  let failed = true
  await page.route('**/api/broker-accounts**', async (route) => {
    expect(new URL(route.request().url()).searchParams.get('limit')).toBe('1000')
    await delayed
    await route.fulfill(
      failed
        ? { status: 422, json: { detail: '账户列表暂不可用' } }
        : { json: [{ id: 8, account_name: 'IBKR U12345678' }] }
    )
  })
  await page.goto('/transactions')
  const table = page.locator('.desktop-data-table')
  await expect(table).toContainText('账户加载中')
  await expect(table).not.toContainText('已删除账户')
  release()
  await expect(table).toContainText('账户暂不可用')
  await expect(page.locator('.el-message--error')).toContainText('账户列表暂不可用')
  failed = false
  await page.reload()
  await expect(table).toContainText('IBKR U***5678')
  await expect(page.locator('body')).not.toContainText('U12345678')
})

test('持仓账户选项使用脱敏全称区分同名账户', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await page.route('**/api/broker-accounts**', (route) =>
    route.fulfill({
      json: [
        {
          id: 8,
          account_name: 'IBKR U12345678',
          broker: 'IBKR',
          account_number_masked: 'U12345678'
        },
        {
          id: 9,
          account_name: 'IBKR U12345678',
          broker: 'IBKR',
          account_number_masked: 'U87654321'
        }
      ]
    })
  )
  await page.goto('/holdings')
  await page.getByTestId('holdings-account-filter').click()
  await expect(
    page.locator('.n-base-select-option', { hasText: 'IBKR U***5678 · IBKR · U***5678' })
  ).toBeVisible()
  await expect(
    page.locator('.n-base-select-option', { hasText: 'IBKR U***5678 · IBKR · U***4321' })
  ).toBeVisible()
  await expect(page.locator('body')).not.toContainText('U12345678')
  await expect(page.locator('body')).not.toContainText('U87654321')
})
