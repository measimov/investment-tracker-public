import { expect, test } from '@playwright/test'
import {
  createTemporaryUser,
  deleteTemporaryUser,
  loginThroughApi,
  setAuthenticatedSession
} from './helpers'

test('a cancelled preview cannot authorize a different account or end its loading state', async ({
  page,
  request
}) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  const token = await loginThroughApi(request, { username: createdUser.username, password })
  const accounts: Array<{ id: number; account_name: string }> = []
  let releaseOld!: () => void
  let releaseNew!: () => void
  const oldGate = new Promise<void>((resolve) => (releaseOld = resolve))
  const newGate = new Promise<void>((resolve) => (releaseNew = resolve))
  let previews = 0
  let committedAccount: string | undefined
  const response = (warning: string) => ({
    total_rows: 1,
    eligible_transaction_rows: 1,
    imported_transactions: 1,
    duplicate_rows: 0,
    errors: [],
    warnings: [warning],
    batch_status: 'COMPLETED',
    reconciliation_status: 'UNVERIFIED'
  })
  try {
    for (const account_name of ['竞态账户一', '竞态账户二']) {
      const created = await request.post('http://127.0.0.1:18000/api/broker-accounts', {
        headers: { Authorization: `Bearer ${token}` },
        data: { broker: 'IBKR', account_name, base_currency: 'USD' }
      })
      expect(created.ok()).toBeTruthy()
      accounts.push(await created.json())
    }
    await page.route('**/api/import/ibkr-activity/preview', async (route) => {
      const current = ++previews
      await (current === 1 ? oldGate : newGate)
      await route.fulfill({ json: response(current === 1 ? '旧预览' : '新预览') })
    })
    await page.route('**/api/import/ibkr-activity', async (route) => {
      const body = route.request().postData() || ''
      committedAccount = body.match(/name="broker_account_id"\r\n\r\n(\d+)/)?.[1]
      await route.fulfill({ json: { ...response(''), warnings: [] } })
    })
    await setAuthenticatedSession(page, token, createdUser)
    await page.goto('/transactions')
    await page.getByRole('button', { name: '导入', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '导入数据' })
    await dialog.getByRole('tab', { name: 'IBKR 活动报表' }).click()
    await dialog.locator('.el-select').click()
    await page
      .locator('.el-select-dropdown:visible')
      .getByRole('option')
      .filter({ hasText: accounts[0].account_name })
      .click()
    await dialog.locator('input[type=file]').setInputFiles({
      name: 'statement.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from('fixture only; intercepted preview')
    })
    await dialog.getByRole('button', { name: '预览', exact: true }).click()
    await expect.poll(() => previews).toBe(1)
    await expect(dialog.getByRole('tab', { name: '标准交易文件' })).toHaveClass(/is-disabled/)
    await dialog.getByRole('button', { name: '取消', exact: true }).click()
    await expect(dialog).not.toBeVisible()
    await page.getByRole('button', { name: '导入', exact: true }).click()
    await dialog.locator('.el-select').click()
    await page
      .locator('.el-select-dropdown:visible')
      .getByRole('option')
      .filter({ hasText: accounts[1].account_name })
      .click()
    const previewButton = dialog.getByRole('button', { name: '预览', exact: true })
    await previewButton.click()
    await expect.poll(() => previews).toBe(2)
    const oldResponse = page.waitForResponse('**/api/import/ibkr-activity/preview')
    releaseOld()
    await oldResponse
    // The old request's finally must not turn off the new request's spinner.
    await expect(previewButton).toHaveClass(/is-loading/)
    await expect(dialog.getByTestId('import-preview-title')).toHaveCount(0)
    releaseNew()
    await expect(dialog.getByTestId('import-preview-title')).toHaveText('预览结果')
    await expect(dialog.getByText('新预览', { exact: true })).toBeVisible()
    await expect(dialog.getByText('旧预览', { exact: true })).toHaveCount(0)
    await dialog.getByRole('button', { name: '导入', exact: true }).click()
    await expect.poll(() => committedAccount).toBe(String(accounts[1].id))
  } finally {
    releaseOld()
    releaseNew()
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})
