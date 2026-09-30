/**
 * README 截图与演示视频（docs/media/README.md 有完整步骤）。
 *
 *   npx playwright test -c playwright.demo.config.ts            # 全部
 *   npx playwright test -c playwright.demo.config.ts -g 截图    # 只截图
 *
 * 截图直接写 docs/media/*.png；视频写 demo-output/videos/*.webm，再由
 * scripts/demo-media.sh 转成 README 用的 GIF 与 Release 附件 MP4。
 */
import { expect, test, type Locator, type Page } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const API = 'http://127.0.0.1:18100'
const HERE = path.dirname(fileURLToPath(import.meta.url))
const MEDIA_DIR = path.resolve(HERE, '../../docs/media')
const VIDEO_DIR = path.resolve(HERE, '../demo-output/videos')
const DEMO_USER = { username: 'demo', password: process.env.DEMO_PASSWORD || 'demo-password' }
const RESEARCH = '/securities/A股/600900'

fs.mkdirSync(MEDIA_DIR, { recursive: true })
fs.mkdirSync(VIDEO_DIR, { recursive: true })

async function signIn(page: Page) {
  const response = await page.request.post(`${API}/api/auth/token`, { data: DEMO_USER })
  expect(response.ok()).toBeTruthy()
  const { access_token: token } = await response.json()
  const me = await (
    await page.request.get(`${API}/api/auth/me`, { headers: { Authorization: `Bearer ${token}` } })
  ).json()
  await page.context().addCookies([
    { name: 'investment_session', value: token, url: API, httpOnly: true, sameSite: 'Strict' },
    { name: 'investment_csrf', value: 'demo-csrf', url: API, sameSite: 'Strict' }
  ])
  await page.addInitScript((user) => localStorage.setItem('user', JSON.stringify(user)), me)
}

/** 等页面数据与图表落定：网络空闲 + 骨架屏/加载遮罩消失 + 一帧动画 */
async function settle(page: Page) {
  await page.waitForLoadState('networkidle')
  await expect(page.locator('.el-loading-mask:visible')).toHaveCount(0, { timeout: 30000 })
  await expect(page.locator('.el-skeleton:visible')).toHaveCount(0, { timeout: 30000 })
  await page.waitForTimeout(900) // echarts 入场动画
}

async function shot(page: Page, name: string, options: { fullPage?: boolean } = {}) {
  await settle(page)
  await page.screenshot({ path: path.join(MEDIA_DIR, `${name}.png`), fullPage: options.fullPage })
}

async function openTab(page: Page, label: string) {
  await page.getByRole('tab', { name: label, exact: true }).click()
  await settle(page)
}

// ---------------------------------------------------------------- 截图 ---

test.describe('截图', () => {
  test.beforeEach(async ({ page }) => signIn(page))

  test('桌面端', async ({ page }) => {
    await page.goto('/')
    await expect(page.locator('.dashboard-chart canvas')).toBeVisible({ timeout: 30000 })
    await shot(page, 'dashboard')

    await page.goto('/holdings')
    await shot(page, 'holdings')

    await page.goto('/statistics')
    await expect(page.locator('canvas').first()).toBeVisible({ timeout: 60000 })
    await settle(page)
    // 收益曲线与风险指标在第二屏：页面在内层容器里滚动，用 scrollIntoView 定位到卡片顶部
    await page
      .getByText('证券组合 TTWR 与风险指标')
      .evaluate((el) => el.closest('.el-card')?.scrollIntoView({ block: 'start' }))
    await shot(page, 'statistics')

    await page.goto('/transactions')
    await shot(page, 'transactions')

    await page.goto('/watchlist')
    await shot(page, 'watchlist')

    await page.goto(RESEARCH)
    await shot(page, 'security-analysis')
    await page
      .getByTestId('graham-screen-section')
      .evaluate((el) => el.scrollIntoView({ block: 'start' }))
    await shot(page, 'security-graham')
    await openTab(page, '基本面')
    await shot(page, 'security-fundamentals')
    await openTab(page, '报表')
    await shot(page, 'security-statements')
    await openTab(page, '公告')
    await shot(page, 'security-announcements')

    await openImportPreview(page)
    await shot(page, 'import-preview')

    await page.goto('/account-data')
    await openReconciliationDiff(page)
    await shot(page, 'reconciliation')
  })

  test('移动端', async ({ browser }) => {
    const context = await browser.newContext({
      viewport: { width: 393, height: 852 },
      deviceScaleFactor: 2,
      isMobile: true,
      hasTouch: true,
      locale: 'zh-CN',
      timezoneId: 'Asia/Shanghai'
    })
    const page = await context.newPage()
    await signIn(page)
    for (const [route, name] of [
      ['/', 'mobile-dashboard'],
      ['/holdings', 'mobile-holdings'],
      [RESEARCH, 'mobile-security']
    ]) {
      await page.goto(route)
      await shot(page, name)
    }
    await context.close()
  })
})

// ---------------------------------------------------------------- 视频 ---

/** 无头录屏没有鼠标指针：注入一个跟随 mousemove 的圆点，操作才看得出来 */
const CURSOR_SCRIPT = () => {
  const install = () => {
    const dot = document.createElement('div')
    dot.style.cssText =
      'position:fixed;z-index:2147483647;width:18px;height:18px;margin:-9px 0 0 -9px;' +
      'border-radius:50%;background:rgba(64,158,255,.35);border:2px solid #409eff;' +
      'pointer-events:none;transition:transform .12s;left:-40px;top:-40px'
    document.body.appendChild(dot)
    document.addEventListener('mousemove', (e) => {
      dot.style.left = `${e.clientX}px`
      dot.style.top = `${e.clientY}px`
    })
    document.addEventListener('mousedown', () => (dot.style.transform = 'scale(.7)'))
    document.addEventListener('mouseup', () => (dot.style.transform = 'scale(1)'))
  }
  if (document.body) install()
  else document.addEventListener('DOMContentLoaded', install)
}

async function glideClick(page: Page, target: Locator) {
  await target.scrollIntoViewIfNeeded()
  const box = await target.boundingBox()
  if (!box) throw new Error('目标不可见')
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 25 })
  await page.waitForTimeout(250)
  await target.click()
}

async function smoothScroll(page: Page, distance: number) {
  for (let i = 0; i < 12; i += 1) {
    await page.mouse.wheel(0, distance / 12)
    await page.waitForTimeout(60)
  }
}

async function record(
  browser: import('@playwright/test').Browser,
  name: string,
  scenario: (page: Page) => Promise<void>
) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    recordVideo: { dir: VIDEO_DIR, size: { width: 1280, height: 800 } },
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai'
  })
  const page = await context.newPage()
  await page.addInitScript(CURSOR_SCRIPT)
  await signIn(page)
  await scenario(page)
  await page.waitForTimeout(800)
  const video = page.video()
  await context.close()
  await video?.saveAs(path.join(VIDEO_DIR, `${name}.webm`))
  await video?.delete()
}

test.describe('视频', () => {
  test('看板导览', async ({ browser }) => {
    await record(browser, 'tour', async (page) => {
      await page.goto('/')
      await settle(page)
      await page.mouse.move(640, 300, { steps: 20 })
      await page.waitForTimeout(1200)
      await glideClick(page, page.locator('.el-menu-item', { hasText: '当前持仓' }).first())
      await settle(page)
      await page.waitForTimeout(1200)
      await glideClick(page, page.locator('.el-menu-item', { hasText: '统计分析' }).first())
      await settle(page)
      await smoothScroll(page, 500)
      await page.waitForTimeout(1500)
    })
  })

  test('标的研究', async ({ browser }) => {
    await record(browser, 'research', async (page) => {
      await page.goto(RESEARCH)
      await settle(page)
      await page.waitForTimeout(1200)
      await smoothScroll(page, 400)
      for (const tab of ['基本面', '报表', '公告']) {
        await glideClick(page, page.getByRole('tab', { name: tab, exact: true }))
        await settle(page)
        await page.waitForTimeout(1000)
      }
    })
  })

  test('完整演示', async ({ browser }) => {
    await record(browser, 'walkthrough', async (page) => {
      await page.goto('/')
      await settle(page)
      await page.waitForTimeout(1500)
      for (const menu of ['当前持仓', '统计分析', '交易记录']) {
        await glideClick(page, page.locator('.el-menu-item', { hasText: menu }).first())
        await settle(page)
        await smoothScroll(page, 400)
        await page.waitForTimeout(1200)
      }
      await page.goto(RESEARCH)
      await settle(page)
      for (const tab of ['基本面', '报表', '公告']) {
        await glideClick(page, page.getByRole('tab', { name: tab, exact: true }))
        await settle(page)
        await page.waitForTimeout(1200)
      }
      await openImportPreview(page, { glide: true })
      await page.waitForTimeout(2500)
      await page.keyboard.press('Escape')
      await page.goto('/account-data')
      await openReconciliationDiff(page, { glide: true })
      await page.waitForTimeout(2500)
    })
  })
})

// ------------------------------------------------------------- 共用流程 ---

const IBKR_DEMO_CSV = [
  'Statement,Header,域名称,域值',
  'Statement,Data,Title,Transaction History',
  '总结,Header,域名称,域值',
  '总结,Data,基础货币,USD',
  'Transaction History,Header,日期,账户,说明,交易类型,代码,数量,价格,Price Currency,总额,佣金,净额',
  'Transaction History,Data,2026-09-28,U***1357,电子资金转账,存款,-,-,-,-,5000.0,-,5000.0',
  'Transaction History,Data,2026-09-29,U***1357,NVIDIA CORP,买,NVDA,20.0,180.25,USD,-3605.0,-1.0,-3606.0',
  'Transaction History,Data,2026-09-29,U***1357,USD 贷方利息- 九月-2026,贷方利息,-,-,-,-,12.34,-,12.34'
].join('\n')

async function openImportPreview(page: Page, options: { glide?: boolean } = {}) {
  const click = (target: Locator) => (options.glide ? glideClick(page, target) : target.click())
  if (!page.url().endsWith('/transactions')) {
    await page.goto('/transactions')
    await settle(page)
  }
  await click(page.getByRole('button', { name: '导入' }))
  const dialog = page.locator('.el-dialog', { hasText: '导入数据' })
  await click(dialog.getByRole('tab', { name: 'IBKR 活动报表' }))
  await click(dialog.locator('.import-account-field .el-select'))
  await click(
    page.locator('.el-select-dropdown:visible .el-select-dropdown__item', { hasText: 'IBKR' })
  )
  await dialog.locator('input[type=file]').setInputFiles({
    name: 'ibkr-activity-demo.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from(IBKR_DEMO_CSV, 'utf8')
  })
  await click(dialog.getByRole('button', { name: /预览/ }))
  const title = dialog.getByTestId('import-preview-title')
  await expect(title).toBeVisible({ timeout: 30000 })
  await expect(page.locator('.el-message')).toHaveCount(0, { timeout: 10000 }) // 等「预览完成」提示消失
  // 预览表格在弹窗下半截：把整块预览的底边滚进视口
  await dialog.locator('.import-preview').evaluate((el) => el.scrollIntoView({ block: 'end' }))
}

async function openReconciliationDiff(page: Page, options: { glide?: boolean } = {}) {
  const click = (target: Locator) => (options.glide ? glideClick(page, target) : target.click())
  await settle(page)
  await click(page.getByRole('tab', { name: /月末核对/ }))
  await settle(page)
  await click(page.getByRole('button', { name: /有差异，查看差异明细/ }).first())
  await expect(page.locator('.el-dialog', { hasText: '对账比对详情' })).toBeVisible()
}
