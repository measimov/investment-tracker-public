import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { adminUser, loginThroughApi, setAuthenticatedSession } from './helpers'
const fixture = JSON.parse(
  readFileSync(new URL('../../docs/media/user-management-ui-fixture.json', import.meta.url), 'utf8')
)
test.use({ timezoneId: 'Asia/Shanghai' })

test('用户初载失败不假空，GET重试确认真实空与列表，刷新失败保留身份', async ({ page, request }) => {
  await setAuthenticatedSession(page, await loginThroughApi(request, adminUser))
  let reads = 0
  await page.route('**/api/users', (route) => {
    reads++
    return route.fulfill(
      reads === 1 || reads === 4
        ? { status: 503, json: { detail: '明确虚构用户读取失败' } }
        : { json: reads === 2 ? [] : fixture.users }
    )
  })
  await page.goto('/admin/users')
  const list = page.getByRole('region', { name: '用户列表', exact: true })
  await expect(list).toContainText('用户列表尚未加载成功')
  await expect(list).not.toContainText('暂无用户')
  await page.getByRole('button', { name: '重试加载用户列表', exact: true }).click()
  await expect(list).toContainText('暂无用户')
  await page.getByRole('button', { name: '重新加载用户列表', exact: true }).click()
  await expect(list).toContainText('3 位用户')
  await expect(list).toContainText('2026/10/03')
  await expect(list).toContainText(fixture.users[0].username)
  await page.getByRole('button', { name: '重新加载用户列表', exact: true }).click()
  await expect(list).toContainText('保留上次成功的用户列表')
  await expect(list).toContainText(fixture.users[0].username)
  expect(reads).toBe(4)
})

test('具名表单保留校验、邮箱null与严格密码payload，mask保护和关闭清理', async ({
  page,
  request
}) => {
  await page.setViewportSize({ width: 393, height: 852 })
  await setAuthenticatedSession(page, await loginThroughApi(request, adminUser))
  const writes: { method: string; path: string; body: unknown }[] = []
  await page.route('**/api/users**', (route) => {
    const req = route.request()
    if (req.method() === 'GET') return route.fulfill({ json: fixture.users })
    writes.push({
      method: req.method(),
      path: new URL(req.url()).pathname,
      body: req.postDataJSON()
    })
    return route.fulfill({ json: fixture.users[0] })
  })
  await page.goto('/admin/users')
  const opener = page.getByRole('button', { name: '添加用户', exact: true })
  await opener.focus()
  await page.keyboard.press('Enter')
  let dialog = page.getByRole('dialog', { name: '添加用户', exact: true })
  await dialog.getByRole('textbox', { name: '用户名', exact: true }).fill('ui_fiction_new')
  await dialog.getByRole('textbox', { name: '密码', exact: true }).fill('short')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog).toContainText('密码长度至少10位')
  expect(writes).toHaveLength(0)
  await dialog.getByRole('textbox', { name: '密码', exact: true }).fill('ui-fiction-password')
  await expect(dialog.getByRole('checkbox', { name: '激活状态', exact: true })).toBeChecked()
  await expect(dialog.getByRole('checkbox', { name: '管理员权限', exact: true })).not.toBeChecked()
  for (const name of ['激活状态', '管理员权限']) {
    const checkbox = dialog.getByRole('checkbox', { name, exact: true })
    const original = name === '激活状态'
    await checkbox.focus()
    await page.keyboard.press('Space')
    await expect(checkbox).toHaveAttribute('aria-checked', String(!original))
    await expect(checkbox).toBeChecked({ checked: !original })
    await page.keyboard.press('Space')
    await expect(checkbox).toHaveAttribute('aria-checked', String(original))
    await expect(checkbox).toBeChecked({ checked: original })
  }
  await page.locator('.el-overlay:visible').click({ position: { x: 2, y: 2 } })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('textbox', { name: '用户名', exact: true })).toHaveValue(
    'ui_fiction_new'
  )
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog).toBeHidden()
  expect(writes[0]).toEqual({
    method: 'POST',
    path: '/api/users',
    body: {
      username: 'ui_fiction_new',
      email: null,
      password: 'ui-fiction-password',
      is_active: true,
      is_admin: false
    }
  })
  await expect(opener).toBeFocused()
  await opener.click()
  await expect(
    page
      .getByRole('dialog', { name: '添加用户' })
      .getByRole('textbox', { name: '密码', exact: true })
  ).toHaveValue('')
  await page.keyboard.press('Escape')
  await expect(opener).toBeFocused()
  const edit = page.getByRole('button', {
    name: `编辑用户 ${fixture.users[2].username}`,
    exact: true
  })
  await edit.click()
  dialog = page.getByRole('dialog', { name: '编辑用户', exact: true })
  await expect(dialog.getByRole('textbox', { name: '用户名', exact: true })).toBeDisabled()
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog).toBeHidden()
  expect(writes[1]).toEqual({
    method: 'PUT',
    path: `/api/users/${fixture.users[2].id}`,
    body: { email: null, is_active: false, is_admin: false }
  })
  const reset = page.getByRole('button', {
    name: `重置用户 ${fixture.users[0].username} 的密码`,
    exact: true
  })
  await reset.click()
  dialog = page.getByRole('dialog', { name: '重置密码', exact: true })
  await dialog.getByRole('textbox', { name: '新密码', exact: true }).fill('ui-fiction-new-password')
  await dialog.getByRole('textbox', { name: '确认密码', exact: true }).fill('ui-fiction-different')
  await dialog.getByRole('button', { name: '重置密码', exact: true }).click()
  await expect(dialog).toContainText('两次输入的密码不一致')
  expect(writes).toHaveLength(2)
  await dialog
    .getByRole('textbox', { name: '确认密码', exact: true })
    .fill('ui-fiction-new-password')
  await dialog.getByRole('button', { name: '重置密码', exact: true }).click()
  await expect(dialog).toBeHidden()
  expect(writes[2]).toEqual({
    method: 'PUT',
    path: `/api/users/${fixture.users[0].id}/password`,
    body: { new_password: 'ui-fiction-new-password' }
  })
  await expect(reset).toBeFocused()
  await reset.click()
  await expect(
    page
      .getByRole('dialog', { name: '重置密码' })
      .getByRole('textbox', { name: '新密码', exact: true })
  ).toHaveValue('')
  await page.keyboard.press('Escape')
  await expect(reset).toBeFocused()
})

test('创建时间排序按钮真实Enter循环，原生手机完整资料无横溢', async ({ page, request }) => {
  await setAuthenticatedSession(page, await loginThroughApi(request, adminUser))
  await page.route('**/api/users', (route) => route.fulfill({ json: fixture.users }))
  await page.goto('/admin/users')
  const sort = page.getByRole('button', { name: '创建时间排序：未排序', exact: true })
  await sort.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByTestId('users-table').getByRole('row').nth(1)).toContainText(
    fixture.users[2].username
  )
  await page.keyboard.press('Enter')
  await expect(page.getByTestId('users-table').getByRole('row').nth(1)).toContainText(
    fixture.users[0].username
  )
  await page.keyboard.press('Enter')
  await expect(sort).toHaveAccessibleName('创建时间排序：未排序')
  await page.setViewportSize({ width: 320, height: 852 })
  const card = page.getByTestId('user-card').first()
  await expect(card).toContainText(fixture.users[0].username)
  await expect(card).toContainText(fixture.users[0].email)
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320)
})

test('两种保存中焦点与busy保持，重复pointer和Enter仅一次mock写入', async ({ page, request }) => {
  await setAuthenticatedSession(page, await loginThroughApi(request, adminUser))
  let writes = 0,
    release!: () => void
  await page.route('**/api/users**', async (route) => {
    if (route.request().method() === 'GET') return route.fulfill({ json: fixture.users })
    writes++
    await new Promise<void>((resolve) => (release = resolve))
    return route.fulfill({ json: fixture.users[0] })
  })
  await page.goto('/admin/users')
  for (const reset of [false, true]) {
    const opener = page.getByRole('button', {
      name: reset ? `重置用户 ${fixture.users[0].username} 的密码` : '添加用户',
      exact: true
    })
    await opener.focus()
    await page.keyboard.press('Enter')
    const dialog = page.getByRole('dialog', { name: reset ? '重置密码' : '添加用户', exact: true })
    if (reset) {
      await dialog.getByRole('textbox', { name: '新密码', exact: true }).fill('ui-fiction-password')
      await dialog
        .getByRole('textbox', { name: '确认密码', exact: true })
        .fill('ui-fiction-password')
    } else {
      await dialog.getByRole('textbox', { name: '用户名', exact: true }).fill('ui_fiction_busy')
      await dialog.getByRole('textbox', { name: '密码', exact: true }).fill('ui-fiction-password')
    }
    const save = dialog.getByRole('button', { name: reset ? '重置密码' : '保存', exact: true })
    await save.click()
    await expect.poll(() => writes).toBe(reset ? 2 : 1)
    await expect(save).toHaveAttribute('aria-busy', 'true')
    await expect(save).toHaveAttribute('aria-disabled', 'true')
    await expect(save).toBeFocused()
    await expect(dialog.getByRole('button', { name: '取消', exact: true })).toBeDisabled()
    await save.click({ force: true })
    await page.keyboard.press('Enter')
    await page.keyboard.press('Escape')
    await expect(dialog).toBeVisible()
    expect(writes).toBe(reset ? 2 : 1)
    release()
    await expect(dialog).toBeHidden()
    await expect(opener).toBeFocused()
  }
})
