import { expect, test, type Locator } from '@playwright/test'
import { loginThroughApi, mockXueqiuCapabilities, setAuthenticatedSession } from './helpers'

async function expectColorRole(locator: Locator, role: string) {
  await expect
    .poll(() =>
      locator.evaluate((node, token) => {
        const probe = document.createElement('span')
        probe.style.color = `var(${token})`
        document.documentElement.append(probe)
        const expected = getComputedStyle(probe).color
        probe.remove()
        return getComputedStyle(node).color === expected
      }, role)
    )
    .toBe(true)
}

test('分析和档案加载时不闪空态、不允许提前生成，先到的档案可独立显示', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  let releaseAnalysis!: () => void
  let releaseProfile!: () => void
  const analysisReady = new Promise<void>((resolve) => (releaseAnalysis = resolve))
  const profileReady = new Promise<void>((resolve) => (releaseProfile = resolve))
  await page.route('**/api/securities/**/analysis', async (route) => {
    await analysisReady
    await route.fulfill({
      json: { symbol: 'LOAD', tags: [], risk_level: 'low', summary: '已加载的分析', content: '' }
    })
  })
  await page.route('**/api/securities/**/profile', async (route) => {
    await profileReady
    await route.fulfill({
      json: { supported: true, business: { profile: { 商业模式: '已加载的商业模式' }, peers: [] } }
    })
  })
  await page.goto('/securities/A股/LOAD')
  await expect(page.getByRole('status', { name: '正在加载AI分析' })).toBeVisible()
  await expect(page.getByRole('status', { name: '正在加载商业画像' })).toBeVisible()
  await expect(page.getByText('暂无 AI 分析', { exact: false })).toHaveCount(0)
  await expect(page.getByTestId('generate-analysis-button')).toBeDisabled()
  releaseProfile()
  await expect(page.getByTestId('business-profile-section')).toContainText('已加载的商业模式')
  await expect(page.getByRole('status', { name: '正在加载AI分析' })).toBeVisible()
  releaseAnalysis()
  await expect(page.getByTestId('analysis-section')).toContainText('已加载的分析')
  await expect(page.getByTestId('generate-analysis-button')).toBeEnabled()
})

test('持仓未知风险保持中性、负面分析为红色，近期转多使用实心观点标签', async ({
  page,
  request
}) => {
  await mockXueqiuCapabilities(page, { opinionHistory: true })
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await page.route('**/api/holdings**', (route) =>
    route.fulfill({
      json: [
        {
          id: 1,
          symbol: 'STATUS',
          market: 'A股',
          name: '状态示例',
          quantity: 100,
          total_cost: 1000,
          avg_cost: 10,
          current_price: 12,
          currency: 'CNY',
          broker_account_id: null
        }
      ]
    })
  )
  await page.route('**/api/securities/analyses', (route) =>
    route.fulfill({
      json: [
        { symbol: 'STATUS', market: 'A股', tags: ['审计非标'], risk_level: '', summary: '测试分析' }
      ]
    })
  )
  await page.route('**/api/securities/opinion-summaries', (route) =>
    route.fulfill({
      json: {
        items: [{ symbol: 'STATUS', market: 'A股', tags: ['近期转多'], summary: '测试观点' }]
      }
    })
  )
  await page.goto('/holdings')
  const analysis = page.getByTestId('ai-tags')
  await expectColorRole(analysis.getByText('风险 未知', { exact: true }), '--app-text-muted')
  await expect(analysis.getByText('审计非标', { exact: true })).toHaveCSS(
    'color',
    'rgb(181, 42, 67)'
  )
  const opinion = page.getByTestId('opinion-tags').locator('.n-tag')
  await expect(opinion).toHaveText('↑ 近期转多')
  await expectColorRole(opinion, '--app-on-primary')
})

test('观察清单有不可判定准则时不显示全部达标色', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await page.route('**/api/watchlist', (route) =>
    route.fulfill({
      json: [
        {
          id: 1,
          symbol: 'PARTIAL',
          market: 'A股',
          name: '部分判定',
          graham_summary: { passed: 3, failed: 0, indeterminate: 4, total: 7 }
        }
      ]
    })
  )
  await page.goto('/watchlist')
  const summary = page.locator('.n-tag').filter({ hasText: '达标 3 / 7' }).first()
  await expectColorRole(summary, '--app-text-muted')
})
