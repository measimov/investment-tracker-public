import { expect, test } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'

test('仪表盘演示截图', async ({ browser }) => {
  const output = path.resolve('../docs/media')
  const evidence = path.resolve('demo-output/dashboard')
  fs.mkdirSync(evidence, { recursive: true })
  for (const width of [1440, 393]) {
    const context = await browser.newContext({
      viewport: { width, height: width === 393 ? 852 : 900 },
      deviceScaleFactor: width === 393 ? 2 : 1,
      locale: 'zh-CN',
      timezoneId: 'Asia/Shanghai',
      reducedMotion: 'reduce'
    })
    const page = await context.newPage()
    const response = await page.request.post('http://127.0.0.1:18100/api/auth/token', {
      data: { username: 'demo', password: process.env.DEMO_PASSWORD || 'demo-password' }
    })
    expect(response.ok()).toBeTruthy()
    const { access_token: token } = await response.json()
    await context.addCookies([
      { name: 'investment_session', value: token, url: 'http://127.0.0.1:18100', httpOnly: true },
      { name: 'investment_csrf', value: 'demo-csrf', url: 'http://127.0.0.1:18100' }
    ])
    await page.goto('/')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('.dashboard')).toHaveAttribute('aria-busy', 'false')
    await expect(page.locator('.dashboard-chart canvas')).toBeVisible()
    await page.waitForTimeout(900)
    await page.screenshot({ path: path.join(evidence, `dashboard-${width}-viewport.png`) })
    await page.screenshot({
      path: path.join(evidence, `dashboard-${width}-full.png`),
      fullPage: true
    })
    await page.screenshot({
      path: path.join(output, width === 1440 ? 'dashboard.png' : 'mobile-dashboard.png')
    })
    await context.close()
  }
})
