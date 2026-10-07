import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import {
  adminUser,
  loginThroughApi,
  setAuthenticatedSession,
  mockXueqiuCapabilities
} from './helpers'

const opinions = JSON.parse(
  readFileSync(new URL('../../docs/media/opinions-ui-fixture.json', import.meta.url), 'utf8')
)

test('switching from pasted cookies to a file waits for that file before submitting', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request, adminUser)
  await setAuthenticatedSession(page, token)
  await mockXueqiuCapabilities(page, { configured: true })
  await page.route('**/api/securities/opinion-summaries', (route) =>
    route.fulfill({ json: opinions.summaries })
  )
  await page.route('**/api/xueqiu-collector/status', (route) =>
    route.fulfill({ json: opinions.collector })
  )
  const content = JSON.stringify({
    cookies: ['xq_a_token', 'xqat'].map((name) => ({
      name,
      value: 'SYNTH_NEW_FILE_TOKEN',
      expirationDate: Date.now() / 1000 + 14 * 86400
    }))
  })
  const status = {
    source: 'file',
    writable: true,
    level: 'normal',
    message: '合成 Cookie 测试',
    keys: ['xq_a_token', 'xqat'],
    primary: ['xq_a_token', 'xqat'].map((name) => ({ name, present: true, days_left: 14 }))
  }
  const submissions: unknown[] = []
  await page.route('**/api/xueqiu-collector/cookie', (route) => {
    if (route.request().method() === 'GET') return route.fulfill({ json: status })
    submissions.push(route.request().postDataJSON())
    return route.fulfill({
      json: { status, backup_created: true, source_format: 'j2team', notes: [], probe: null }
    })
  })

  await page.goto('/opinions')
  await page
    .getByTestId('xueqiu-collector-card')
    .getByRole('button', { name: '运行详情、关注作者、跟踪组合与最近运行', exact: true })
    .click()
  await page.getByTestId('collector-update-cookie').click()
  const dialog = page.getByTestId('xueqiu-cookie-dialog')
  await dialog.locator('textarea').fill('xq_a_token=SYNTH_OLD_PASTE; xqat=SYNTH_OLD_PASTE')
  await page.evaluate(() => {
    const original = File.prototype.text
    File.prototype.text = function () {
      return new Promise<string>((resolve) => {
        Object.assign(window, { releaseCookieFile: () => original.call(this).then(resolve) })
      })
    }
  })
  await dialog.locator('input[type=file]').setInputFiles({
    name: 'new-cookie.json',
    mimeType: 'application/json',
    buffer: Buffer.from(content)
  })

  await expect(dialog.getByTestId('xueqiu-cookie-file-reading')).toBeVisible()
  await expect(dialog.getByTestId('xueqiu-cookie-submit')).toBeDisabled()
  expect(submissions).toEqual([])

  await page.evaluate(() => {
    const controlledWindow = window as unknown as Window & {
      releaseCookieFile: () => Promise<void>
    }
    return controlledWindow.releaseCookieFile()
  })
  await expect(dialog.getByTestId('xueqiu-cookie-file')).toContainText('new-cookie.json')
  await dialog.getByTestId('xueqiu-cookie-submit').click()
  await expect(dialog.getByTestId('xueqiu-cookie-result')).toContainText('Cookie 已更新')
  expect(submissions).toEqual([{ content, probe: false }])
  await expect(dialog).not.toContainText('SYNTH_NEW_FILE_TOKEN')
  await expect(dialog.locator('textarea')).toHaveValue('')
})
