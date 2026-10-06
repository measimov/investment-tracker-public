import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { adminUser, loginThroughApi, setAuthenticatedSession } from './helpers'

const fixture = JSON.parse(
  readFileSync(new URL('../../docs/media/system-alerts-ui-fixture.json', import.meta.url), 'utf8')
)
test.use({ timezoneId: 'Asia/Shanghai' })

test('告警与事件读取分别未知、独立重试、旧结果保留及真空确认', async ({ page, request }) => {
  await setAuthenticatedSession(page, await loginThroughApi(request, adminUser))
  let alertsFailed = true,
    eventsFailed = true,
    empty = false,
    alertReads = 0,
    eventReads = 0
  await page.route('**/api/notifications/alerts', (route) => {
    alertReads++
    return route.fulfill(
      alertsFailed
        ? { status: 503, json: { detail: '明确虚构告警读取失败' } }
        : {
            json: empty
              ? {
                  ...fixture.alerts,
                  active: [],
                  recent_resolved: [],
                  counts: { active: 0, critical: 0, warning: 0, info: 0, recent_resolved: 0 }
                }
              : fixture.alerts
          }
    )
  })
  await page.route('**/api/notifications/events?*', (route) => {
    eventReads++
    expect(new URL(route.request().url()).searchParams.get('limit')).toBe('50')
    return route.fulfill(
      eventsFailed
        ? { status: 503, json: { detail: '明确虚构提醒读取失败' } }
        : { json: empty ? { ...fixture.events, items: [] } : fixture.events }
    )
  })
  await page.goto('/admin/alerts')
  const main = page.getByRole('main')
  await expect(main).toContainText('系统告警读取失败')
  await expect(main).toContainText('事件提醒读取失败')
  await expect(main.locator('.counts')).toContainText('严重 —')
  await expect(main).not.toContainText('没有未恢复的告警')
  await expect(main).not.toContainText('还没有事件提醒')
  alertsFailed = false
  await page.getByRole('button', { name: '重试告警读取', exact: true }).click()
  await expect(main).toContainText('UI明确虚构critical告警')
  await expect(main).toContainText('事件提醒读取失败')
  expect(eventReads).toBe(1)
  eventsFailed = false
  await page.getByRole('button', { name: '重试事件提醒读取', exact: true }).click()
  await expect(main).toContainText('明确虚构price_move事件正文')
  expect(alertReads).toBe(2)
  alertsFailed = eventsFailed = true
  await page.getByRole('button', { name: '重新加载系统告警与事件提醒', exact: true }).click()
  await expect(main).toContainText('下方保留上次成功的告警与渠道状态')
  await expect(main).toContainText('保留上次成功的提醒与设置')
  await expect(main).toContainText('UI明确虚构critical告警')
  await expect(main).toContainText('明确虚构price_move事件正文')
  alertsFailed = eventsFailed = false
  empty = true
  await page.getByRole('button', { name: '重新加载系统告警与事件提醒', exact: true }).click()
  await expect(main).toContainText('没有未恢复的告警')
  await expect(main).toContainText('近 7 天没有恢复的告警')
  await expect(main).toContainText('还没有事件提醒')
  await expect(main.locator('.counts')).toContainText('严重 0')
})

test('两个管理员写控制器仅显式触发，忙时不重复并保留原响应语义', async ({ page, request }) => {
  await setAuthenticatedSession(page, await loginThroughApi(request, adminUser))
  await page.route('**/api/notifications/alerts', (route) =>
    route.fulfill({ json: fixture.alerts })
  )
  await page.route('**/api/notifications/events?*', (route) =>
    route.fulfill({ json: fixture.events })
  )
  let checks = 0,
    tests = 0,
    releaseCheck!: () => void,
    releaseTest!: () => void
  const checkPending = new Promise<void>((resolve) => (releaseCheck = resolve))
  const testPending = new Promise<void>((resolve) => (releaseTest = resolve))
  await page.route('**/api/notifications/check', async (route) => {
    checks++
    expect(route.request().method()).toBe('POST')
    expect(route.request().postData()).toBeNull()
    await checkPending
    return route.fulfill({
      json: {
        ...fixture.alerts,
        counts: { active: 1, critical: 1, warning: 0, info: 0, recent_resolved: 1 },
        active: [{ ...fixture.alerts.active[0], title: '明确虚构检查后告警' }]
      }
    })
  })
  await page.route('**/api/notifications/test', async (route) => {
    tests++
    expect(route.request().method()).toBe('POST')
    expect(route.request().postData()).toBeNull()
    await testPending
    return route.fulfill({
      json: {
        status: 'partial',
        ok: false,
        configured: 2,
        sent: 1,
        message: '明确虚构测试部分成功',
        channels: [
          { kind: 'bark', channel: 'masked-sent-channel', ok: true },
          { kind: 'bark', channel: 'masked-channel', ok: false, error: '明确虚构发送失败' }
        ]
      }
    })
  })
  await page.goto('/admin/alerts')
  await expect(page.getByRole('main')).toContainText('UI明确虚构critical告警')
  expect(checks).toBe(0)
  expect(tests).toBe(0)
  const check = page.getByRole('button', { name: '立即检查', exact: true })
  await check.focus()
  await page.keyboard.press('Enter')
  await expect(check).toHaveAttribute('aria-busy', 'true')
  await check.click({ force: true })
  await page.keyboard.press('Enter')
  expect(checks).toBe(1)
  releaseCheck()
  await expect(page.getByRole('main')).toContainText('明确虚构检查后告警')
  await expect(check).toHaveAttribute('aria-busy', 'false')
  const send = page.getByRole('button', { name: '发送测试通知', exact: true })
  await send.click()
  await expect(send).toHaveAttribute('aria-busy', 'true')
  await send.click({ force: true })
  await send.focus()
  await page.keyboard.press('Enter')
  expect(tests).toBe(1)
  releaseTest()
  await expect(page.getByRole('main')).toContainText(
    '明确虚构测试部分成功（masked-channel：明确虚构发送失败）'
  )
  await page.getByRole('button', { name: '关闭测试通知结果', exact: true }).click()
  await expect(page.getByRole('main')).not.toContainText('明确虚构测试部分成功')
})

test('窄屏保留完整告警与提醒正文，原生说明键盘展开、时间和状态准确', async ({ page, request }) => {
  await page.setViewportSize({ width: 320, height: 852 })
  await setAuthenticatedSession(page, await loginThroughApi(request, adminUser))
  await page.route('**/api/notifications/alerts', (route) =>
    route.fulfill({ json: fixture.alerts })
  )
  await page.route('**/api/notifications/events?*', (route) =>
    route.fulfill({ json: fixture.events })
  )
  await page.goto('/admin/alerts')
  const active = page.getByRole('region', { name: '当前告警', exact: true })
  await expect(active.getByTestId('alert-card')).toHaveCount(3)
  const details = active.locator('details').first()
  await details.locator('summary').focus()
  await page.keyboard.press('Enter')
  await expect(details).toHaveAttribute('open', '')
  await expect(details.locator('p')).toHaveText(fixture.alerts.active[0].message)
  const recent = page.getByRole('region', { name: '最近提醒', exact: true })
  await expect(recent).toContainText('2026/10/03 07:30')
  await expect(recent).toContainText('未推送（未配置渠道）')
  await expect(recent).toContainText('推送失败（已重试 3 次）')
  const error = recent.locator('details').first()
  await error.locator('summary').focus()
  await page.keyboard.press('Enter')
  await expect(error.locator('p')).toHaveText(fixture.events.items[2].last_error)
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320)
})
