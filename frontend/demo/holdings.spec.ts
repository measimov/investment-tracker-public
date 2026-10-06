import { expect, test } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'

// 仅使用项目离线演示库；与完整 README 录制分开，便于持仓试点逐轮复验。
test('持仓页截图', async ({ browser }) => {
  const output = process.env.HOLDINGS_CAPTURE_DIR || path.resolve('../docs/media')
  fs.mkdirSync(output, { recursive: true })
  for (const width of [1440, 1280, 1024, 768, 393, 375, 320]) {
    const context = await browser.newContext({
      viewport: { width, height: width < 640 ? 852 : 900 },
      deviceScaleFactor: width < 640 ? 2 : 1,
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
    await expect(page.getByTestId('holdings-count')).toContainText('只持仓')
    const name =
      width === 1440 ? 'holdings' : width === 393 ? 'mobile-holdings' : `holdings-${width}`
    const evidence = process.env.HOLDINGS_CAPTURE_DIR || path.resolve('demo-output/holdings')
    fs.mkdirSync(evidence, { recursive: true })
    await page.screenshot({ path: path.join(evidence, `${name}-viewport.png`) })
    await page.screenshot({ path: path.join(evidence, `${name}-full.png`), fullPage: true })
    if (width === 1440 || width === 393)
      await page.screenshot({ path: path.join(output, `${name}.png`), fullPage: true })
    await context.close()
  }
})
