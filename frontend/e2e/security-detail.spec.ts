import { expect, test } from '@playwright/test'
import { loginThroughApi, setAuthenticatedSession, settleAfter } from './helpers'

test('slow responses from a previous security never leak into the current one', async ({
  page,
  request
}) => {
  // [评审回归] 同业跳转复用同一路由组件实例：A 的 analysis 响应晚于 B 返回时，
  // 若没有请求身份守卫，A 的分析会写进 B 的页面（标题是 B、内容是 A）。
  // 必须走点击同业的客户端路由——整页 goto 会被浏览器中止在途请求，复现不了。
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  let releaseSlowA: (() => void) | null = null
  const slowAReleased = new Promise<void>((resolve) => {
    releaseSlowA = resolve
  })

  const profileBody = (symbol: string) => ({
    symbol,
    market: 'A股',
    supported: true,
    capabilities: { structured: true, report_digest: true, risk_signals: true },
    datasets: {},
    latest_periods: {},
    events: [],
    report_digests: [],
    digest_progress: { digested: 0, failed_capped: 0 },
    statement_progress: null,
    business: {
      profile: {
        商业模式: `${symbol} 的商业模式说明`,
        业务分部: [],
        上游依赖: [],
        下游需求: [],
        供应商集中度: '未披露',
        客户集中度: '未披露',
        行业与竞争: '—',
        估值观察因子: []
      },
      peers: [{ symbol: 'RACEB', name: '同业标的B', industry: '银行' }],
      industry: '银行'
    },
    earnings_quality: { status: 'no_data' }
  })

  // A 的档案立刻返回（页面渲染出同业链接），A 的分析挂起
  await page.route('**/api/securities/**/profile', async (route) => {
    const isA = route.request().url().includes('RACEA')
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(profileBody(isA ? 'RACEA' : 'RACEB'))
    })
  })

  await page.route('**/api/securities/**/analysis', async (route) => {
    const isA = route.request().url().includes('RACEA')
    if (isA) await slowAReleased
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: isA ? 1 : 2,
        symbol: isA ? 'RACEA' : 'RACEB',
        market: 'A股',
        name: isA ? '标的A' : '标的B',
        tags: [isA ? '业绩下滑' : '业绩增长'],
        risk_level: isA ? 'high' : 'low',
        summary: isA ? 'A的摘要不得出现在B页面' : 'B的摘要',
        content: isA ? '## 财务质量趋势\nA的全文正文' : '## 财务质量趋势\nB的全文正文',
        model: 'deepseek-v4-pro'
      })
    })
  })

  await page.goto('/securities/A股/RACEA')
  await expect(page.getByTestId('peer-list')).toContainText('同业标的B')

  // 点击同业 → 客户端路由切到 B；B 的两个请求都正常返回
  await page.getByTestId('peer-list').getByText('同业标的B').click()
  await expect(page).toHaveURL(/RACEB/)
  await expect(page.getByText('B的摘要')).toBeVisible()
  await expect(page.getByTestId('business-profile-section')).toContainText('RACEB 的商业模式')

  // 放行 A 的迟到响应：不得覆盖 B 的页面
  const lateA = page.waitForResponse((r) => r.url().includes('RACEA/analysis'))
  releaseSlowA!()
  await settleAfter(page, lateA)

  await expect(page).toHaveURL(/RACEB/)
  await expect(page.getByText('B的摘要')).toBeVisible()
  await expect(page.getByText('A的摘要不得出现在B页面')).toHaveCount(0)
  await expect(page.locator('body')).not.toContainText('A的全文正文')
  await expect(page.getByTestId('risk-level-tag')).toContainText('低')
})

test('graham criteria card is cleared on peer navigation while the new profile is pending', async ({
  page,
  request
}) => {
  // [评审 P2 回归] 同业跳转复用组件实例：路由切到 B 后、B 的 profile 返回前，
  // A 的格雷厄姆准则卡不得继续显示在 B 的标题下；B 的 profile 失败时也不得残留。
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  let releaseB: (() => void) | null = null
  const bReleased = new Promise<void>((resolve) => {
    releaseB = resolve
  })

  const profileBody = (symbol: string, withGraham: boolean) => ({
    symbol,
    market: 'A股',
    supported: true,
    capabilities: { structured: true, report_digest: true, risk_signals: true },
    datasets: {},
    latest_periods: {},
    events: [],
    report_digests: [],
    digest_progress: { digested: 0, failed_capped: 0 },
    statement_progress: null,
    business: {
      profile: null,
      peers: [{ symbol: 'GRMB', name: '同业标的B', industry: '银行' }],
      industry: '银行'
    },
    earnings_quality: { status: 'no_data' },
    graham_screen: withGraham
      ? {
          status: 'ok',
          as_of_year: '2025',
          passed: 1,
          failed: 0,
          indeterminate: 0,
          criteria: [
            { criterion: 'pe', verdict: 'pass', reason: 'A 的准则依据不得残留', value: 9 }
          ],
          fragility: {}
        }
      : { status: 'no_data' }
  })

  await page.route('**/api/securities/**/profile', async (route) => {
    const isA = route.request().url().includes('GRMA')
    if (!isA) {
      await bReleased
      // B 的 profile 失败：残留检查在失败路径同样成立
      await route.fulfill({ status: 500, contentType: 'application/json', body: '{}' })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(profileBody('GRMA', true))
    })
  })
  await page.route('**/api/securities/**/analysis', async (route) => {
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })

  await page.goto('/securities/A股/GRMA')
  await expect(page.getByTestId('graham-screen-section')).toContainText('A 的准则依据不得残留')

  await page.getByTestId('peer-list').getByText('同业标的B').click()
  await expect(page).toHaveURL(/GRMB/)
  // B 的 profile 仍挂起：A 的准则卡必须已经清空
  await expect(page.getByTestId('graham-screen-section')).toHaveCount(0)

  const lateB = page.waitForResponse((r) => r.url().includes('GRMB/profile'))
  releaseB!()
  await settleAfter(page, lateB)
  await expect(page).toHaveURL(/GRMB/)
  await expect(page.getByTestId('graham-screen-section')).toHaveCount(0)
})

test('watch state ignores an out-of-order response after peer navigation', async ({
  page,
  request
}) => {
  // [评审 P2 回归] A 的 membership 响应（watching=true）挂起 → 点同业跳 B
  // （B 返回 not-watching）→ 放行 A 的迟到响应：不得把 B 页面标成"已在观察清单"。
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  let releaseA: (() => void) | null = null
  const aReleased = new Promise<void>((resolve) => {
    releaseA = resolve
  })

  const profileBody = (symbol: string) => ({
    symbol,
    market: 'A股',
    supported: true,
    capabilities: { structured: true, report_digest: true, risk_signals: true },
    datasets: {},
    latest_periods: {},
    events: [],
    report_digests: [],
    digest_progress: { digested: 0, failed_capped: 0 },
    statement_progress: null,
    business: {
      profile: null,
      peers: [{ symbol: 'WSB', name: '同业标的B', industry: '银行' }],
      industry: '银行'
    },
    earnings_quality: { status: 'no_data' },
    graham_screen: { status: 'no_data' }
  })
  await page.route('**/api/securities/**/profile', async (route) => {
    const isA = route.request().url().includes('WSA')
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(profileBody(isA ? 'WSA' : 'WSB'))
    })
  })
  await page.route('**/api/securities/**/analysis', async (route) => {
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })
  await page.route('**/api/watchlist/contains**', async (route) => {
    const isA = route.request().url().includes('WSA')
    if (isA) await aReleased
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ watching: isA, item_id: isA ? 1 : null })
    })
  })

  await page.goto('/securities/A股/WSA')
  await expect(page.getByTestId('peer-list')).toContainText('同业标的B')
  await page.getByTestId('peer-list').getByText('同业标的B').click()
  await expect(page).toHaveURL(/WSB/)
  await expect(page.getByTestId('add-to-watchlist-button')).toBeVisible()

  const lateA = page.waitForResponse(
    (r) => r.url().includes('/watchlist/contains') && r.url().includes('WSA')
  )
  releaseA!()
  await settleAfter(page, lateA)
  await expect(page).toHaveURL(/WSB/)
  await expect(page.getByTestId('add-to-watchlist-button')).toBeVisible()
  await expect(page.getByText('已在观察清单', { exact: true })).toHaveCount(0)
})

test('confirming the watch prompt after navigating away does not add the wrong security', async ({
  page,
  request
}) => {
  // [评审 P2 二轮回归] 在 A 页打开「加入观察」prompt → 浏览器 back 到同组件的 B 页
  // → 再点确认：不得把为 A 输入的理由加到 B（也不得给 A 加）。
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  const profileBody = (symbol: string) => ({
    symbol,
    market: 'A股',
    supported: true,
    capabilities: { structured: true, report_digest: true, risk_signals: true },
    datasets: {},
    latest_periods: {},
    events: [],
    report_digests: [],
    digest_progress: { digested: 0, failed_capped: 0 },
    statement_progress: null,
    business: {
      profile: null,
      // B 页挂 A 为同业：用客户端路由跳 A，history 里 B→A 都在同一 SPA 会话内，
      // 之后 history.back() 才是客户端回退（page.goBack 跨硬导航会整页重载、弹窗消失）
      peers: symbol === 'PRB' ? [{ symbol: 'PRA', name: '同业标的A', industry: '银行' }] : [],
      industry: '银行'
    },
    earnings_quality: { status: 'no_data' },
    graham_screen: { status: 'no_data' }
  })
  await page.route('**/api/securities/**/profile', async (route) => {
    const symbol = route.request().url().includes('PRA') ? 'PRA' : 'PRB'
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(profileBody(symbol))
    })
  })
  await page.route('**/api/securities/**/analysis', async (route) => {
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })
  await page.route('**/api/watchlist/contains**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ watching: false, item_id: null })
    })
  })
  const posted: string[] = []
  await page.route('**/api/watchlist', async (route) => {
    if (route.request().method() === 'POST') {
      posted.push(route.request().postDataJSON().symbol)
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ id: 1, symbol: 'X', market: 'A股' })
      })
      return
    }
    await route.continue()
  })

  // 先到 B，再经同业链接客户端跳到 A（同组件复用、同一 SPA history）
  await page.goto('/securities/A股/PRB')
  await expect(page.getByTestId('peer-list')).toContainText('同业标的A')
  await page.getByTestId('peer-list').getByText('同业标的A').click()
  await expect(page).toHaveURL(/PRA/)
  await expect(page.getByTestId('add-to-watchlist-button')).toBeVisible()

  await page.getByTestId('add-to-watchlist-button').click()
  const promptBox = page.locator('.el-message-box', { hasText: '加入观察' })
  await promptBox.locator('textarea').fill('为 A 写的理由')

  // 客户端 history 回退到 B：弹窗独立于路由仍挂着
  await page.evaluate(() => window.history.back())
  await expect(page).toHaveURL(/PRB/)
  await expect(promptBox).toBeVisible()
  // 弹窗仍挂着（ElMessageBox 独立于路由）：此时确认
  await promptBox.getByRole('button', { name: '加入' }).click()
  await page.waitForTimeout(500)

  expect(posted).toEqual([]) // 既不加 B，也不代 A 加
  await expect(page.getByText('已在观察清单', { exact: true })).toHaveCount(0)
})

test('an older request for the same security cannot overwrite a newer one (ABA)', async ({
  page,
  request
}) => {
  // [评审回归] 请求身份不能只用 market/symbol：A₁（慢）→ 切 B → 切回 A 发起
  // A₂（快），A₂ 渲染后 A₁ 才到，业务 key 又相等，旧 A₁ 会覆盖更新的 A₂。
  // 守卫必须是单调递增的请求代次。
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  let releaseA1: (() => void) | null = null
  const a1Released = new Promise<void>((resolve) => {
    releaseA1 = resolve
  })
  let aRequestCount = 0

  const profileBody = (symbol: string, peer: string) => ({
    symbol,
    market: 'A股',
    supported: true,
    capabilities: { structured: true, report_digest: true, risk_signals: true },
    datasets: {},
    latest_periods: {},
    events: [],
    report_digests: [],
    digest_progress: { digested: 0, failed_capped: 0 },
    statement_progress: null,
    business: {
      profile: {
        商业模式: `${symbol} 的商业模式说明`,
        业务分部: [],
        上游依赖: [],
        下游需求: [],
        供应商集中度: '未披露',
        客户集中度: '未披露',
        行业与竞争: '—',
        估值观察因子: []
      },
      peers: [{ symbol: peer, name: `同业${peer}`, industry: '银行' }],
      industry: '银行'
    },
    earnings_quality: { status: 'no_data' }
  })

  await page.route('**/api/securities/**/profile', async (route) => {
    const isA = route.request().url().includes('ABAA')
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(isA ? profileBody('ABAA', 'ABAB') : profileBody('ABAB', 'ABAA'))
    })
  })

  await page.route('**/api/securities/**/analysis', async (route) => {
    const isA = route.request().url().includes('ABAA')
    if (!isA) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 2,
          symbol: 'ABAB',
          market: 'A股',
          name: '标的B',
          tags: ['业绩增长'],
          risk_level: 'medium',
          summary: 'B的摘要',
          content: '## 财务质量趋势\nB的全文',
          model: 'deepseek-v4-pro'
        })
      })
      return
    }
    aRequestCount += 1
    const generation = aRequestCount
    if (generation === 1) await a1Released // A₁ 挂起，A₂ 立即返回
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 1,
        symbol: 'ABAA',
        market: 'A股',
        name: '标的A',
        tags: [generation === 1 ? '业绩下滑' : '业绩增长'],
        risk_level: generation === 1 ? 'high' : 'low',
        summary: generation === 1 ? 'A第一代陈旧摘要' : 'A第二代最新摘要',
        content: `## 财务质量趋势\nA第${generation}代全文`,
        model: 'deepseek-v4-pro'
      })
    })
  })

  // A₁ 发起并挂起
  await page.goto('/securities/A股/ABAA')
  await expect(page.getByTestId('peer-list')).toContainText('同业ABAB')

  // 切到 B（客户端路由），B 正常返回
  await page.getByTestId('peer-list').getByText('同业ABAB').click()
  await expect(page).toHaveURL(/ABAB/)
  await expect(page.getByText('B的摘要')).toBeVisible()

  // 切回 A，发起 A₂ 并立即完成
  await page.getByTestId('peer-list').getByText('同业ABAA').click()
  await expect(page).toHaveURL(/ABAA/)
  await expect(page.getByText('A第二代最新摘要')).toBeVisible()

  // 放行 A₁：同一标的的旧代次请求不得覆盖 A₂
  const lateA1 = page.waitForResponse((r) => r.url().includes('ABAA/analysis'))
  releaseA1!()
  await settleAfter(page, lateA1)

  await expect(page.getByText('A第二代最新摘要')).toBeVisible()
  await expect(page.getByText('A第一代陈旧摘要')).toHaveCount(0)
  await expect(page.locator('body')).not.toContainText('A第1代全文')
  await expect(page.getByTestId('risk-level-tag')).toContainText('低')
})

test('shows staged progress while generating an AI analysis', async ({ page, request }) => {
  // 进度全部走 route mock，不触发真实 LLM
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  const profileBody = {
    symbol: 'PROG01',
    market: 'A股',
    supported: true,
    capabilities: { structured: true, report_digest: true, risk_signals: true },
    datasets: {},
    latest_periods: {},
    events: [],
    report_digests: [],
    digest_progress: { digested: 0, failed_capped: 0 },
    statement_progress: null,
    business: { profile: null, peers: [], industry: null },
    earnings_quality: { status: 'no_data' }
  }
  await page.route('**/api/securities/**/profile', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(profileBody)
    })
  )
  await page.route('**/api/securities/**/analysis', (route) =>
    route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"无"}' })
  )
  await page.route('**/PROG01/analysis-jobs', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'job-prog',
        status: 'queued',
        stage_label: '排队中',
        total: 6,
        completed: 0,
        progress_percent: 0
      })
    })
  )

  // 阶段脚本：同步基本面 → 生成分析（LLM）→ 成功
  const script = [
    { status: 'running', stage_label: '同步基本面档案', completed: 1, progress_percent: 16.67 },
    { status: 'running', stage_label: '生成分析（LLM）', completed: 5, progress_percent: 83.33 },
    { status: 'succeeded', stage_label: '已完成', completed: 6, progress_percent: 100 }
  ]
  let tick = 0
  await page.route('**/api/securities/analysis-jobs/job-prog', (route) => {
    const payload = script[Math.min(tick, script.length - 1)]
    tick += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ id: 'job-prog', total: 6, ...payload })
    })
  })

  await page.goto('/securities/A股/PROG01')
  await page.getByTestId('generate-analysis-button').click()

  const progress = page.getByTestId('analysis-progress')
  await expect(progress).toContainText('同步基本面档案', { timeout: 15000 })
  await expect(progress).toContainText('1/6')
  await expect(progress).toContainText('生成分析（LLM）', { timeout: 15000 })
  // 成功后进度块收起（分析正文本身即完成证据）
  await expect(page.getByText('分析已生成')).toBeVisible({ timeout: 15000 })
  await expect(progress).toHaveCount(0)
})

test('a failed analysis keeps the progress block with its error', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  await page.route('**/api/securities/**/profile', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        symbol: 'FAIL01',
        market: 'A股',
        supported: true,
        capabilities: { structured: true, report_digest: true, risk_signals: true },
        datasets: {},
        latest_periods: {},
        events: [],
        report_digests: [],
        digest_progress: { digested: 0, failed_capped: 0 },
        statement_progress: null,
        business: { profile: null, peers: [], industry: null },
        earnings_quality: { status: 'no_data' }
      })
    })
  )
  await page.route('**/api/securities/**/analysis', (route) =>
    route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"无"}' })
  )
  await page.route('**/FAIL01/analysis-jobs', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ id: 'job-fail', status: 'queued', total: 6, completed: 0 })
    })
  )
  await page.route('**/api/securities/analysis-jobs/job-fail', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'job-fail',
        status: 'failed',
        stage_label: '生成分析（LLM）',
        total: 6,
        completed: 5,
        progress_percent: 83.33,
        error: 'LLM 输出解析失败：tags 含白名单外标签'
      })
    })
  )

  await page.goto('/securities/A股/FAIL01')
  await page.getByTestId('generate-analysis-button').click()

  const progress = page.getByTestId('analysis-progress')
  await expect(progress).toBeVisible({ timeout: 15000 })
  await expect(progress).toContainText('LLM 输出解析失败')
  await expect(progress.locator('.el-progress.is-exception')).toHaveCount(1)
  // 按钮恢复可用（不再 loading）
  await expect(page.getByTestId('generate-analysis-button')).toBeEnabled()
})

test('analysis progress of a previous security never leaks into the current one', async ({
  page,
  request
}) => {
  // [评审回归] pollJobUntilDone 的 onUpdate 是每轮无条件调用的（取消检查在
  // fetch 之前），所以 onUpdate 内部必须自己判代次。泄漏窗口只在"响应已在
  // 途中时发生了导航"，因此这里必须挂起 A 的轮询响应、切到 B 之后再放行。
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  let releaseA: (() => void) | null = null
  const aReleased = new Promise<void>((resolve) => {
    releaseA = resolve
  })

  const profileBody = (symbol: string, peer: string) => ({
    symbol,
    market: 'A股',
    supported: true,
    capabilities: { structured: true, report_digest: true, risk_signals: true },
    datasets: {},
    latest_periods: {},
    events: [],
    report_digests: [],
    digest_progress: { digested: 0, failed_capped: 0 },
    statement_progress: null,
    business: {
      profile: null,
      peers: [{ symbol: peer, name: `同业${peer}`, industry: '银行' }],
      industry: '银行'
    },
    earnings_quality: { status: 'no_data' }
  })
  await page.route('**/api/securities/**/profile', (route) => {
    const isA = route.request().url().includes('LEAKA')
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(isA ? profileBody('LEAKA', 'LEAKB') : profileBody('LEAKB', 'LEAKA'))
    })
  })
  await page.route('**/api/securities/**/analysis', (route) =>
    route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"无"}' })
  )
  await page.route('**/LEAKA/analysis-jobs', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ id: 'job-leak', status: 'queued', total: 6, completed: 0 })
    })
  )
  // A 的第一次轮询响应挂起，直到 B 已经渲染完成才放行
  await page.route('**/api/securities/analysis-jobs/job-leak', async (route) => {
    await aReleased
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'job-leak',
        status: 'running',
        stage_label: 'A标的的分析阶段',
        total: 6,
        completed: 3,
        progress_percent: 50
      })
    })
  })

  await page.goto('/securities/A股/LEAKA')
  await page.getByTestId('generate-analysis-button').click()
  // 启动响应已渲染出「排队中」，此时第一次轮询正挂起
  await expect(page.getByTestId('analysis-progress')).toContainText('排队中')

  await page.getByTestId('peer-list').getByText('同业LEAKB').click()
  await expect(page).toHaveURL(/LEAKB/)
  await expect(page.getByTestId('analysis-progress')).toHaveCount(0)

  // 放行 A 的迟到响应：不得画进 B 的页面
  const lateA = page.waitForResponse((r) => r.url().includes('/analysis-jobs/job-leak'))
  releaseA!()
  await settleAfter(page, lateA)

  await expect(page).toHaveURL(/LEAKB/)
  await expect(page.getByTestId('analysis-progress')).toHaveCount(0)
  await expect(page.locator('body')).not.toContainText('A标的的分析阶段')
})

// 持仓页批量分析：全部走 route mock，不触发真实 LLM
