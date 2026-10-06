import { expect, test } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'

test('导航外壳演示截图', async ({ browser }) => {
  const output = path.resolve('../docs/media')
  const evidence = path.resolve('demo-output/navigation')
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
    await page.goto('/holdings')
    await page.waitForLoadState('networkidle')
    await expect(page.getByTestId('holdings-count')).toContainText('8 只持仓')
    await page.screenshot({ path: path.join(evidence, `holdings-${width}-viewport.png`) })
    await page.screenshot({
      path: path.join(evidence, `holdings-${width}-full.png`),
      fullPage: true
    })
    if (width === 1440) {
      await page.screenshot({ path: path.join(output, 'navigation.png') })
      await page.getByRole('button', { name: '收起导航' }).click()
      await expect(page.locator('.desktop-sidebar')).toHaveCount(0)
      await page.screenshot({ path: path.join(evidence, 'holdings-collapsed-1440.png') })
    } else {
      await page.getByRole('button', { name: '打开导航' }).click()
      await expect(page.getByRole('navigation', { name: '主导航' })).toBeVisible()
      await expect.poll(async () => (await page.locator('.n-drawer').boundingBox())?.x).toBe(0)
      await page.screenshot({ path: path.join(output, 'mobile-navigation.png') })
    }
    await context.close()
  }
})
