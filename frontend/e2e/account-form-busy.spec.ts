import { expect, test } from '@playwright/test'
import { loginThroughApi, setAuthenticatedSession } from './helpers'
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
for (const width of [393, 1440])
  for (const feature of ['account', 'cash']) {
    test(`${width}px ${feature} form preserves keyboard state and pending drafts, blocks duplicate writes, and returns focus`, async ({
      page,
      request
    }) => {
      await page.setViewportSize({ width, height: 852 })
      await setAuthenticatedSession(page, await loginThroughApi(request))
      const path = feature === 'account' ? 'broker-accounts' : 'cash-events'
      for (const name of [
        'broker-accounts',
        'cash-events',
        'import-batches',
        'reconciliation-snapshots',
        'security-rules'
      ]) {
        await page.route(`**/api/${name}*`, (route) =>
          route.fulfill({ json: name === 'broker-accounts' ? [account] : [] })
        )
      }
      let writes = 0,
        release!: () => void,
        fail = true
      let held = new Promise<void>((resolve) => (release = resolve))
      const accountPayload = {
        account_name: 'UI明确虚构草稿',
        broker: account.broker,
        account_number_masked: '****8888',
        base_currency: 'CNY',
        is_active: true,
        notes: 'UI明确虚构保留备注'
      }
      const cashPayload = {
        broker_account_id: account.id,
        event_date: '2026-09-30',
        event_type: 'DEPOSIT',
        tax_kind: null,
        amount: 12.34,
        currency: 'CNY',
        notes: 'UI明确虚构保留备注'
      }
      await page.route(`**/api/${path}`, async (route) => {
        if (route.request().method() === 'GET') return route.fallback()
        expect(route.request().method()).toBe('POST')
        expect(route.request().postDataJSON()).toEqual(
          feature === 'account' ? accountPayload : cashPayload
        )
        writes++
        await held
        return fail
          ? route.fulfill({ status: 503, json: { detail: 'UI明确虚构保存失败，草稿仍在' } })
          : route.fulfill({
              json:
                feature === 'account'
                  ? { ...account, ...accountPayload, id: 92002, has_records: false }
                  : {
                      ...cashPayload,
                      id: 92002,
                      read_only: false,
                      tax_allocations: [],
                      unallocated_tax_amount: null,
                      created_at: account.created_at,
                      updated_at: account.updated_at
                    }
            })
      })
      try {
        await page.goto('/account-data')
        await expect(page.getByRole('main').locator('.summary-item strong').first()).toHaveText('1')
        if (feature === 'cash')
          await page.getByRole('tab', { name: '现金事件', exact: true }).click()
        const opener = page.getByRole('button', {
          name: feature === 'account' ? '新增账户' : '新增事件',
          exact: true
        })
        await opener.focus()
        await opener.press('Enter')
        const dialog = page.getByRole('dialog', {
          name: feature === 'account' ? '新增券商账户' : '新增现金事件',
          exact: true
        })
        await expect(dialog).toBeVisible()
        if (feature === 'account') {
          await dialog
            .getByRole('textbox', { name: '账户名称', exact: true })
            .fill(accountPayload.account_name)
          const broker = dialog.getByRole('combobox', { name: '券商', exact: true })
          await broker.fill(account.broker)
          await broker.press('Enter')
          await dialog
            .getByRole('textbox', { name: '账户尾号', exact: true })
            .fill(accountPayload.account_number_masked)
          const state = dialog.getByRole('checkbox', { name: '启用账户', exact: true })
          await expect(state).toBeChecked()
          await state.focus()
          await state.press('Space')
          await expect(state).not.toBeChecked()
          await state.press('Space')
          await expect(state).toBeChecked()
          if (width === 393) expect((await state.boundingBox())!.height).toBeGreaterThanOrEqual(44)
        } else {
          const date = dialog.getByRole('combobox', { name: '现金事件日期', exact: true })
          await date.fill('2026/09/30')
          await date.press('Tab')
          await dialog.getByRole('spinbutton', { name: '现金事件金额', exact: true }).fill('12.34')
        }
        const note = dialog.getByRole('textbox', {
          name: feature === 'account' ? '账户备注' : '现金事件备注',
          exact: true
        })
        await note.fill('UI明确虚构保留备注')
        await expect(
          dialog.getByRole('button', { name: '关闭此对话框', exact: true })
        ).toBeVisible()
        const save = dialog.getByRole('button', { name: '保存', exact: true }),
          cancel = dialog.getByRole('button', { name: '取消', exact: true })
        await save.click()
        await expect.poll(() => writes).toBe(1)
        await expect(save).toHaveAttribute('aria-busy', 'true')
        await expect(save).toHaveAttribute('aria-disabled', 'true')
        await expect(save).toBeFocused()
        await expect(cancel).toBeDisabled()
        await expect(dialog.getByRole('button', { name: '关闭此对话框' })).toHaveCount(0)
        await save.click({ force: true })
        await save.press('Enter')
        await page.keyboard.press('Escape')
        await expect(dialog).toBeVisible()
        expect(writes).toBe(1)
        release()
        await expect(dialog).toBeVisible()
        await expect(page.locator('#app')).toContainText('UI明确虚构保存失败，草稿仍在')
        await expect(note).toHaveValue('UI明确虚构保留备注')
        await expect(save).toHaveAttribute('aria-busy', 'false')
        await expect(cancel).toBeEnabled()
        fail = false
        held = new Promise<void>((resolve) => (release = resolve))
        await save.click()
        await expect.poll(() => writes).toBe(2)
        await expect(save).toBeFocused()
        release()
        await expect(dialog).toBeHidden()
        await expect(opener).toBeFocused()
        await opener.press('Enter')
        await expect(dialog).toBeVisible()
        await cancel.click()
        await expect(dialog).toBeHidden()
        await expect(opener).toBeFocused()
        await opener.press('Enter')
        await expect(dialog).toBeVisible()
        await page.keyboard.press('Escape')
        await expect(dialog).toBeHidden()
        await expect(opener).toBeFocused()
        expect(writes).toBe(2)
        expect(await page.evaluate(() => document.body.scrollWidth)).toBe(width)
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width)
      } finally {
        release()
      }
    })
  }
