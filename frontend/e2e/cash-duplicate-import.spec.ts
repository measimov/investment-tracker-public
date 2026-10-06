import { expect, test } from '@playwright/test'
import {
  createTemporaryUser,
  deleteTemporaryUser,
  loginThroughApi,
  setAuthenticatedSession
} from './helpers'

test('cash duplicate rows show cash values and confirm the same source hash', async ({
  page,
  request
}) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  const token = await loginThroughApi(request, { username: createdUser.username, password })
  const hash = 'c'.repeat(64)
  let previews = 0
  let confirmedPreview = ''
  let committed = ''
  const result = (confirmed: boolean) => ({
    broker: '招商证券',
    filename: 'statement.pdf',
    total_rows: 1,
    eligible_trade_rows: 0,
    eligible_cash_rows: 1,
    imported_transactions: 0,
    imported_cash_events: confirmed ? 1 : 0,
    duplicate_rows: 0,
    skipped_non_trade_rows: 0,
    skipped_invalid_rows: 0,
    skipped_excluded_rows: 0,
    affected_symbols: 0,
    business_counts: { 质押回购拆出: 1 },
    duplicate_samples: [],
    import_samples: [],
    errors: [],
    warnings: [],
    batch_status: confirmed ? 'COMPLETED' : 'PARTIAL',
    reconciliation_status: 'UNVERIFIED',
    suspected_duplicate_rows: confirmed ? 0 : 1,
    suspected_duplicate_samples: confirmed
      ? []
      : [
          {
            row_number: 1,
            symbol: '131810',
            name: '回购',
            market: 'A股',
            transaction_type: 'TRANSFER_OUT',
            trade_date: '2026-06-01',
            quantity: '161',
            amount: '-161001.61',
            currency: 'CNY',
            price: '1.365',
            existing_price: '1.37',
            existing_amount: '-161001.61',
            existing_currency: 'CNY',
            existing_date: '2026-06-01',
            existing_source_filename: 'old-statement.pdf',
            existing_row_number: 12,
            row_hash: hash,
            previously_held: false,
            reason: '同账户、日期、业务、数量、金额、费用及余额相同，价格/利率精度不同'
          }
        ]
  })
  try {
    const created = await request.post('http://127.0.0.1:18000/api/broker-accounts', {
      headers: { Authorization: `Bearer ${token}` },
      data: { broker: '招商证券', account_name: '现金防重账户', base_currency: 'CNY' }
    })
    expect(created.ok()).toBeTruthy()
    await page.route('**/api/import/cmb-fund-flows/preview', async (route) => {
      const body = route.request().postData() || ''
      previews++
      if (body.includes(hash)) confirmedPreview = body
      await route.fulfill({ json: result(body.includes(hash)) })
    })
    await page.route('**/api/import/cmb-fund-flows', async (route) => {
      committed = route.request().postData() || ''
      await route.fulfill({ json: result(true) })
    })
    await setAuthenticatedSession(page, token, createdUser)
    await page.goto('/transactions')
    await page.getByRole('button', { name: '导入', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '导入数据' })
    await dialog.getByRole('tab', { name: /招商/ }).click()
    await dialog.locator('.el-select').click()
    await page
      .locator('.el-select-dropdown:visible')
      .getByRole('option')
      .filter({ hasText: '现金防重账户' })
      .click()
    await dialog.locator('input[type=file]').setInputFiles({
      name: 'statement.pdf',
      mimeType: 'application/pdf',
      buffer: Buffer.from('%PDF-fixture; preview intercepted')
    })
    await dialog.getByRole('button', { name: '预览', exact: true }).click()
    const block = dialog.getByTestId('suspected-duplicates')
    await expect(block).toContainText('资金转出')
    await expect(block).toContainText('-¥161,001.61')
    await expect(block).toContainText('old-statement.pdf 第 12 行')
    // A repo rate is not displayed as the transaction price comparison.
    await expect(block).not.toContainText('1.37 → 1.365')
    await block.locator('.el-table__body .el-checkbox').first().click()
    await block.getByRole('button', { name: '确认所选为真实的另一笔并重新预览' }).click()
    await expect.poll(() => previews).toBe(2)
    expect(confirmedPreview).toContain('confirm_suspected_row_hashes')
    expect(confirmedPreview).toContain(hash)
    await expect(block).not.toBeVisible()
    await dialog.getByRole('button', { name: '导入', exact: true }).click()
    await expect.poll(() => committed).toContain(hash)
    expect(committed).toContain('confirm_suspected_row_hashes')
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})
