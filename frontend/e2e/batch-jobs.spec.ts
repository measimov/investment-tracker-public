import { expect, test, type Page } from '@playwright/test'
import {
  loginThroughApi,
  setAuthenticatedSession,
  BATCH_HOLDINGS,
  mockHoldingsPage
} from './helpers'

// 后端目标预览：已排除清仓/EXCLUDE/现金管理标的，前端确认框必须用这个数
async function mockBatchTargets(page: Page, total: number) {
  await page.route('**/api/securities/analysis-batch-targets', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        total,
        targets: Array.from({ length: total }, (_, index) => ({
          symbol: `T${index}`,
          market: 'A股'
        }))
      })
    })
  )
}

test('one-click batch analysis asks for confirmation and shows progress', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await mockHoldingsPage(page)
  // [评审回归] 后端排除了现金管理标的：持仓有 2 只 A股，但真实目标只有 1 只
  await mockBatchTargets(page, 1)

  let analysesCall = 0
  await page.route('**/api/securities/analyses', (route) => {
    analysesCall += 1
    // 第一次没有分析，任务推进后返回一条（标签列应随之亮起）
    const body =
      analysesCall <= 1
        ? []
        : [
            {
              symbol: 'BAT001',
              market: 'A股',
              tags: ['业绩增长'],
              risk_level: 'low',
              summary: 'B 摘要'
            }
          ]
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body)
    })
  })
  await page.route('**/api/securities/active-analysis-jobs', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )

  let startCalls = 0
  await page.route('**/api/securities/analysis-batch-jobs', (route) => {
    startCalls += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'batch-1',
        type: 'security_analysis_batch',
        status: 'queued',
        total: 2,
        completed: 0,
        progress_percent: 0
      })
    })
  })

  const script = [
    {
      status: 'running',
      completed: 1,
      progress_percent: 50,
      success_count: 1,
      failed_count: 0,
      skipped_count: 0,
      current_symbol: 'BAT002',
      current_market: 'A股',
      current_stage: '生成分析（LLM）',
      results: [{ symbol: 'BAT001', market: 'A股', status: 'succeeded' }]
    },
    {
      status: 'succeeded',
      completed: 2,
      progress_percent: 100,
      success_count: 2,
      failed_count: 0,
      skipped_count: 0,
      current_symbol: null,
      current_market: null,
      results: [
        { symbol: 'BAT001', market: 'A股', status: 'succeeded' },
        { symbol: 'BAT002', market: 'A股', status: 'succeeded' }
      ]
    }
  ]
  let tick = 0
  await page.route('**/api/securities/analysis-batch-jobs/batch-1', (route) => {
    const payload = script[Math.min(tick, script.length - 1)]
    tick += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ id: 'batch-1', total: 2, ...payload })
    })
  })

  await page.goto('/holdings')
  await expect(page.getByTestId('analyze-all-button')).toBeVisible()

  // 二次确认必须给出数量与耗时量级；取消则不发请求
  await page.getByTestId('analyze-all-button').click()
  const dialog = page.locator('.el-message-box')
  // 数量来自后端预览（1 只），不是前端按市场本地估算的 2 只
  await expect(dialog).toContainText('1 只')
  await expect(dialog).toContainText('预计耗时')
  await expect(dialog).toContainText('自动跳过')
  await dialog.getByRole('button', { name: '取消' }).click()
  expect(startCalls).toBe(0)

  await page.getByTestId('analyze-all-button').click()
  await page.locator('.el-message-box').getByRole('button', { name: '开始分析' }).click()
  // 确认后到 POST 之间隔着预览 resolve 与 axios 往返，用 poll 而不是即刻断言
  await expect.poll(() => startCalls, { timeout: 15000 }).toBe(1)

  const progress = page.getByTestId('batch-analysis-progress')
  await expect(progress).toContainText('1/2', { timeout: 15000 })
  // [评审回归] 启动后按钮只禁用、不转圈（轮询要跑数十分钟）
  await expect(page.getByTestId('analyze-all-button')).toBeDisabled()
  await expect(page.getByTestId('analyze-all-button').locator('.is-loading')).toHaveCount(0)
  await expect(progress).toContainText('BAT002 A股')
  await expect(progress).toContainText('成功 1 · 跳过 0 · 失败 0')
  await expect(page.getByText('批量分析完成：成功 2 只')).toBeVisible({ timeout: 15000 })
  // 完成后标签列刷新
  await expect(page.getByTestId('ai-tags').first()).toBeVisible()
})

test('batch analysis progress is restored when returning to the holdings page', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await mockHoldingsPage(page)
  await mockBatchTargets(page, 5)
  await page.route('**/api/securities/analyses', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )
  await page.route('**/api/securities/active-analysis-jobs', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        {
          id: 'batch-live',
          type: 'security_analysis_batch',
          status: 'running',
          total: 5,
          completed: 2,
          progress_percent: 40,
          success_count: 2,
          failed_count: 0,
          skipped_count: 0,
          current_symbol: 'BAT002',
          current_market: 'A股'
        }
      ])
    })
  )
  await page.route('**/api/securities/analysis-batch-jobs/batch-live', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'batch-live',
        status: 'running',
        total: 5,
        completed: 2,
        progress_percent: 40,
        success_count: 2,
        failed_count: 0,
        skipped_count: 0,
        current_symbol: 'BAT002',
        current_market: 'A股'
      })
    })
  )

  // 不点任何按钮，直接进入页面即应接上进行中的任务
  await page.goto('/holdings')
  const progress = page.getByTestId('batch-analysis-progress')
  await expect(progress).toBeVisible({ timeout: 15000 })
  await expect(progress).toContainText('2/5')
  // 任务活跃时按钮禁用（避免重复发起）
  await expect(page.getByTestId('analyze-all-button')).toBeDisabled()
})

test('digest backfill button previews gaps and shows generated counts', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await mockHoldingsPage(page)
  await mockBatchTargets(page, 2)
  await page.route('**/api/securities/analyses', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )
  await page.route('**/api/securities/active-analysis-jobs', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )
  await page.route('**/api/securities/digest-backfill-preview', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        targets_total: 2,
        targets_without_digest: 1,
        digests_existing: 4,
        per_symbol_budget: 4
      })
    })
  )
  let startCalls = 0
  await page.route('**/api/securities/digest-backfill-jobs', (route) => {
    startCalls += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'digest-1',
        type: 'report_digest_batch',
        status: 'queued',
        total: 2,
        completed: 0,
        progress_percent: 0
      })
    })
  })
  const script = [
    {
      status: 'running',
      completed: 1,
      progress_percent: 50,
      success_count: 1,
      failed_count: 0,
      digests_generated: 4,
      symbols_with_remaining: 1,
      current_symbol: 'BAT002',
      current_market: 'A股'
    },
    {
      status: 'succeeded',
      completed: 2,
      progress_percent: 100,
      success_count: 2,
      failed_count: 0,
      digests_generated: 7,
      symbols_with_remaining: 1,
      current_symbol: null,
      current_market: null
    }
  ]
  let tick = 0
  await page.route('**/api/securities/digest-backfill-jobs/digest-1', (route) => {
    const payload = script[Math.min(tick, script.length - 1)]
    tick += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ id: 'digest-1', total: 2, ...payload })
    })
  })

  await page.goto('/holdings')
  const button = page.getByTestId('digest-backfill-button')
  await expect(button).toBeVisible()

  // 确认框数字来自纯 DB 预览；取消不发请求
  await button.click()
  const dialog = page.locator('.el-message-box')
  await expect(dialog).toContainText('2 只持仓标的')
  await expect(dialog).toContainText('1 只目前一份摘要都没有')
  await expect(dialog).toContainText('最多补 4 份')
  await dialog.getByRole('button', { name: '取消' }).click()
  expect(startCalls).toBe(0)

  await button.click()
  await page.locator('.el-message-box').getByRole('button', { name: '开始回填' }).click()
  await expect.poll(() => startCalls, { timeout: 15000 }).toBe(1)

  const progress = page.getByTestId('digest-backfill-progress')
  await expect(progress).toContainText('1/2', { timeout: 15000 })
  await expect(progress).toContainText('已生成摘要 4 份')
  // 回填活跃时一键分析禁用（互斥，后端 409 兜底、前端体验先行）
  await expect(page.getByTestId('analyze-all-button')).toBeDisabled()
  // 完成提示要说清"还有更早年份可续跑"——这是加深十年的唯一入口
  await expect(page.getByText(/新生成 7 份；1 只标的还有更早年份可补/)).toBeVisible({
    timeout: 15000
  })
})

test('stopping the batch view halts polling but says the job keeps running', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await mockHoldingsPage(page)
  await page.route('**/api/securities/analyses', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )
  await page.route('**/api/securities/active-analysis-jobs', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        {
          id: 'batch-stop',
          type: 'security_analysis_batch',
          status: 'running',
          total: 9,
          completed: 1
        }
      ])
    })
  )
  let polls = 0
  await page.route('**/api/securities/analysis-batch-jobs/batch-stop', (route) => {
    polls += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'batch-stop',
        status: 'running',
        total: 9,
        completed: 1,
        progress_percent: 11
      })
    })
  })

  await page.goto('/holdings')
  const progress = page.getByTestId('batch-analysis-progress')
  await expect(progress).toBeVisible({ timeout: 15000 })
  // 文案必须诚实：这不是"取消"
  await expect(progress).toContainText('后台任务会继续运行')
  await expect(progress).toContainText('继续消耗 token')

  await page.getByTestId('stop-watching-batch').click()
  await expect(progress).toHaveCount(0)
  const pollsAfterStop = polls
  await page.waitForTimeout(6000) // 跨过一个轮询间隔
  expect(polls).toBe(pollsAfterStop)
})

test('the batch button is disabled when no holding is analyzable', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await mockHoldingsPage(page, [BATCH_HOLDINGS[2]]) // 只有加密货币
  await mockBatchTargets(page, 0)
  await page.route('**/api/securities/analyses', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )
  await page.route('**/api/securities/active-analysis-jobs', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )

  await page.goto('/holdings')
  await expect(page.getByTestId('analyze-all-button')).toBeDisabled()
})

test('the confirm dialog waits for the server target preview', async ({ page, request }) => {
  // [评审回归] 预览在途期间不得用本地估算弹确认框：batchTargetCount 初值为 null
  // 时若直接回退本地数，用户在预览慢时点按钮就会看到错误的目标数与 token 预期。
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await mockHoldingsPage(page) // 本地可见 2 只 A股
  await page.route('**/api/securities/analyses', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )
  await page.route('**/api/securities/active-analysis-jobs', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )

  let releasePreview: (() => void) | null = null
  const previewReleased = new Promise<void>((resolve) => {
    releasePreview = resolve
  })
  await page.route('**/api/securities/analysis-batch-targets', async (route) => {
    await previewReleased
    // 服务端真实目标只有 1 只（另一只被现金管理规则排除）
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ total: 1, targets: [{ symbol: 'BAT001', market: 'A股' }] })
    })
  })

  await page.goto('/holdings')
  await expect(page.getByTestId('analyze-all-button')).toBeVisible()

  // 预览仍挂起：点击后不得出现任何确认框（更不能显示本地估算的 2 只）
  await page.getByTestId('analyze-all-button').click()
  await page.waitForTimeout(1000)
  await expect(page.locator('.el-message-box')).toHaveCount(0)

  // 放行预览：确认框出现且用服务端数字
  releasePreview!()
  const dialog = page.locator('.el-message-box')
  await expect(dialog).toBeVisible({ timeout: 15000 })
  await expect(dialog).toContainText('1 只')
  await expect(dialog).not.toContainText('2 只')
})

test('a preview that resolves to zero explains instead of opening the confirm dialog', async ({
  page,
  request
}) => {
  // 预览已 ready 且为 0 时按钮本来就是禁用的；这里覆盖的是另一条路径——
  // 预览**在途时**按钮按本地估算可点，点下去后预览返回 0，必须如实告知而
  // 不是弹一个"将对 2 只标的"的确认框。
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await mockHoldingsPage(page) // 本地看起来有 2 只可分析
  await page.route('**/api/securities/analyses', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )
  await page.route('**/api/securities/active-analysis-jobs', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )

  let releasePreview: (() => void) | null = null
  const previewReleased = new Promise<void>((resolve) => {
    releasePreview = resolve
  })
  await page.route('**/api/securities/analysis-batch-targets', async (route) => {
    await previewReleased
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ total: 0, targets: [] }) // 后端排除后一只不剩
    })
  })

  await page.goto('/holdings')
  await page.getByTestId('analyze-all-button').click()
  releasePreview!()

  await expect(page.getByText('当前没有可分析的持仓标的', { exact: false })).toBeVisible({
    timeout: 15000
  })
  await expect(page.locator('.el-message-box')).toHaveCount(0)
})

// ---------------------------------------------------------------------------
// #142 核心 CRUD 盲区回归：汇率增删改、交易编辑/删除、统计页价格 what-if
// ---------------------------------------------------------------------------
