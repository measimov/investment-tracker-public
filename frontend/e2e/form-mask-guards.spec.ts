import { expect, test } from '@playwright/test'
import { loginThroughApi, setAuthenticatedSession } from './helpers'

const opening = {
  id: 9971,
  symbol: 'UI-OPEN',
  name: '虚构未填成本样例（非真实持仓）',
  market: '港股',
  currency: 'HKD',
  action_type: 'OPENING_POSITION',
  ex_date: '2026-01-02',
  adjusted_quantity: '12.125',
  adjusted_cost_per_share: null,
  cost_basis_adjustment: null,
  read_only: true,
  import_batch_id: 9972,
  notes: '虚构原备注'
}

test('成熟录入表单点遮罩保留草稿，取消不写入且返回入口焦点', async ({ page, request }) => {
  await setAuthenticatedSession(page, await loginThroughApi(request))
  const writes: string[] = []
  await page.route('**/api/**', (route) => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    if (req.method() !== 'GET' && path !== '/api/auth/refresh') {
      writes.push(path)
      return route.abort()
    }
    if (path === '/api/corporate-actions') return route.fulfill({ json: [opening] })
    if (path === '/api/corporate-actions/count') return route.fulfill({ json: { total: 1 } })
    return route.continue()
  })
  for (const width of [1440, 393]) {
    await page.setViewportSize({ width, height: 852 })
    for (const [path, button, title] of [
      ['/transactions', '新增交易', '新增交易'],
      ['/corporate-actions', '新增记录', '新增公司行动'],
      ['/corporate-actions', '补录 UI-OPEN 2026/01/02 成本', '补录期初建仓成本']
    ]) {
      await page.goto(path)
      const trigger = page.getByRole('button', { name: button, exact: true })
      await trigger.click()
      const dialog = page.getByRole('dialog', { name: title, exact: true })
      await expect(dialog).toBeVisible()
      const draft = dialog.locator('textarea')
      await draft.fill('虚构未保存草稿')
      if (width === 1440) {
        await expect
          .poll(async () => (await dialog.locator('.el-dialog').boundingBox())?.x ?? 0)
          .toBeGreaterThan(2)
        await page.mouse.click(2, 2)
        await expect(dialog).toBeVisible()
        await expect(draft).toHaveValue('虚构未保存草稿')
      }
      await dialog.getByRole('button', { name: '取消', exact: true }).click()
      await expect(dialog).toBeHidden()
      await expect(trigger).toBeFocused()
      await trigger.click()
      await expect(dialog).toBeVisible()
      await expect(draft).toHaveValue(title === '补录期初建仓成本' ? '虚构原备注' : '')
      await page.keyboard.press('Escape')
      await expect(dialog).toBeHidden()
      await expect(trigger).toBeFocused()
    }
  }
  expect(writes).toEqual([])
})
