// AI 复盘页：追问期间切换报告的竞态回归（检视意见）。
// LLM 端点全部用 route 拦截 mock，不依赖真实 key。
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const user = { username: 'demo', password: 'e2e-user-password' }
const authCookieName = 'investment_session'
const csrfCookieName = 'investment_csrf'

async function loginThroughApi(request: APIRequestContext): Promise<string> {
  const response = await request.post('http://127.0.0.1:18000/api/auth/token', { data: user })
  expect(response.ok()).toBeTruthy()
  return (await response.json()).access_token
}

async function setSession(page: Page, token: string) {
  await page.context().addCookies([
    {
      name: authCookieName,
      value: token,
      url: 'http://127.0.0.1:18000',
      httpOnly: true,
      secure: false,
      sameSite: 'Strict'
    },
    {
      name: csrfCookieName,
      value: 'e2e-csrf-token',
      url: 'http://127.0.0.1:18000',
      httpOnly: false,
      secure: false,
      sameSite: 'Strict'
    }
  ])
  await page.addInitScript(() => {
    window.localStorage.setItem(
      'user',
      JSON.stringify({ id: 2, username: 'demo', email: null, is_active: true, is_admin: false })
    )
  })
}

const REPORT_A = {
  id: 1,
  title: '投资复盘 A',
  trigger_source: 'manual',
  model: 'deepseek-v4-pro',
  total_tokens: 100,
  created_at: '2026-07-29T10:00:00Z'
}
const REPORT_B = { ...REPORT_A, id: 2, title: '投资复盘 B' }

function detailPayload(report: typeof REPORT_A, content: string) {
  return {
    ...report,
    content,
    prompt_tokens: 80,
    completion_tokens: 20,
    messages: []
  }
}

test('a slow answer does not leak into another report after switching', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setSession(page, token)

  let releaseAnswer!: () => void
  const answerGate = new Promise<void>((resolve) => {
    releaseAnswer = resolve
  })

  await page.route('**/api/llm-reports/schedule', (route) =>
    route.fulfill({ json: { cadence: 'off' } })
  )
  await page.route('**/api/llm-reports', (route) => route.fulfill({ json: [REPORT_A, REPORT_B] }))
  await page.route('**/api/llm-reports/1', (route) =>
    route.fulfill({ json: detailPayload(REPORT_A, '# 报告A正文') })
  )
  await page.route('**/api/llm-reports/2', (route) =>
    route.fulfill({ json: detailPayload(REPORT_B, '# 报告B正文') })
  )
  await page.route('**/api/llm-reports/1/messages', async (route) => {
    await answerGate // 挂起，直到测试放行——模拟慢 LLM 响应
    await route.fulfill({
      json: {
        question: { id: 11, role: 'user', content: '问题一', created_at: '2026-07-29T10:01:00Z' },
        answer: {
          id: 12,
          role: 'assistant',
          content: '这是报告A的回答',
          created_at: '2026-07-29T10:01:30Z'
        }
      }
    })
  })

  await page.goto('/reports')
  await expect(page.locator('.markdown-body').first()).toContainText('报告A正文')

  // 在 A 上发起追问（响应被挂起）
  await page.locator('.chat-input textarea').fill('问题一')
  await page.getByRole('button', { name: '追问' }).click()

  // 等待期间切换到报告 B
  await page.locator('.report-item', { hasText: '投资复盘 B' }).click()
  await expect(page.locator('.markdown-body').first()).toContainText('报告B正文')

  // 放行 A 的慢响应；输入框恢复可用（asking 结束）
  releaseAnswer()
  await expect(page.locator('.chat-input textarea')).toBeEnabled()

  // 关键断言：A 的回答不得出现在 B 的界面里
  await expect(page.locator('.chat-messages')).not.toContainText('这是报告A的回答')
  // 残留竞态回归：A 已提交的问题文本不得残留在 B 的输入框中
  await expect(page.locator('.chat-input textarea')).toHaveValue('')
})

test('a slow detail response does not overwrite the newly selected report', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setSession(page, token)

  let releaseDetailA!: () => void
  const detailAGate = new Promise<void>((resolve) => {
    releaseDetailA = resolve
  })
  let detailARequests = 0

  await page.route('**/api/llm-reports/schedule', (route) =>
    route.fulfill({ json: { cadence: 'off' } })
  )
  await page.route('**/api/llm-reports', (route) => route.fulfill({ json: [REPORT_A, REPORT_B] }))
  await page.route('**/api/llm-reports/1', async (route) => {
    detailARequests += 1
    if (detailARequests === 1) {
      await detailAGate // 首次进入页面自动选中 A：挂起，模拟慢响应
    }
    await route.fulfill({ json: detailPayload(REPORT_A, '# 报告A正文') })
  })
  await page.route('**/api/llm-reports/2', (route) =>
    route.fulfill({ json: detailPayload(REPORT_B, '# 报告B正文') })
  )

  await page.goto('/reports')
  // A 的详情被挂起时立刻切到 B（B 快速返回）
  await page.locator('.report-item', { hasText: '投资复盘 B' }).click()
  await expect(page.locator('.markdown-body').first()).toContainText('报告B正文')

  // 放行慢 A —— 它不得覆盖当前选中的 B
  releaseDetailA()
  await page.waitForTimeout(300)
  await expect(page.locator('.markdown-body').first()).toContainText('报告B正文')
  await expect(page.locator('.report-item.active')).toContainText('投资复盘 B')
})

test('a failed slow answer does not leave the question text in another report', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setSession(page, token)

  let failAnswer!: () => void
  const answerGate = new Promise<void>((resolve) => {
    failAnswer = resolve
  })

  await page.route('**/api/llm-reports/schedule', (route) =>
    route.fulfill({ json: { cadence: 'off' } })
  )
  await page.route('**/api/llm-reports', (route) => route.fulfill({ json: [REPORT_A, REPORT_B] }))
  await page.route('**/api/llm-reports/1', (route) =>
    route.fulfill({ json: detailPayload(REPORT_A, '# 报告A正文') })
  )
  await page.route('**/api/llm-reports/2', (route) =>
    route.fulfill({ json: detailPayload(REPORT_B, '# 报告B正文') })
  )
  await page.route('**/api/llm-reports/1/messages', async (route) => {
    await answerGate
    await route.fulfill({ status: 502, json: { detail: 'LLM 调用失败' } })
  })

  await page.goto('/reports')
  await expect(page.locator('.markdown-body').first()).toContainText('报告A正文')
  await page.locator('.chat-input textarea').fill('问题一')
  await page.getByRole('button', { name: '追问' }).click()

  // 挂起期间切到 B，然后让 A 的请求以 502 失败
  await page.locator('.report-item', { hasText: '投资复盘 B' }).click()
  await expect(page.locator('.markdown-body').first()).toContainText('报告B正文')
  failAnswer()

  // 失败路径同样不得把 A 的草稿留在 B 的输入框
  await expect(page.locator('.chat-input textarea')).toBeEnabled()
  await expect(page.locator('.chat-input textarea')).toHaveValue('')
})

test('failed ask keeps the draft for retry while staying on the same report', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setSession(page, token)

  await page.route('**/api/llm-reports/schedule', (route) =>
    route.fulfill({ json: { cadence: 'off' } })
  )
  await page.route('**/api/llm-reports', (route) => route.fulfill({ json: [REPORT_A] }))
  await page.route('**/api/llm-reports/1', (route) =>
    route.fulfill({ json: detailPayload(REPORT_A, '# 报告A正文') })
  )
  await page.route('**/api/llm-reports/1/messages', (route) =>
    route.fulfill({ status: 502, json: { detail: 'LLM 调用失败' } })
  )

  await page.goto('/reports')
  await expect(page.locator('.markdown-body').first()).toContainText('报告A正文')
  await page.locator('.chat-input textarea').fill('要重试的问题')
  await page.getByRole('button', { name: '追问' }).click()

  // 留在原报告：失败保留草稿便于重试
  await expect(page.locator('.chat-input textarea')).toBeEnabled()
  await expect(page.locator('.chat-input textarea')).toHaveValue('要重试的问题')
})

test('a failed list refresh never auto-selects from the stale list', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await setSession(page, token)

  let listRequests = 0
  let detailARequests = 0

  await page.route('**/api/llm-reports/schedule', (route) =>
    route.fulfill({ json: { cadence: 'off' } })
  )
  await page.route('**/api/llm-reports', async (route) => {
    listRequests += 1
    if (listRequests === 1) {
      await route.fulfill({ json: [REPORT_A] })
    } else {
      await route.fulfill({ status: 500, json: { detail: '列表加载失败' } })
    }
  })
  await page.route('**/api/llm-reports/1', async (route, request_) => {
    if (request_.method() === 'DELETE') {
      await route.fulfill({ status: 204, body: '' })
      return
    }
    detailARequests += 1
    await route.fulfill({ json: detailPayload(REPORT_A, '# 报告A正文') })
  })

  await page.goto('/reports')
  await expect(page.locator('.markdown-body').first()).toContainText('报告A正文')
  const detailRequestsBeforeDelete = detailARequests

  // 删除 A → 刷新列表失败（500）→ 不得回退旧列表自动重选已删除的 A
  await page.getByRole('button', { name: '删除报告' }).click()
  await page.locator('.el-message-box__btns .el-button--primary').click()
  await expect(page.locator('.report-list .el-empty, .report-item').first()).toBeVisible({
    timeout: 5000
  })
  await page.waitForTimeout(300)
  expect(detailARequests).toBe(detailRequestsBeforeDelete)
})
