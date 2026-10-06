// 应用外壳（#219）：修改密码、导航「更多」高亮、嵌套路由高亮、标签页标题、404。
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const API = 'http://127.0.0.1:18000'
const user = { username: 'demo', password: 'e2e-user-password' }
const adminUser = { username: 'admin', password: 'e2e-admin-password' }

async function loginThroughApi(
  request: APIRequestContext,
  credentials: { username: string; password: string }
) {
  return request.post(`${API}/api/auth/token`, { data: credentials })
}

async function loginThroughUi(page: Page, credentials: { username: string; password: string }) {
  await page.goto('/login')
  await page.getByPlaceholder('请输入用户名').fill(credentials.username)
  await page.getByPlaceholder('请输入密码').fill(credentials.password)
  await page.getByRole('button', { name: '登录' }).click()
  await expect(page).toHaveURL(/\/$/)
}

test('user changes their own password from the user menu and must log in again', async ({
  page,
  request
}) => {
  const adminLogin = await loginThroughApi(request, adminUser)
  expect(adminLogin.ok()).toBeTruthy()
  const adminHeaders = { Authorization: `Bearer ${(await adminLogin.json()).access_token}` }
  const username = `pwd_e2e_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
  const oldPassword = 'old-e2e-password'
  const newPassword = 'new-e2e-password-2'
  const created = await request.post(`${API}/api/users`, {
    headers: adminHeaders,
    data: { username, password: oldPassword, is_active: true, is_admin: false }
  })
  expect(created.ok()).toBeTruthy()
  const createdUser = await created.json()

  try {
    await loginThroughUi(page, { username, password: oldPassword })

    await page.getByRole('button', { name: '设置', exact: true }).click()
    await page.getByRole('button', { name: '修改密码', exact: true }).click()
    const dialog = page.locator('.el-dialog', { hasText: '修改密码' })
    await expect(dialog).toBeVisible()

    // 原密码错误：中文提示（后端 detail 已本地化），会话保持
    await dialog.getByPlaceholder('请输入当前密码').fill('definitely-wrong')
    await dialog.getByPlaceholder('至少 10 位').fill(newPassword)
    await dialog.getByPlaceholder('请再次输入新密码').fill(newPassword)
    await dialog.getByRole('button', { name: '确认修改' }).click()
    await expect(page.locator('.el-message').filter({ hasText: '原密码不正确' })).toBeVisible()
    await expect(dialog).toBeVisible()

    await dialog.getByPlaceholder('请输入当前密码').fill(oldPassword)
    await dialog.getByRole('button', { name: '确认修改' }).click()
    await expect(page).toHaveURL(/\/login/)
    await expect(
      page.locator('.el-message').filter({ hasText: '已退出所有会话，请重新登录' })
    ).toBeVisible()

    // 旧密码失效、新密码可用
    expect((await loginThroughApi(request, { username, password: oldPassword })).status()).toBe(401)
    expect((await loginThroughApi(request, { username, password: newPassword })).ok()).toBeTruthy()

    // 页面上的旧会话已被吊销：访问受保护页被守卫送回登录页（只跳一次，提示留得住）
    await page.goto('/holdings')
    await expect(page).toHaveURL(/\/login\?redirect=(%2F|\/)holdings/)
    await expect(page.locator('.el-message').filter({ hasText: '请先登录' })).toBeVisible()
  } finally {
    const removed = await request.delete(`${API}/api/users/${createdUser.id}`, {
      headers: adminHeaders
    })
    expect(removed.ok()).toBeTruthy()
  }
})

test('navigation highlights primary, other and nested routes, sets titles, and has a 404', async ({
  page
}) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await loginThroughUi(page, user)
  await expect(page).toHaveTitle('仪表盘 · 投资追踪系统')
  const menu = page.getByRole('navigation', { name: '主导航' })
  await expect(menu.getByRole('link', { name: '当前持仓', exact: true })).toBeVisible()
  await menu.getByRole('link', { name: '公司行动', exact: true }).click()
  await expect(page).toHaveURL(/\/corporate-actions$/)
  await expect(page).toHaveTitle('公司行动 · 投资追踪系统')
  await expect(menu.getByRole('link', { name: '公司行动', exact: true })).toHaveAttribute(
    'aria-current',
    'page'
  )
  await page.goto('/securities/A股/600000')
  await expect(page).toHaveTitle('600000 · 标的档案 · 投资追踪系统')
  await expect(menu.getByRole('link', { name: '当前持仓', exact: true })).toHaveAttribute(
    'aria-current',
    'page'
  )
  await expect(menu.getByRole('link', { name: '公司行动', exact: true })).not.toHaveAttribute(
    'aria-current',
    'page'
  )
  await page.goto('/no-such-page')
  await expect(page).toHaveTitle('页面不存在 · 投资追踪系统')
  await expect(page.getByText('页面不存在', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '返回首页' }).click()
  await expect(page).toHaveURL(/\/$/)
})

// #268：A 登出、B 在同一页面（不重载）登录后，持仓页不得复用 A 的缓存
test('switching users without a page reload never shows the previous user holdings', async ({
  page,
  request
}) => {
  const adminLogin = await loginThroughApi(request, adminUser)
  expect(adminLogin.ok()).toBeTruthy()
  const adminHeaders = { Authorization: `Bearer ${(await adminLogin.json()).access_token}` }
  const suffix = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
  const password = 'switch-e2e-password'
  const users: Array<{ id: number; username: string }> = []
  for (const name of ['alice', 'bob']) {
    const created = await request.post(`${API}/api/users`, {
      headers: adminHeaders,
      data: { username: `${name}_${suffix}`, password, is_active: true, is_admin: false }
    })
    expect(created.ok()).toBeTruthy()
    users.push(await created.json())
  }
  const [alice, bob] = users
  const aliceSymbol = `SW${suffix.slice(-6).toUpperCase()}`

  try {
    const aliceToken = (
      await (await loginThroughApi(request, { username: alice.username, password })).json()
    ).access_token
    const bought = await request.post(`${API}/api/transactions`, {
      headers: { Authorization: `Bearer ${aliceToken}` },
      data: {
        symbol: aliceSymbol,
        name: 'Alice 专属持仓',
        market: '美股',
        transaction_type: 'BUY',
        quantity: 10,
        price: 5,
        fee: 0,
        transaction_date: '2026-01-05',
        currency: 'USD'
      }
    })
    expect(bought.ok()).toBeTruthy()

    await loginThroughUi(page, { username: alice.username, password })
    await page
      .getByRole('navigation', { name: '主导航' })
      .getByRole('link', { name: '当前持仓', exact: true })
      .click()
    await expect(page.getByText(aliceSymbol).first()).toBeVisible()

    // 登出并在同一个页面里登录 B：Login 走 router.push，不整页刷新，Pinia 状态会留下来
    await page.getByRole('button', { name: '设置', exact: true }).click()
    await page.getByRole('button', { name: '退出登录', exact: true }).click()
    await expect(page).toHaveURL(/\/login/)
    await page.getByPlaceholder('请输入用户名').fill(bob.username)
    await page.getByPlaceholder('请输入密码').fill(password)
    await page.getByRole('button', { name: '登录' }).click()
    await expect(page).toHaveURL(/\/$/)

    await page
      .getByRole('navigation', { name: '主导航' })
      .getByRole('link', { name: '当前持仓', exact: true })
      .click()
    await expect(page.getByText('暂无持仓数据').first()).toBeVisible()
    await expect(page.getByText(aliceSymbol)).toHaveCount(0)
  } finally {
    for (const created of users) {
      await request.delete(`${API}/api/users/${created.id}`, { headers: adminHeaders })
    }
  }
})

// 长不存在路径原先被全局溢出裁掉，且缺少实际 H1；未登录返回仍沿原路由守卫。
test('anonymous long 404 preserves the whole path and keyboard return at narrow and desktop widths', async ({
  page
}) => {
  const path = '/ui-public-nonexistent_' + 'long_public_path_'.repeat(40)
  for (const width of [320, 1440]) {
    await page.setViewportSize({ width, height: 852 })
    await page.goto(path)
    await expect(
      page.getByRole('heading', { name: '页面不存在', level: 1, exact: true })
    ).toBeVisible()
    await expect(page.locator('h1')).toHaveCount(1)
    await expect(page.locator('.el-result__subtitle')).toHaveText(`没有找到「${path}」对应的页面`)
    await expect
      .poll(() =>
        page.evaluate(() =>
          Math.max(document.body.scrollWidth, document.documentElement.scrollWidth)
        )
      )
      .toBeLessThanOrEqual(width)
    const home = page.getByRole('button', { name: '返回首页', exact: true })
    await home.focus()
    await home.press('Enter')
    await expect(page).toHaveURL(/\/login(?:\?|$)/)
    await expect(
      page.getByRole('heading', { name: '投资追踪系统', level: 1, exact: true })
    ).toBeVisible()
  }
})
