import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { loginThroughApi, mockXueqiuCapabilities, setAuthenticatedSession, user } from './helpers'

const fixture = JSON.parse(
  readFileSync(new URL('../../docs/media/research-reading-fixture.json', import.meta.url), 'utf8')
)

test('读取失败保持未知，独立只读重试区分404且不启动生成任务', async ({ page, request }) => {
  await mockXueqiuCapabilities(page, { configured: true })
  await setAuthenticatedSession(page, await loginThroughApi(request))
  let analysisReads = 0
  let profileReads = 0
  let opinionReads = 0
  const writes: string[] = []
  await page.route('**/api/securities/**', async (route) => {
    const req = route.request()
    if (req.method() !== 'GET') {
      writes.push(req.url())
      return route.abort()
    }
    if (req.url().endsWith('/analysis')) {
      analysisReads++
      return route.fulfill({
        status: analysisReads === 1 ? 503 : 404,
        json: { detail: '虚构分析读取失败' }
      })
    }
    if (req.url().endsWith('/profile')) {
      profileReads++
      return route.fulfill(
        profileReads === 1
          ? { status: 503, json: { detail: '虚构档案读取失败' } }
          : { json: fixture.profile }
      )
    }
    if (req.url().endsWith('/opinion-summary')) {
      opinionReads++
      return route.fulfill({
        status: opinionReads === 1 ? 503 : 404,
        json: { detail: '虚构观点读取失败' }
      })
    }
    return route.continue()
  })
  await page.goto('/securities/A股/UI-RESEARCH')
  await expect(page.getByTestId('analysis-load-error')).toContainText('是否有分析未知')
  await expect(page.getByTestId('profile-load-error')).toContainText('当前内容未知')
  await expect(page.getByText(/暂无 AI 分析；/)).toHaveCount(0)
  await page.getByRole('button', { name: '重试分析', exact: true }).click()
  await expect(page.getByText(/暂无 AI 分析；/)).toBeVisible()
  expect(profileReads).toBe(1)
  await page.getByRole('button', { name: '重试档案', exact: true }).click()
  await expect(page.getByTestId('business-profile-section')).toContainText(
    fixture.profile.business.profile.商业模式
  )
  await page.getByRole('tab', { name: '观点', exact: true }).click()
  const section = page.getByTestId('opinion-section')
  await expect(section).toContainText('虚构观点读取失败')
  await expect(section.getByText(/暂无观点摘要；/)).toHaveCount(0)
  await section.getByRole('button', { name: '重新加载观点' }).click()
  await expect(section.getByText(/暂无观点摘要；/)).toBeVisible()
  expect([analysisReads, profileReads, opinionReads]).toEqual([2, 2, 2])
  expect(writes).toEqual([])
})

test('未认证直达经真实登录返回详情后可返回持仓', async ({ page }) => {
  await page.route('**/api/securities/**/analysis', (route) =>
    route.fulfill({ json: fixture.analysis })
  )
  await page.route('**/api/securities/**/profile', (route) =>
    route.fulfill({ json: fixture.profile })
  )
  await page.goto('/securities/A股/UI-RESEARCH')
  await expect(page).toHaveURL(/\/login\?redirect=/)
  await page.getByPlaceholder('请输入用户名').fill(user.username)
  await page.getByPlaceholder('请输入密码').fill(user.password)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(
    page.getByRole('heading', { name: fixture.analysis.name, exact: true })
  ).toBeVisible()
  await page.getByRole('button', { name: '返回', exact: true }).click()
  await expect(page).toHaveURL(/\/holdings$/)
  await expect(page.getByRole('heading', { name: '当前持仓', exact: true })).toBeVisible()
})

test('窄屏报表保留中报、原币与折元依据，宽表可用键盘滚动', async ({ page, request }) => {
  await page.setViewportSize({ width: 320, height: 852 })
  await setAuthenticatedSession(page, await loginThroughApi(request))
  await page.route('**/api/securities/**/analysis', (route) =>
    route.fulfill({ json: fixture.analysis })
  )
  let profileReads = 0
  let releaseProfile!: () => void
  const refreshed = new Promise<void>((resolve) => (releaseProfile = resolve))
  await page.route('**/api/securities/**/profile', async (route) => {
    profileReads++
    if (profileReads === 2)
      return route.fulfill({ status: 503, json: { detail: '虚构档案刷新失败' } })
    if (profileReads === 3) await refreshed
    return route.fulfill({ json: fixture.hkProfile })
  })
  // 模拟完成任务只返回浏览器fixture；不启动后端任务或外呼模型。
  await page.route('**/UI-RESEARCH/analysis-jobs', (route) =>
    route.fulfill({ json: { id: 'research-fixture', status: 'queued' } })
  )
  await page.route('**/api/securities/analysis-jobs/research-fixture', (route) =>
    route.fulfill({ json: { id: 'research-fixture', status: 'succeeded' } })
  )
  await page
    .context()
    .route('https://example.com/fictional-report.pdf', (route) =>
      route.fulfill({ contentType: 'text/html', body: '<p>明确虚构的原文链接验收</p>' })
    )
  await page.goto('/securities/港股/UI-RESEARCH')
  await page.getByRole('tab', { name: '报表', exact: true }).click()
  const table = page.getByRole('region', { name: '核心科目透视表，可横向滚动' })
  await expect(table).toContainText('PDF+雅虎')
  await expect(table).toContainText('0.0851')
  await expect(table).toContainText('✕')
  await table.focus()
  await page.keyboard.press('ArrowRight')
  await expect.poll(() => table.evaluate((node) => node.scrollLeft)).toBeGreaterThan(0)
  const interim = page.getByRole('switch', { name: '显示中报', exact: true })
  await interim.focus()
  await page.keyboard.press('Space')
  await expect(interim).toHaveAttribute('aria-checked', 'true')
  await expect(table).toContainText('6M')
  const basis = table.locator('summary[aria-label="EPS折元依据"]')
  await basis.focus()
  await page.keyboard.press('Enter')
  await expect(basis.locator('..')).toHaveAttribute('open', '')
  await expect(table).toContainText('原文')
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320)
  const digest = page.getByTestId('report-digest-section')
  const header = digest.getByRole('button', { name: /2025-12-31/ })
  await expect(header).toHaveAttribute('aria-expanded', 'false')
  const source = digest.getByRole('link', { name: '原文', exact: true })
  await source.focus()
  const opened = page.waitForEvent('popup')
  await page.keyboard.press('Enter')
  const popup = await opened
  await popup.waitForURL('https://example.com/fictional-report.pdf')
  await expect(header).toHaveAttribute('aria-expanded', 'false')
  await popup.close()
  await page.getByTestId('generate-analysis-button').click()
  await expect(page.getByTestId('profile-load-error')).toContainText('保留此标的上次成功数据')
  await expect(interim).toHaveAttribute('aria-checked', 'true')
  await page.getByRole('button', { name: '重试档案', exact: true }).click()
  await expect(interim).toHaveAttribute('aria-checked', 'true')
  releaseProfile()
  await expect(page.getByTestId('profile-load-error')).toHaveCount(0)
  await expect(interim).toHaveAttribute('aria-checked', 'true')
  expect(profileReads).toBe(3)
})

test('风险比例未知不拼百分号，真零与股东四位精度保留', async ({ page, request }) => {
  await setAuthenticatedSession(page, await loginThroughApi(request))
  let value: number | null = null
  await page.route('**/api/securities/**/analysis', (route) =>
    route.fulfill({ json: fixture.analysis })
  )
  await page.route('**/api/securities/**/profile', (route) => {
    const profile = structuredClone(fixture.profile)
    profile.datasets.pledge_stat = [{ end_date: '20251231', pledge_ratio: value, pledge_count: 2 }]
    profile.datasets.stk_holdertrade = [
      {
        ann_date: '20260101',
        holder_name: '明确虚构股东',
        in_de: 'IN',
        change_vol: 100,
        change_ratio: value
      }
    ]
    profile.events = [
      {
        id: 999,
        event_type: 'SHARE_UNLOCK',
        event_date: '2026-10-03',
        payload: { float_share: 100000, float_ratio_pct: value }
      }
    ]
    return route.fulfill({ json: profile })
  })
  for (const next of [null, 0, 0.1234]) {
    value = next
    await page.goto('/securities/A股/UI-RESEARCH')
    await page.getByRole('tab', { name: '基本面', exact: true }).click()
    const main = page.getByRole('main')
    await expect(main).toContainText('明确虚构股东')
    await expect(main).not.toContainText('—%')
    await expect(main).toContainText(
      `质押比例 ${next === null ? '—' : next === 0 ? '0.00%' : '0.12%'}`
    )
    await expect(main).toContainText(
      `股（占比 ${next === null ? '—' : next === 0 ? '0.00%' : '0.1234%'}）`
    )
    await expect(main).toContainText(
      `占总股本 ${next === null ? '—' : next === 0 ? '0.00%' : '0.12%'}`
    )
  }
})

for (const width of [320, 1440]) {
  test(`研究脆弱性比率统一舍入并保留未知、零与净现金说明（${width}px）`, async ({
    page,
    request
  }) => {
    await page.setViewportSize({ width, height: 852 })
    await setAuthenticatedSession(page, await loginThroughApi(request))
    await page.route('**/api/securities/**/analysis', (route) =>
      route.fulfill({ json: fixture.analysis })
    )
    let value: number | null = null
    await page.route('**/api/securities/**/profile', (route) => {
      const profile = structuredClone(fixture.profile)
      profile.graham_screen.fragility = {
        debt_to_assets: value,
        net_debt_to_assets: value === null || value === 0 ? value : -value,
        net_cash_to_market_cap: value,
        interest_coverage: 1.25,
        interest_coverage_note: '明确虚构利息覆盖说明'
      }
      return route.fulfill({ json: profile })
    })
    for (const next of [null, 0, 0.0255]) {
      value = next
      await page.goto('/securities/A股/UI-RESEARCH')
      const main = page.getByRole('main')
      await expect(main).toContainText('明确虚构利息覆盖说明')
      await expect(main).toContainText('利息覆盖 1.3 倍')
      if (next === null) {
        await expect(main).not.toContainText('总负债率')
        await expect(main).not.toContainText('净债务/总资产')
        await expect(main).not.toContainText('净现金/市值')
      } else {
        await expect(main).toContainText(`总负债率 ${next === 0 ? '0.0%' : '2.6%'}`)
        await expect(main).toContainText(`净债务/总资产 ${next === 0 ? '0.0%' : '-2.6%（净现金）'}`)
        await expect(main).toContainText(`净现金/市值 ${next === 0 ? '0.0%' : '2.6%'}`)
      }
      await expect(main).not.toContainText('—%')
      await expect(main).not.toContainText('总负债率 +')
    }
  })
}
