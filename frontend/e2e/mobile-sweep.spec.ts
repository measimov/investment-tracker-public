// 移动端兼容性全页扫描（阶段 4）：Pixel 5 视口（393×851，Chromium 系）逐页断言无横向溢出。
// 横向滚动是移动端布局破损的最可靠信号——任何页面 body 都不得横向滚动。
import { devices, expect, test, type APIRequestContext, type Page } from '@playwright/test'

const user = { username: 'demo', password: 'e2e-user-password' }
const adminUser = { username: 'admin', password: 'e2e-admin-password' }
const authCookieName = 'investment_session'
const csrfCookieName = 'investment_csrf'

test.use({ ...devices['Pixel 5'] })

async function loginThroughApi(request: APIRequestContext, credentials = user): Promise<string> {
  const response = await request.post('http://127.0.0.1:18000/api/auth/token', {
    data: credentials
  })
  expect(response.ok()).toBeTruthy()
  return (await response.json()).access_token
}

async function seedData(request: APIRequestContext, token: string) {
  const headers = { Authorization: `Bearer ${token}` }

  // 每个 seed 请求都必须成功：接口失败会让后续页面在空态下测量，产生假阳性
  async function seedPost(path: string, data: Record<string, unknown>) {
    const response = await request.post(`http://127.0.0.1:18000${path}`, { headers, data })
    expect(
      response.ok(),
      `seed ${path} 失败: ${response.status()} ${await response.text()}`
    ).toBeTruthy()
  }

  // 券商账户：账户数据页的"新增事件/新增核对"在无账户时是禁用的，
  // 没有它对话框覆盖会卡在等待一个永远点不动的按钮上
  await seedPost('/api/broker-accounts', {
    broker: '招商证券',
    account_name: '移动端扫描账户',
    account_number_masked: '****4321',
    base_currency: 'CNY'
  })

  // 一笔持仓 + 一笔已平仓 + 汇率 + 现金股息，让各页有真实内容可渲染
  await seedPost('/api/exchange-rates', {
    from_currency: 'USD',
    to_currency: 'CNY',
    rate: 7.2,
    effective_date: '2026-01-01'
  })
  for (const txn of [
    {
      symbol: '600000',
      name: '移动端扫描标的',
      market: 'A股',
      transaction_type: 'BUY',
      quantity: 100,
      price: 10,
      fee: 1,
      transaction_date: '2026-01-05',
      currency: 'CNY'
    },
    {
      symbol: '600000',
      name: '移动端扫描标的',
      market: 'A股',
      transaction_type: 'SELL',
      quantity: 40,
      price: 12,
      fee: 1,
      transaction_date: '2026-02-05',
      currency: 'CNY'
    }
  ]) {
    await seedPost('/api/transactions', txn)
  }
  await seedPost('/api/corporate-actions/cash-dividend', {
    symbol: '600000',
    name: '移动端扫描标的',
    market: 'A股',
    ex_date: '2026-03-01',
    dividend_per_share: 0.3,
    total_dividend: 30,
    tax_rate: 0.1,
    currency: 'CNY'
  })
}

// 种子标记内容必须真的渲染出来，才证明页面不是在空态/重定向下测量的
const SEEDED_CONTENT: Record<string, string> = {
  '/transactions': '移动端扫描标的',
  '/holdings': '600000',
  '/corporate-actions': '移动端扫描标的',
  '/exchange-rates': 'USD'
}

async function setSession(page: Page, token: string, isAdmin = false) {
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
  await page.addInitScript(
    ({ admin }) => {
      window.localStorage.setItem(
        'user',
        JSON.stringify({
          id: admin ? 1 : 2,
          username: admin ? 'admin' : 'demo',
          email: null,
          is_active: true,
          is_admin: admin
        })
      )
    },
    { admin: isAdmin }
  )
}

const ROUTES: Array<[string, string]> = [
  ['/', '仪表盘'],
  ['/transactions', '交易记录'],
  ['/holdings', '持仓'],
  ['/statistics', '统计分析'],
  ['/account-data', '账户数据'],
  ['/corporate-actions', '公司行动'],
  ['/exchange-rates', '汇率'],
  ['/reports', 'AI 复盘'],
  // 标的档案详情页：年度科目表/财报摘要/AI 分析全文都在这里，是本页面族里
  // 表格与长文本最密集的一页，此前一直漏在移动端扫描之外
  ['/securities/A股/600000', '标的档案']
]

// 管理员路由单独跑：普通用户会被 requiresAdmin 守卫重定向，
// 在登录页上测"无横向溢出"是假通过
const ADMIN_ROUTES: Array<[string, string]> = [
  ['/admin/users', '用户管理'],
  ['/admin/holdings', '全部持仓']
]

async function sweepRoutes(page: Page, routes: Array<[string, string]>): Promise<string[]> {
  const failures: string[] = []
  for (const [route, label] of routes) {
    await page.goto(route)
    await page.waitForLoadState('networkidle')
    // 守卫重定向会把断言变成"在登录页上测登录页"，先确认没被踢走
    // pathname 是百分号编码的（中文市场名如 A股），比较前先解码
    expect(
      decodeURIComponent(new URL(page.url()).pathname),
      `${label}(${route}) 被重定向到 ${page.url()}`
    ).toBe(route)
    const marker = SEEDED_CONTENT[route]
    if (marker) {
      await expect(
        page.locator(`text=${marker}`).first(),
        `${label}(${route}) 未渲染出种子内容，页面可能处于空态或被重定向`
      ).toBeVisible()
    }
    const overflow = await page.evaluate(() => {
      const doc = document.documentElement
      // 量 body 而非 documentElement：`html { overflow-x: hidden }`（styles.css）
      // 把 documentElement.scrollWidth 钳在视口宽度上，那个差值**永远是 0**。
      // 实测往 body 插一个 1400px 的块：docScroll 仍是 1280，bodyScroll 才是 1400。
      //
      // 注意这条断言的**真实强度有限**：`.el-card__body` 与 `.el-main` 都是
      // overflow-x:auto，卡片内的宽内容会变成内层滚动条而非撑宽文档。它能抓到的
      // 是卡片之外的溢出（对话框、页头、固定定位元素）。卡片内容是否可达，靠
      // "宽内容必须落在可滚动容器里" 那条断言保证。
      const overflowPx = Math.max(document.body.scrollWidth - doc.clientWidth, 0)
      // 找出最宽的溢出元素帮助定位
      let worst: { right: number; tag: string; cls: string } | null = null
      if (overflowPx > 1) {
        for (const el of document.querySelectorAll('body *')) {
          const rect = el.getBoundingClientRect()
          if (rect.right > doc.clientWidth + 1 && (!worst || rect.right > worst.right)) {
            worst = {
              right: Math.round(rect.right),
              tag: el.tagName,
              cls: String(el.className).slice(0, 80)
            }
          }
        }
      }
      return { overflowPx, worst }
    })
    if (overflow.overflowPx > 1) {
      failures.push(
        `${label}(${route}): 溢出 ${overflow.overflowPx}px ${JSON.stringify(overflow.worst)}`
      )
    }
  }
  return failures
}

test('mobile viewport has no horizontal overflow on any page', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await seedData(request, token)
  await setSession(page, token)

  const failures = await sweepRoutes(page, ROUTES)
  expect(failures, failures.join('\n')).toEqual([])
})

test('mobile admin pages have no horizontal overflow', async ({ page, request }) => {
  const token = await loginThroughApi(request, adminUser)
  await setSession(page, token, true)

  const failures = await sweepRoutes(page, ADMIN_ROUTES)
  expect(failures, failures.join('\n')).toEqual([])
})

test('mobile login page has no horizontal overflow', async ({ page }) => {
  await page.goto('/login')
  await page.waitForLoadState('networkidle')
  const overflowPx = await page.evaluate(() =>
    Math.max(document.documentElement.scrollWidth - document.documentElement.clientWidth, 0)
  )
  expect(overflowPx).toBeLessThanOrEqual(1)
})

test('mobile dialogs are constrained within the viewport', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await setSession(page, token)

  async function assertDialogFits(label: string) {
    const dialog = page.locator('.el-dialog:visible').first()
    await expect(dialog, label).toBeVisible()
    const box = await dialog.boundingBox()
    const viewport = page.viewportSize()
    expect(box, `${label} 对话框未渲染`).not.toBeNull()
    expect(viewport, '视口尺寸不可用').not.toBeNull()
    expect(box!.width, `${label} 对话框宽度超出视口`).toBeLessThanOrEqual(viewport!.width)
    expect(box!.x, `${label} 对话框左缘越界`).toBeGreaterThanOrEqual(0)
    await page.keyboard.press('Escape')
    await expect(dialog).toBeHidden()
  }

  // 全部普通用户可达的对话框，按声明宽度三档（420 / 560 / 720）各有覆盖
  const dialogs: Array<[string, string, string?]> = [
    ['/transactions', '新增交易'],
    ['/transactions', '导入'],
    // 移动卡片里按钮写全称，桌面表格才是'转仓'
    ['/holdings', '转仓到其他账户'],
    ['/statistics', '输入价格'],
    ['/exchange-rates', '手动添加汇率'],
    ['/corporate-actions', '新增记录'],
    ['/account-data', '新增账户'],
    ['/account-data', '新增事件', '现金事件'],
    ['/account-data', '新增核对', '月末核对'],
    ['/account-data', '新增规则', '特例规则']
  ]

  let current = ''
  for (const [route, trigger, tab] of dialogs) {
    if (route !== current) {
      await page.goto(route)
      await page.waitForLoadState('networkidle')
      current = route
    }
    if (tab) {
      await page.getByRole('tab', { name: tab }).click()
    }
    await page.getByRole('button', { name: trigger, exact: true }).first().click()
    await assertDialogFits(`${route} · ${trigger}`)
  }
})

test('mobile admin dialogs are constrained within the viewport', async ({ page, request }) => {
  const token = await loginThroughApi(request, adminUser)
  await setSession(page, token, true)

  await page.goto('/admin/users')
  await page.waitForLoadState('networkidle')
  for (const trigger of ['添加用户', '重置密码']) {
    await page.getByRole('button', { name: trigger, exact: true }).first().click()
    const dialog = page.locator('.el-dialog:visible').first()
    await expect(dialog, trigger).toBeVisible()
    const box = await dialog.boundingBox()
    const viewport = page.viewportSize()
    expect(box!.width, `${trigger} 对话框宽度超出视口`).toBeLessThanOrEqual(viewport!.width)
    expect(box!.x, `${trigger} 对话框左缘越界`).toBeGreaterThanOrEqual(0)
    await page.keyboard.press('Escape')
    await expect(dialog).toBeHidden()
  }
})

// 合法长文本不得把卡片撑破：schema 上限的账户名（100）与无断点备注渲染后，
// 卡片与操作区的 bounding box 必须仍落在视口内。只查根节点 scrollWidth 会假
// 通过——外层容器把超宽内容裁掉了，根节点不横滚，但按钮已经点不到（评审实测）。
test('mobile cards keep long text and actions inside the viewport', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  const headers = { Authorization: `Bearer ${token}` }
  // 无空格 ASCII 是最坏的合法输入：中文可任意折行，掩盖不了断行约束的缺失
  const longName = 'B'.repeat(90)
  const longNote = 'A'.repeat(120)

  const created = await request.post('http://127.0.0.1:18000/api/broker-accounts', {
    headers,
    data: {
      broker: '招商证券',
      account_name: longName,
      account_number_masked: '****9999',
      base_currency: 'CNY',
      notes: longNote
    }
  })
  expect(created.ok(), `种子账户失败: ${created.status()} ${await created.text()}`).toBeTruthy()

  await setSession(page, token)
  await page.goto('/account-data')
  await page.waitForLoadState('networkidle')

  const card = page.getByTestId('account-card').filter({ hasText: longNote }).first()
  await expect(card, '长文本账户卡片未渲染').toBeVisible()

  const viewport = page.viewportSize()!
  const cardBox = (await card.boundingBox())!
  expect(
    Math.round(cardBox.x + cardBox.width),
    `卡片右缘 ${Math.round(cardBox.x + cardBox.width)} 超出视口 ${viewport.width}`
  ).toBeLessThanOrEqual(viewport.width)

  const actions = card.locator('.mobile-card-actions').first()
  const actionsBox = (await actions.boundingBox())!
  expect(
    Math.round(actionsBox.x + actionsBox.width),
    `操作区右缘 ${Math.round(actionsBox.x + actionsBox.width)} 超出视口 ${viewport.width}`
  ).toBeLessThanOrEqual(viewport.width)

  // 删除按钮必须真的可点：命中测试落在按钮自身上，而不是被裁到屏幕外
  const deleteButton = actions.getByRole('button', { name: '删除空账户' })
  await expect(deleteButton).toBeVisible()
  const buttonBox = (await deleteButton.boundingBox())!
  expect(buttonBox.x + buttonBox.width).toBeLessThanOrEqual(viewport.width)
})

// 工具栏在窄屏必须纵向堆叠：标题与操作区并排时按钮会被挤成一列图标，
// 这是横向溢出检查抓不到的破损（元素没出界，只是被压扁）
test('mobile page toolbars stack vertically', async ({ page, request }) => {
  const token = await loginThroughApi(request)
  await seedData(request, token)
  await setSession(page, token)

  const failures: string[] = []
  for (const [route, label] of ROUTES) {
    await page.goto(route)
    await page.waitForLoadState('networkidle')
    const header = page.locator('.page-header').first()
    if ((await header.count()) === 0) continue
    const actions = header.locator('.header-actions').first()
    if ((await actions.count()) === 0) continue
    const headerBox = await header.boundingBox()
    const actionsBox = await actions.boundingBox()
    if (!headerBox || !actionsBox) continue
    // 堆叠后操作区顶边应明显低于页头顶边（并排时二者几乎齐平）
    if (actionsBox.y - headerBox.y < 12) {
      failures.push(
        `${label}(${route}): 工具栏未堆叠，header.y=${Math.round(headerBox.y)} actions.y=${Math.round(actionsBox.y)}`
      )
    }
  }
  expect(failures, failures.join('\n')).toEqual([])
})

// ---------------------------------------------------------------------------
// 标的档案详情页（数据填满时）
// ---------------------------------------------------------------------------

// 空态下的详情页只有骨架，测不出真正的移动端风险——年度科目表 12 列、
// AI 分析全文、财报摘要卡片都要在有数据时才渲染。这里用 route 桩把接口填满，
// 只测布局（不依赖 LLM 与外部数据源）。
async function stubPopulatedSecurity(page: Page) {
  const pivotRow = (year: number) => ({
    end_date: `${year}1231`,
    fp: 'FY',
    currency: 'USD',
    total_revenue: 123456789012,
    cost_of_revenue: 65432109876,
    operating_income: 34567890123,
    n_income_attr_p: 28901234567,
    total_assets: 456789012345,
    total_liab: 234567890123,
    total_hldr_eqy_exc_min_int: 222221122222,
    n_cashflow_act: 45678901234,
    free_cashflow: 34567890123,
    basic_eps: 12.3456
  })
  await page.route('**/api/securities/**/profile*', async (route) => {
    await route.fulfill({
      json: {
        symbol: '600000',
        market: 'A股',
        name: '移动端扫描标的股份有限公司',
        capabilities: { structured: true, report_digest: true, risk_signals: true },
        datasets: { yahoo_fundamentals: Array.from({ length: 12 }, (_, i) => pivotRow(2025 - i)) },
        latest_periods: { yahoo_fundamentals: '20251231|FY' },
        business: {
          profile: {
            商业模式:
              '公司以自主研发与规模化制造为核心，通过经销与直营双渠道触达终端消费者。'.repeat(6),
            行业与竞争: '行业集中度持续提升，前三大品牌合计份额超过六成，价格战阶段性缓和。'.repeat(
              4
            ),
            供应商集中度: '前五大供应商采购占比 23.4%，其中芯片与压缩机为关键瓶颈环节。',
            客户集中度: '前五大客户收入占比 8.7%，渠道分散，单一客户依赖度低。',
            业务分部: [
              { 名称: '智能家居', 收入占比: '57.3%', 毛利率: '28.4%', 趋势: '上升' },
              { 名称: '工业技术', 收入占比: '21.8%', 毛利率: '19.6%', 趋势: '平稳' },
              { 名称: '楼宇科技', 收入占比: '12.4%', 毛利率: '31.2%', 趋势: '上升' }
            ],
            上游依赖: [
              { 要素: '钢材与铜材', 影响: '直接构成制造成本，价格上行会压缩毛利率约 1-2 个百分点' },
              { 要素: '功率半导体', 影响: '供给紧张时交付周期拉长，影响旺季出货节奏' }
            ],
            下游需求: [
              { 客群或场景: '国内家电换新', 需求驱动: '以旧换新补贴与地产竣工面积' },
              { 客群或场景: '海外自有品牌', 需求驱动: '渠道去库存结束后的补库需求' }
            ],
            估值观察因子: [
              {
                因子: '毛利率修复弹性',
                方向: '正向',
                传导: '原材料回落 → 毛利率修复 → 净利润增速上台阶'
              },
              {
                因子: '海外收入占比',
                方向: '正向',
                传导: '汇率贬值 → 出口竞争力 → 收入与汇兑收益双升'
              }
            ]
          },
          peers: [
            { symbol: '000651', name: '格力电器' },
            { symbol: '000333', name: '美的集团' },
            { symbol: '600690', name: '海尔智家' }
          ],
          industry: '家用电器'
        },
        earnings_quality: {
          status: 'ok',
          years: ['2025', '2024', '2023'],
          per_year: {
            2025: { cfo_ni_ratio: 1.3478, accruals_ratio: -0.0384, gross_margin: 56.2134 }
          },
          beneish_m_score: { 2025: { score: -2.648, flag: false } }
        },
        report_digests: [
          {
            end_date: '20251231',
            report_type: 'annual',
            digest: {
              经营回顾: '报告期内公司实现营业收入 3,391.23 亿元，同比增长 14.2%。'.repeat(6),
              风险要点: '原材料价格波动、海外关税政策变化、汇率波动对出口业务的影响。'.repeat(4)
            }
          }
        ],
        digest_progress: { digested: 8, failed_capped: 1 }
      }
    })
  })
  await page.route('**/api/securities/**/analysis*', async (route) => {
    await route.fulfill({
      json: {
        symbol: '600000',
        market: 'A股',
        tags: ['业绩增长', '现金流背离', '利润质量存疑', '估值偏高'],
        risk_level: 'medium',
        summary: '经营现金流对净利润覆盖充分，但应收增速持续快于营收，需关注渠道压货。',
        report_markdown: [
          '## 商业模式与产业链',
          '公司以自主研发与规模化制造为核心，'.repeat(20),
          '## 财务质量趋势',
          '| 报告期 | 营业收入 | 归母净利润 | 经营现金流 | 毛利率 |',
          '| --- | --- | --- | --- | --- |',
          '| 2025 | 3,391.23 亿元 | 289.01 亿元 | 456.79 亿元 | 56.21% |',
          '| 2024 | 2,970.15 亿元 | 251.33 亿元 | 398.22 亿元 | 52.90% |',
          '## 待关注问题',
          '1. 应收账款增速连续两年快于营收，'.repeat(8)
        ].join('\n\n'),
        created_at: '2026-08-04T10:00:00',
        model: 'deepseek-v4-pro'
      }
    })
  })
}

test('mobile security detail page stays inside the viewport when populated', async ({
  page,
  request
}) => {
  const token = await loginThroughApi(request)
  await setSession(page, token)
  await stubPopulatedSecurity(page)

  await page.goto('/securities/A股/600000')
  await page.waitForLoadState('networkidle')
  await expect(page.locator('text=智能家居').first()).toBeVisible()

  const viewport = page.viewportSize()!
  const overflow = await page.evaluate(() => {
    const doc = document.documentElement
    const overflowPx = Math.max(document.body.scrollWidth - doc.clientWidth, 0)
    let worst: { right: number; tag: string; cls: string } | null = null
    if (overflowPx > 1) {
      for (const el of document.querySelectorAll('body *')) {
        const rect = el.getBoundingClientRect()
        if (rect.right > doc.clientWidth + 1 && (!worst || rect.right > worst.right)) {
          worst = {
            right: Math.round(rect.right),
            tag: el.tagName,
            cls: String(el.className).slice(0, 90)
          }
        }
      }
    }
    return { overflowPx, worst }
  })
  expect(
    overflow.overflowPx,
    `详情页横向溢出 ${JSON.stringify(overflow.worst)}`
  ).toBeLessThanOrEqual(1)

  // 宽内容（年度科目表、分析全文里的表格）必须在**自己的容器里**横向滚动，
  // 而不是把整页撑宽
  const scrollers = await page.evaluate(() => {
    const results: string[] = []
    for (const el of document.querySelectorAll('body *')) {
      if (el.scrollWidth > el.clientWidth + 1) {
        const style = getComputedStyle(el)
        results.push(`${el.tagName}.${String(el.className).slice(0, 40)}:${style.overflowX}`)
      }
    }
    return results
  })
  // 宽内容必须真实存在，否则这条断言是空转的（12 列科目表在 375px 下必然超宽）
  expect(scrollers.length, '页面上没有任何超宽内容，桩数据可能没渲染出来').toBeGreaterThan(0)
  const trapped = scrollers.filter((item) => /auto|scroll/.test(item.split(':').pop()!))
  expect(trapped.length, `宽内容没有被关进可滚动容器: ${scrollers.join(' | ')}`).toBeGreaterThan(0)

  // 关键操作按钮不得被挤出屏幕
  for (const label of ['重新分析', '刷新']) {
    const button = page.getByRole('button', { name: new RegExp(label) }).first()
    if (await button.count()) {
      const box = await button.boundingBox()
      if (box) {
        expect(box.x + box.width, `${label} 按钮右缘超出视口`).toBeLessThanOrEqual(viewport.width)
      }
    }
  }
})

test('mobile holding card title navigates to the security detail page', async ({
  page,
  request
}) => {
  // 桌面端标题是 el-link，移动端此前是纯 span——手机上没有任何入口能进标的档案，
  // AI 分析与财报摘要在手机上根本打不开。
  const token = await loginThroughApi(request)
  await seedData(request, token)
  await setSession(page, token)

  await page.goto('/holdings')
  await page.waitForLoadState('networkidle')

  const title = page.getByTestId('holding-card-title').first()
  await expect(title, '移动端持仓卡片缺少可点击标题').toBeVisible()

  // 拇指可靠点击的最小区域
  const box = await title.boundingBox()
  expect(box!.height, `标题点击区仅 ${Math.round(box!.height)}px`).toBeGreaterThanOrEqual(44)

  // 断言跳到的是**这张卡自己**的标的：e2e 库被多个 spec 共用，首张卡片是谁
  // 取决于跑了哪些用例，写死代码会让这条测试依赖执行顺序
  const symbol = (await title.getByTestId('holding-card-symbol').innerText()).trim()
  const market = (
    await page.getByTestId('holding-card').first().locator('.el-tag').first().innerText()
  ).trim()

  await title.click()
  await page.waitForURL(/\/securities\//)
  expect(decodeURIComponent(new URL(page.url()).pathname)).toBe(`/securities/${market}/${symbol}`)
})

test('mobile holding card without a name still has a usable accessible name', async ({
  page,
  request
}) => {
  // `Holding.name` 在 schema 与 TS 类型里都是可空的，模板串会生成
  // "查看 null 的标的档案"——读屏用户听到的是个错误名字。
  //
  // 用 route 桩而不是 seed：holding_service 在插入时写的是 `name or symbol`，
  // 经公开 API 造不出 name=null 的持仓；但列是 nullable、类型也声明可空，
  // 直接改库或历史行仍会出现，前端不能假设它有值。
  const token = await loginThroughApi(request)
  await setSession(page, token)
  await page.route('**/api/holdings*', async (route) => {
    await route.fulfill({
      json: [
        {
          id: 9001,
          symbol: '688981',
          name: null,
          market: 'A股',
          broker_account_id: null,
          quantity: 100,
          avg_cost: 50,
          total_cost: 5000,
          currency: 'CNY'
        }
      ]
    })
  })

  await page.goto('/holdings')
  await page.waitForLoadState('networkidle')

  const title = page.getByTestId('holding-card-title').first()
  const label = (await title.getAttribute('aria-label')) || ''
  expect(label, `无障碍名称落到了占位值: ${label}`).not.toMatch(/null|undefined/)
  expect(label).toContain('688981')
  // 视觉上不留一行空白的名称占位
  await expect(title.locator('.mobile-card-name')).toHaveCount(0)

  await title.click()
  await page.waitForURL(/\/securities\//)
  expect(decodeURIComponent(new URL(page.url()).pathname)).toBe('/securities/A股/688981')
})
