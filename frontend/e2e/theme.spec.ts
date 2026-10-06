import { expect, test } from '@playwright/test'
import { loginThroughApi, setAuthenticatedSession } from './helpers'

async function chooseTheme(page: import('@playwright/test').Page, label: string) {
  const settings = page.getByRole('button', { name: '设置', exact: true })
  if ((await settings.getAttribute('aria-expanded')) !== 'true') await settings.click()
  await page.getByRole('button', { name: /^外观：/ }).click()
  await page.locator('.n-dropdown-option').filter({ hasText: label }).click()
  await settings.click()
}

test('system preference, explicit override and refresh work on the public shell', async ({
  page
}) => {
  await page.emulateMedia({ colorScheme: 'dark' })
  await page.goto('/login')
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await chooseTheme(page, '浅色')
  await page.emulateMedia({ colorScheme: 'light' })
  await page.emulateMedia({ colorScheme: 'dark' })
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  await page.reload()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  await chooseTheme(page, '跟随系统')
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await page.emulateMedia({ colorScheme: 'light' })
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
})

test('switching appearance retains amounts and updates positive and negative colors', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  const response = await request.get('http://127.0.0.1:18000/api/statistics/portfolio-snapshot', {
    headers: { Authorization: `Bearer ${token}` }
  })
  expect(response.ok()).toBeTruthy()
  const snapshot = await response.json()
  await page.route('**/api/statistics/portfolio-snapshot', (route) =>
    route.fulfill({
      json: {
        ...snapshot,
        markets: [{ market: 'A股', total_cost: 12345 }],
        performance: {
          ...snapshot.performance,
          current_performance: {
            ...snapshot.performance.current_performance,
            current_market_value_cny: 12345
          },
          receivable_return: {
            ...snapshot.performance.receivable_return,
            cash_basis_return_cny: 120,
            estimated_return_cny: -50,
            known_pending_gross_cny: null
          }
        }
      }
    })
  )
  await page.goto('/')
  await chooseTheme(page, '浅色')
  const chart = page.locator('.dashboard-chart canvas').first()
  await page.locator('section[aria-label="市场分布"]').scrollIntoViewIfNeeded()
  await expect(chart).toBeVisible()
  const amount = page.getByTestId('dashboard-market-value')
  const positive = page.getByTestId('cash-basis-return')
  const negative = page.getByTestId('estimated-dividend-return')
  const unknown = page.getByTestId('pending-dividend-return')
  const unknownUsesTextColor = () =>
    unknown.evaluate((element) => {
      const probe = document.createElement('span')
      probe.style.color = 'var(--app-text)'
      document.documentElement.append(probe)
      const expected = getComputedStyle(probe).color
      probe.remove()
      return getComputedStyle(element).color === expected
    })
  await expect(amount).toContainText('12,345')
  await expect(positive).toContainText('120')
  await expect(negative).toHaveText('-¥50.00')
  await expect(unknown).toHaveText('—')
  await expect.poll(unknownUsesTextColor).toBe(true)
  const values = await Promise.all([
    amount.textContent(),
    positive.textContent(),
    negative.textContent()
  ])
  await expect(positive).toHaveCSS('color', 'rgb(8, 127, 91)')
  await expect(negative).toHaveCSS('color', 'rgb(181, 42, 67)')
  await chart.evaluate((canvas) => {
    canvas.setAttribute('data-theme-retained', 'true')
  })
  const requests: string[] = []
  page.on('request', (entry) => {
    if (/\/api\/statistics\//.test(entry.url())) requests.push(entry.url())
  })
  await chooseTheme(page, '深色')
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await expect(chart).toHaveAttribute('data-theme-retained', 'true')
  expect(
    await Promise.all([amount.textContent(), positive.textContent(), negative.textContent()])
  ).toEqual(values)
  await expect(positive).toHaveCSS('color', 'rgb(116, 207, 167)')
  await expect(negative).toHaveCSS('color', 'rgb(243, 139, 158)')
  await expect(unknown).toHaveText('—')
  await expect.poll(unknownUsesTextColor).toBe(true)
  expect(requests).toEqual([])
})

for (const fontState of ['loaded', 'failed', 'late'] as const) {
  test(`cold chart fonts ${fontState} preserve the first canvas font choice`, async ({
    page,
    request
  }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' })
    const token = await loginThroughApi(request)
    await setAuthenticatedSession(page, token)
    const snapshotResponse = await request.get(
      'http://127.0.0.1:18000/api/statistics/portfolio-snapshot',
      { headers: { Authorization: `Bearer ${token}` } }
    )
    expect(snapshotResponse.ok()).toBeTruthy()
    const snapshot = await snapshotResponse.json()
    await page.route('**/api/statistics/portfolio-snapshot', (route) =>
      route.fulfill({ json: { ...snapshot, markets: [{ market: 'A股', total_cost: 12345 }] } })
    )
    let release!: () => void
    const fonts = new Promise<void>((resolve) => {
      release = resolve
    })
    await page.route('**/*.woff2', async (route) => {
      await fonts
      if (fontState === 'failed') await route.abort()
      else await route.continue()
    })
    await page.addInitScript(() => {
      const draws: { text: string; width: number; font: string }[] = []
      Object.assign(window, { chartFontDraws: draws })
      const fillText = CanvasRenderingContext2D.prototype.fillText
      CanvasRenderingContext2D.prototype.fillText = function (text, x, y, maxWidth) {
        draws.push({ text, width: this.measureText(text).width, font: this.font })
        if (maxWidth === undefined) return fillText.call(this, text, x, y)
        return fillText.call(this, text, x, y, maxWidth)
      }
    })
    try {
      await page.goto('/', { waitUntil: 'domcontentloaded' })
      await expect(page.locator('.dashboard-chart')).toBeVisible()
      await expect(page.locator('.dashboard-chart canvas')).toHaveCount(0)
      if (fontState === 'late')
        await expect(page.locator('.dashboard-chart canvas')).toBeVisible({ timeout: 10000 })
      if (fontState === 'late') {
        await page.locator('.dashboard-chart canvas').evaluate((canvas) => {
          Object.assign(window, { lateFontCanvas: canvas })
        })
      }
      release()
      await page.evaluate(() => document.fonts.ready)
      await expect(page.locator('.dashboard-chart canvas')).toBeVisible()
      const firstNumber = await page.evaluate(() => {
        const draws = (
          window as Window & { chartFontDraws?: { text: string; width: number; font: string }[] }
        ).chartFontDraws
        return draws?.find((draw) => draw.text === '100.0%')
      })
      expect(firstNumber).toBeDefined()
      const finalWidth = await page.locator('.dashboard-chart canvas').evaluate((canvas, font) => {
        // 独立画布也继承图表的页面语言；衬线字体的本地化字形会影响字宽。
        const probe = document.createElement('canvas')
        probe.style.position = 'absolute'
        canvas.parentElement!.append(probe)
        try {
          const context = probe.getContext('2d')!
          context.font = font
          return context.measureText('100.0%').width
        } finally {
          probe.remove()
        }
      }, firstNumber!.font)
      expect(firstNumber!.width).toBeCloseTo(finalWidth, 3)
      if (fontState === 'loaded') expect(firstNumber!.font).toContain('Noto Serif SC Variable')
      else expect(firstNumber!.font).not.toContain('Variable')
      if (fontState === 'late') {
        // 字体迟到后强制触发实际 resize 重绘，避免只验证迟到前的绘字记录。
        const numberDrawCount = () =>
          page.evaluate(() => {
            return (
              (
                window as Window & {
                  chartFontDraws?: { text: string; width: number; font: string }[]
                }
              ).chartFontDraws?.filter((draw) => draw.text === '100.0%').length ?? 0
            )
          })
        const beforeResizeDraws = await numberDrawCount()
        const viewport = page.viewportSize()!
        await page.setViewportSize({ ...viewport, width: viewport.width + 1 })
        await expect.poll(numberDrawCount).toBeGreaterThan(beforeResizeDraws)
        expect(
          await page.locator('.dashboard-chart canvas').evaluate((canvas) => {
            return canvas === (window as Window & { lateFontCanvas?: Element }).lateFontCanvas
          })
        ).toBe(true)
        const numberDraws = await page.evaluate(() => {
          return (
            window as Window & {
              chartFontDraws?: { text: string; width: number; font: string }[]
            }
          ).chartFontDraws?.filter((draw) => draw.text === '100.0%')
        })
        expect(numberDraws!.length).toBeGreaterThan(0)
        for (const draw of numberDraws!) {
          expect(draw.font).toBe(firstNumber!.font)
          expect(draw.font).not.toContain('Variable')
          expect(draw.width).toBeCloseTo(firstNumber!.width, 3)
        }
      }
    } finally {
      release()
    }
  })
}
