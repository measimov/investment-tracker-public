import { expect, test } from '@playwright/test'
import {
  user,
  adminUser,
  loginThroughApi,
  setAuthenticatedSession,
  createTemporaryUser,
  deleteTemporaryUser
} from './helpers'

test('exchange rate add, edit and deactivate through the UI', async ({ page, request }) => {
  // 汇率是全局表：用 GBP + 旧生效日期（2020-01-02），不影响其他用例依赖的
  // USD/HKD/SGD 最新汇率；POST 端点是 upsert，上次运行残留同键行也不会翻车。
  // 「从API更新汇率」按钮走外部接口，E2E 不外呼——数据联动由当前汇率卡片断言。
  // 写汇率仅管理员（#277）
  const token = await loginThroughApi(request, adminUser)
  await setAuthenticatedSession(page, token)
  await page.goto('/exchange-rates')

  await page.getByRole('button', { name: '手动添加汇率' }).click()
  const addDialog = page.locator('.el-dialog', { hasText: '添加汇率' })
  await expect(addDialog).toBeVisible()

  // 目标币种默认 CNY、来源默认 manual（showAddDialog），只需点开源币种一个
  // 下拉——多个 Element Plus popper 连开会撞上前一个的淡出期，选项定位到
  // 正在关闭的浮层上
  await addDialog.locator('.el-form-item', { hasText: '源币种' }).locator('.el-select').click()
  await page
    .locator('.el-select-dropdown:visible')
    .getByRole('option', { name: '英镑 (GBP)' })
    .click()
  const rateInput = addDialog.locator('.el-form-item', { hasText: '汇率' }).locator('input')
  await rateInput.fill('9.1234')
  await rateInput.blur()
  const dateInput = addDialog.locator('.el-form-item', { hasText: '生效日期' }).locator('input')
  await dateInput.fill('2020-01-02')
  await dateInput.press('Enter')
  await addDialog.getByRole('button', { name: '确定' }).click()

  const rateRow = page
    .locator('.rate-history tr')
    .filter({ hasText: 'GBP' })
    .filter({ hasText: '2020/01/02' })
  await expect(rateRow).toHaveCount(1)
  await expect(rateRow).toContainText('9.1234')
  // 当前汇率卡片联动：GBP 唯一一条即最新，卡片出现
  await expect(page.locator('.current-rates').getByText('GBP', { exact: true })).toBeVisible()

  // 编辑：只改汇率数值
  await rateRow.getByRole('button', { name: '编辑' }).click()
  const editDialog = page.locator('.el-dialog', { hasText: '编辑汇率' })
  await expect(editDialog).toBeVisible()
  const editRateInput = editDialog.locator('.el-form-item', { hasText: '汇率' }).locator('input')
  await editRateInput.fill('8.5')
  await editRateInput.blur()
  await editDialog.getByRole('button', { name: '确定' }).click()
  await expect(rateRow).toContainText('8.5000')

  // 停用（#277：删除改为停用，保留审计）：确认框后行从默认列表消失，当前汇率卡片同步移除 GBP
  await rateRow.getByRole('button', { name: '停用' }).click()
  await page
    .locator('.el-message-box')
    .getByRole('button', { name: /确定|OK/ })
    .click()
  await expect(rateRow).toHaveCount(0)
  await expect(page.locator('.current-rates').getByText('GBP', { exact: true })).toHaveCount(0)
})

test('transaction edit and delete through the dialog', async ({ page, request }) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  try {
    const token = await loginThroughApi(request, {
      username: createdUser.username,
      password
    })
    const buyResponse = await request.post('http://127.0.0.1:18000/api/transactions', {
      headers: { Authorization: `Bearer ${token}` },
      data: {
        symbol: 'EDT001',
        name: '编辑删除测试资产',
        market: 'A股',
        transaction_type: 'BUY',
        quantity: 100,
        price: 10,
        fee: 0,
        transaction_date: '2026-01-05',
        currency: 'CNY',
        notes: 'edit-delete e2e'
      }
    })
    expect(buyResponse.ok()).toBeTruthy()

    await setAuthenticatedSession(page, token, createdUser)
    // 评审 P2：交易写入（PUT/DELETE）后"我的标的"候选缓存必须失效
    let emptySearchCalls = 0
    await page.route('**/api/securities/search*', (route) => {
      if (!new URL(route.request().url()).searchParams.get('q')) emptySearchCalls += 1
      return route.fulfill({
        json: {
          items: [],
          catalog: {
            ready: false,
            stale: true,
            last_success_at: null,
            failing_sources: [],
            capabilities: {}
          }
        }
      })
    })
    await page.route('**/api/securities/resolve*', (route) =>
      route.fulfill({
        json: {
          symbol: 'X',
          market: 'A股',
          name: null,
          currency: null,
          security_type: 'unknown',
          list_status: 'unknown',
          in_catalog: false,
          resolved_from: null,
          error: 'stub'
        }
      })
    )
    const probeEmptySearch = async () => {
      await page.getByRole('button', { name: '新增交易' }).click()
      const probe = page.locator('.el-dialog', { hasText: '新增交易' })
      await probe.locator('.el-form-item', { hasText: '股票代码' }).locator('input').click()
      await page.waitForTimeout(400)
      await probe.getByRole('button', { name: '取消' }).click()
      await expect(probe).toBeHidden()
    }
    await page.goto('/transactions')

    const row = page.locator('tr', { hasText: 'EDT001' })
    await expect(row).toHaveCount(1)
    await probeEmptySearch()
    await probeEmptySearch()
    expect(emptySearchCalls).toBe(1) // 无写入：第二次命中缓存
    await row.getByRole('button', { name: '编辑' }).click()

    const dialog = page.locator('.el-dialog', { hasText: '编辑交易' })
    await expect(dialog).toBeVisible()
    const priceInput = dialog.locator('.el-form-item', { hasText: '价格' }).locator('input')
    await priceInput.fill('12.5')
    await priceInput.blur()
    await dialog.getByRole('button', { name: '确定' }).click()

    await expect(page.getByText('更新成功')).toBeVisible()
    await expect(row).toContainText('12.50')
    await probeEmptySearch()
    expect(emptySearchCalls).toBe(2) // PUT 后失效

    await row.getByRole('button', { name: '删除' }).click()
    await page.locator('.el-message-box').getByRole('button', { name: '删除' }).click()
    await expect(page.getByText('删除成功')).toBeVisible()
    await expect(row).toHaveCount(0)
    await probeEmptySearch()
    expect(emptySearchCalls).toBe(3) // DELETE 后失效
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

test('statistics price dialog what-if updates the FIFO performance card', async ({
  page,
  request
}) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  try {
    const token = await loginThroughApi(request, {
      username: createdUser.username,
      password
    })
    const buyResponse = await request.post('http://127.0.0.1:18000/api/transactions', {
      headers: { Authorization: `Bearer ${token}` },
      data: {
        symbol: 'PRC001',
        name: '价格弹窗测试资产',
        market: 'A股',
        transaction_type: 'BUY',
        quantity: 100,
        price: 10,
        fee: 0,
        transaction_date: '2026-01-05',
        currency: 'CNY',
        notes: 'price-dialog e2e'
      }
    })
    expect(buyResponse.ok()).toBeTruthy()

    await setAuthenticatedSession(page, token, createdUser)
    await page.goto('/statistics')

    await page.getByRole('button', { name: '输入价格' }).click()
    const dialog = page.locator('.el-dialog', { hasText: '输入当前价格' })
    await expect(dialog).toBeVisible()
    const priceInput = dialog.locator('tr', { hasText: 'PRC001' }).locator('input')
    await priceInput.fill('12')
    await priceInput.blur()
    await dialog.getByRole('button', { name: '计算' }).click()

    await expect(page.getByText('计算完成')).toBeVisible()
    // 100 股 × (12 - 10)：当前市值 ¥1,200.00、浮盈率 +20.00%
    const fifoCard = page.locator('.el-card', { hasText: '当前持仓表现' })
    await expect(fifoCard).toContainText('¥1,200.00')
    await expect(fifoCard).toContainText('20.00')
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

// ---------------------------------------------------------------------------
// 观察清单（新 feature 轮 PR-B）：CRUD + 详情页「加入观察」闭环
// ---------------------------------------------------------------------------

test('watchlist add, edit, remove and detail-page integration', async ({ page, request }) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  try {
    const token = await loginThroughApi(request, {
      username: createdUser.username,
      password
    })
    await setAuthenticatedSession(page, token, createdUser)
    // 标的选择器会打 /securities/search（聚焦）与 /resolve（手输后按需解析→腾讯行情）：
    // 打桩保证 CI 零外呼；选择器本身另有专测
    // 空查询 = "我的标的"候选（账本派生、缓存 5 分钟）：计数用来验证账本写入后重新取数
    let emptySearchCalls = 0
    await page.route('**/api/securities/search*', (route) => {
      if (!new URL(route.request().url()).searchParams.get('q')) emptySearchCalls += 1
      return route.fulfill({
        json: {
          items: [],
          catalog: {
            ready: false,
            stale: true,
            last_success_at: null,
            failing_sources: [],
            capabilities: {}
          }
        }
      })
    })
    // 打开添加对话框并聚焦代码框：触发一次空查询；紧接着取消
    const probeEmptySearch = async () => {
      await page.getByTestId('add-watchlist-button').click()
      const probe = page.locator('.el-dialog', { hasText: '添加观察标的' })
      await probe.locator('.el-form-item', { hasText: '股票代码' }).locator('input').click()
      await page.waitForTimeout(400) // el-autocomplete 聚焦取数有 200ms debounce
      await probe.getByRole('button', { name: '取消' }).click()
      await expect(probe).toBeHidden()
    }
    await page.route('**/api/securities/resolve*', (route) =>
      route.fulfill({
        json: {
          symbol: 'WCH001',
          market: 'A股',
          name: null,
          name_en: null,
          currency: null,
          security_type: 'unknown',
          list_status: 'unknown',
          in_catalog: false,
          resolved_from: null,
          error: 'stub'
        }
      })
    )

    // 1) 观察清单页添加
    await page.goto('/watchlist')
    await page.getByTestId('add-watchlist-button').click()
    const dialog = page.locator('.el-dialog', { hasText: '添加观察标的' })
    await expect(dialog).toBeVisible()
    await dialog.locator('.el-form-item', { hasText: '股票代码' }).locator('input').fill('WCH001')
    await dialog.locator('.el-form-item', { hasText: '市场' }).locator('.el-select').click()
    await page.locator('.el-select-dropdown:visible').getByRole('option', { name: 'A股' }).click()
    await dialog
      .locator('.el-form-item', { hasText: '观察理由' })
      .locator('textarea')
      .fill('等待 PB 回到 1.2 以下')
    await page.getByTestId('watchlist-submit').click()
    await expect(page.getByText('已加入观察清单')).toBeVisible()

    const row = page.locator('tr', { hasText: 'WCH001' })
    await expect(row).toHaveCount(1)
    await expect(row).toContainText('等待 PB 回到 1.2 以下')
    await expect(row).toContainText('未同步档案') // 无档案数据 → 不显示误导性 0/7

    // 重复添加 → 409 中文报错
    await page.getByTestId('add-watchlist-button').click()
    await dialog.locator('.el-form-item', { hasText: '股票代码' }).locator('input').fill('WCH001')
    await dialog.locator('.el-form-item', { hasText: '市场' }).locator('.el-select').click()
    await page.locator('.el-select-dropdown:visible').getByRole('option', { name: 'A股' }).click()
    await page.getByTestId('watchlist-submit').click()
    await expect(page.getByText('该标的已在观察清单中')).toBeVisible()
    await dialog.getByRole('button', { name: '取消' }).click()

    // 2) 编辑理由
    await row.getByRole('button', { name: '编辑' }).click()
    const editDialog = page.locator('.el-dialog', { hasText: '编辑观察标的' })
    await editDialog
      .locator('.el-form-item', { hasText: '观察理由' })
      .locator('textarea')
      .fill('修订后的观察理由')
    await page.getByTestId('watchlist-submit').click()
    await expect(row).toContainText('修订后的观察理由')
    // 评审 P2：编辑自选（PUT）后候选缓存必须失效——下次空查询重新取数；无写入则命中缓存
    const afterAdds = emptySearchCalls
    await probeEmptySearch()
    expect(emptySearchCalls).toBe(afterAdds + 1)
    await probeEmptySearch()
    expect(emptySearchCalls).toBe(afterAdds + 1)

    // 3) 代码链接进详情页 → 显示已在观察（el-link 无 href，不具 link role，按文本点）
    await row.locator('.el-link', { hasText: 'WCH001' }).click()
    await expect(page).toHaveURL(/securities/)
    await expect(page.getByText('已在观察清单', { exact: true })).toBeVisible()

    // 4) 另一标的从详情页加入观察（prompt 填理由）
    await page.goto(`/securities/${encodeURIComponent('A股')}/WCH002`)
    await page.getByTestId('add-to-watchlist-button').click()
    const promptBox = page.locator('.el-message-box', { hasText: '加入观察' })
    await promptBox.locator('textarea').fill('详情页加入的理由')
    await promptBox.getByRole('button', { name: '加入' }).click()
    await expect(page.getByText('已加入观察清单')).toBeVisible()
    await expect(page.getByText('已在观察清单', { exact: true })).toBeVisible()

    // 5) 清单页可见两条；移除一条
    await page.goto('/watchlist')
    await expect(page.locator('tr', { hasText: 'WCH002' })).toContainText('详情页加入的理由')
    await page.locator('tr', { hasText: 'WCH002' }).getByRole('button', { name: '移除' }).click()
    await page.locator('.el-message-box').getByRole('button', { name: '确定' }).click()
    await expect(page.getByText('已移出观察清单')).toBeVisible()
    await expect(page.locator('tr', { hasText: 'WCH002' })).toHaveCount(0)
    await expect(page.locator('tr', { hasText: 'WCH001' })).toHaveCount(1)
    // 评审 P2：移除自选（DELETE）后候选缓存失效——空查询重新取数
    const beforeRemoveProbe = emptySearchCalls
    await probeEmptySearch()
    expect(emptySearchCalls).toBe(beforeRemoveProbe + 1)
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

// ---------------------------------------------------------------------------
// 雪球观点页：e2e 库的归档表由迁移建出但为空、采集器未启用且从未运行——正好验证
// "数据源未接入"的显式降级（状态条 + 空表提示 + 批量按钮禁用）与采集器卡片
// （未启用），页面不得 5xx/白屏。
// ---------------------------------------------------------------------------

test('opinions page degrades explicitly without collected data', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)

  await page.goto('/opinions')
  await expect(page.getByTestId('opinion-source-missing')).toBeVisible()
  await expect(page.getByTestId('opinion-batch-button')).toBeDisabled()
  await expect(page.getByTestId('opinion-symbols-table')).toBeVisible()
  await expect(page.getByTestId('xueqiu-collector-card')).toBeVisible()
  await expect(page.getByTestId('collector-health')).toHaveText('未启用')
  // 更新 Cookie 仅管理员可见
  await expect(page.getByTestId('collector-update-cookie')).toHaveCount(0)
  // 按标的采集从未运行：摘要行如实说明；「今日热帖」卡已下线（2026-09-28），不再渲染
  await expect(page.getByTestId('collector-symbols-summary')).toContainText('尚未运行')
  await expect(page.getByTestId('xueqiu-hots-card')).toHaveCount(0)

  // 详情页的观点 section：生成按钮点击后收到 409 预检（e2e 环境先命中
  // "未配置 LLM"，配了 key 的环境则是"数据源未接入"），以信息条呈现而非报错弹窗
  await page.goto('/securities/A股/600036')
  await page.getByRole('tab', { name: '观点' }).click()
  await expect(page.getByTestId('opinion-section')).toBeVisible()
  await page.getByTestId('generate-opinion-button').click()
  await expect(page.getByTestId('opinion-section').locator('.el-alert')).toContainText(
    /未接入|未配置 LLM/
  )

  // 雪球公告/讨论折叠区：展开才拉取，库里没有数据时给空态与「尚未运行」提示
  await page.getByTestId('xueqiu-symbol-feed').getByText('雪球公告 / 讨论').click()
  await expect(page.getByTestId('xueqiu-symbol-feed')).toContainText('按标的采集尚未运行过')
  await expect(page.getByTestId('xueqiu-announcements')).toContainText('暂无雪球公告')
})

// 管理员的「更新 Cookie」对话框：e2e 环境没配 XUEQIU_COOKIE_FILE，对话框只读展示原因
// （不给提交按钮），不得报错。写入/校验/不回显归后端 pytest 与 vitest。
test('admin sees why the Xueqiu cookie cannot be updated from the UI', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request, adminUser)
  await setAuthenticatedSession(page, token)

  await page.goto('/opinions')
  await page.getByTestId('collector-update-cookie').click()
  const dialog = page.getByTestId('xueqiu-cookie-dialog')
  await expect(dialog.getByTestId('xueqiu-cookie-status')).toContainText('未配置')
  await expect(dialog.getByTestId('xueqiu-cookie-not-writable')).toContainText('XUEQIU_COOKIE_FILE')
  await expect(dialog.getByTestId('xueqiu-cookie-submit')).toHaveCount(0)
  await expect(dialog.getByTestId('xueqiu-cookie-input')).toHaveCount(0)
})

// ---------------------------------------------------------------------------
// 标的可搜索下拉（标的全集）：选中候选完整回填；手输新代码 + 选市场按需解析只填空；
// 用户手改过的名称不被再次覆盖。/search 与 /resolve 打桩——目录内容与排序归后端 pytest
// ---------------------------------------------------------------------------

test('security select fills the transaction form from the catalog and keeps user edits', async ({
  page,
  request
}) => {
  const { adminToken, createdUser, password } = await createTemporaryUser(request)
  try {
    const token = await loginThroughApi(request, { username: createdUser.username, password })
    await setAuthenticatedSession(page, token, createdUser)

    const catalog = [
      {
        symbol: '00700',
        market: '港股',
        name: '腾讯控股',
        name_en: 'Tencent Holdings Ltd.',
        name_trad: '騰訊控股',
        pinyin: 'TXKG',
        currency: 'HKD',
        security_type: 'stock',
        board: '主板',
        exchange: 'HKEX',
        list_status: 'listed',
        in_catalog: true,
        origins: [],
        last_used: null
      },
      {
        symbol: '510300',
        market: 'A股',
        name: '沪深300ETF',
        name_en: null,
        name_trad: null,
        pinyin: 'HS300ETF',
        currency: 'CNY',
        security_type: 'etf',
        board: null,
        exchange: 'SSE',
        list_status: 'listed',
        in_catalog: true,
        origins: [],
        last_used: null
      }
    ]
    await page.route('**/api/securities/search*', async (route) => {
      const q = (new URL(route.request().url()).searchParams.get('q') || '').toUpperCase()
      const items = q
        ? catalog.filter((c) => c.symbol.includes(q) || c.pinyin.includes(q) || c.name.includes(q))
        : []
      await route.fulfill({
        json: {
          items,
          catalog: {
            ready: true,
            stale: false,
            last_success_at: null,
            failing_sources: [],
            capabilities: { pinyin: true, simplified: true }
          }
        }
      })
    })
    const resolveCalls: string[] = []
    await page.route('**/api/securities/resolve*', async (route) => {
      const url = new URL(route.request().url())
      const symbol = url.searchParams.get('symbol') || ''
      const market = url.searchParams.get('market') || ''
      resolveCalls.push(`${market}|${symbol}`)
      const hit = symbol === 'D05' && market === '新加坡股'
      await route.fulfill({
        json: {
          symbol,
          market,
          name: hit ? 'DBS Group' : null,
          name_en: null,
          currency: hit ? 'SGD' : null,
          security_type: 'unknown',
          list_status: 'unknown',
          in_catalog: hit,
          resolved_from: hit ? 'tencent-quote' : null,
          error: hit ? null : 'stub'
        }
      })
    })

    await page.goto('/transactions')
    await page.getByRole('button', { name: '新增交易' }).click()
    const dialog = page.locator('.el-dialog', { hasText: '新增交易' })
    await expect(dialog).toBeVisible()
    const formItem = (label: RegExp) =>
      dialog
        .locator('.el-form-item')
        .filter({ has: page.locator('.el-form-item__label', { hasText: label }) })
        .first()
    // data-testid 经 el-autocomplete → el-input 透传落在原生 input 上，按表单项取 textbox 更稳
    const symbolInput = formItem(/^股票代码$/).getByRole('textbox')
    const nameInput = dialog.getByPlaceholder('资产名称')

    // 1) 拼音检索 → 选中候选 → 名称/市场/币种一起回填
    await symbolInput.fill('txkg')
    const popper = page.locator('.security-select-popper:visible')
    await expect(popper.getByRole('option', { name: /00700/ })).toBeVisible()
    await expect(popper).toContainText('腾讯控股')
    await expect(popper).toContainText('港股')
    await popper.getByRole('option', { name: /00700/ }).click()
    await expect(symbolInput).toHaveValue('00700')
    await expect(nameInput).toHaveValue('腾讯控股')
    await expect(formItem(/^市场$/).locator('.el-select')).toContainText('港股')
    await expect(formItem(/^币种$/).locator('.el-select')).toContainText('HKD')

    // 2) 手输新代码：自动带出的名称清空；选定市场后按需解析只填空的名称与推断币种
    await symbolInput.fill('D05')
    await symbolInput.press('Tab')
    await expect(nameInput).toHaveValue('')
    await formItem(/^市场$/)
      .locator('.el-select')
      .click()
    await page
      .locator('.el-select-dropdown:visible')
      .getByRole('option', { name: '新加坡股' })
      .click()
    await expect(nameInput).toHaveValue('DBS Group')
    await expect(formItem(/^币种$/).locator('.el-select')).toContainText('SGD')
    expect(resolveCalls).toContain('新加坡股|D05')

    // 3) 用户手改名称后再换代码：名称保留，不被清空也不被解析结果覆盖
    await nameInput.fill('DBS')
    await symbolInput.fill('D06')
    await symbolInput.press('Tab')
    await expect(symbolInput).toHaveValue('D06')
    await expect(nameInput).toHaveValue('DBS')
    expect(resolveCalls).toContain('新加坡股|D06')

    // 4) ETF 候选带类型标签
    await symbolInput.fill('510300')
    await expect(popper.getByRole('option', { name: /510300/ })).toContainText('ETF')

    // 5) 评审 P1：币种仍是自动推导值时切到推不出的市场必须清空——否则 BTC 会以 CNY 入账
    await symbolInput.fill('BTC')
    await symbolInput.press('Tab')
    await formItem(/^市场$/)
      .locator('.el-select')
      .click()
    await page
      .locator('.el-select-dropdown:visible')
      .getByRole('option', { name: '加密货币' })
      .click()
    await expect(formItem(/^币种$/).locator('.el-select')).not.toContainText(/CNY|HKD|USD|SGD/)
    await dialog.getByRole('button', { name: '确定' }).click()
    await expect(dialog.getByText('请选择币种')).toBeVisible()
    await dialog.getByRole('button', { name: '取消' }).click()
  } finally {
    await deleteTemporaryUser(request, adminToken, createdUser.id)
  }
})

// ElAutocomplete 的 blur 把 close() 推迟到 setTimeout 里且不复查焦点：失焦后同一个宏任务内
// 回到输入框并输入（上面用例的 Tab → fill 在满载套件里就会落进这个窗口），迟到的 close 曾把
// 刚激活的下拉关掉、候选回来也不弹。这里在一个任务里完成「失焦 → 回焦 → 输入」，确定性复现。
test('security select still opens when refocused and typed before the deferred blur fires', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setAuthenticatedSession(page, token)
  await page.route('**/api/securities/search*', async (route) => {
    const q = new URL(route.request().url()).searchParams.get('q') || ''
    const items =
      q === '510300'
        ? [
            {
              symbol: '510300',
              market: 'A股',
              name: '沪深300ETF',
              security_type: 'etf',
              list_status: 'listed',
              in_catalog: true,
              origins: []
            }
          ]
        : []
    await route.fulfill({
      json: {
        items,
        catalog: {
          ready: true,
          stale: false,
          last_success_at: null,
          failing_sources: [],
          capabilities: { pinyin: true, simplified: true }
        }
      }
    })
  })

  await page.goto('/transactions')
  await page.getByRole('button', { name: '新增交易' }).click()
  const dialog = page.locator('.el-dialog', { hasText: '新增交易' })
  await expect(dialog).toBeVisible()
  await page.evaluate(() => {
    const symbol = document.querySelector<HTMLInputElement>(
      '.el-dialog [data-testid="symbol-autocomplete"]'
    )
    const name = document.querySelector<HTMLInputElement>('.el-dialog [placeholder="资产名称"]')
    if (!symbol || !name) throw new Error('表单输入框未找到')
    symbol.focus()
    name.focus() // 失焦：ElAutocomplete 在 setTimeout 里才 close()
    symbol.focus() // 同一个任务内回焦并输入，赶在那个迟到的 close 之前
    symbol.value = '510300'
    symbol.dispatchEvent(new Event('input', { bubbles: true }))
  })
  const popper = page.locator('.security-select-popper:visible')
  await expect(popper.getByRole('option', { name: /510300/ })).toContainText('ETF')
})

// 官方公告（#306）：持仓徽标与详情页「公告」tab，全部走 route mock
