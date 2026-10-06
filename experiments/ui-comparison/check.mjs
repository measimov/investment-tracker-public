import { chromium } from '../../frontend/node_modules/playwright/index.mjs'
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

export const baseURL = process.env.UI_BENCH_BASE_URL || 'http://127.0.0.1:4318'
export const resultsDir = path.join(path.dirname(fileURLToPath(import.meta.url)), 'results')
export const variants = (
  process.argv.find((arg) => arg.startsWith('--variants='))?.split('=')[1] || 'element,naive,nuxt'
).split(',')
for (const variant of variants)
  assert(['element', 'naive', 'nuxt'].includes(variant), `Unknown variant: ${variant}`)

export async function ready(page, variant, rows = 50) {
  await page.goto(`${baseURL}/${variant}/?rows=${rows}`, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () =>
      document.documentElement.dataset.ready === 'true' &&
      typeof window.__uiBench?.snapshot === 'function'
  )
}

export const snapshot = (page) => page.evaluate(() => window.__uiBench.snapshot())
export const twoFrames = (page) =>
  page.evaluate(
    () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
  )

async function input(page, id) {
  const field = page.getByTestId(id)
  await field.waitFor({ state: 'visible' })
  return (await field.evaluate((el) => /^(INPUT|TEXTAREA)$/.test(el.tagName)))
    ? field
    : field.locator('input,textarea').first()
}

async function fill(page, id, value) {
  const field = await input(page, id)
  await field.fill(String(value))
  await field.press('Tab')
  await twoFrames(page)
}

async function date(page, id, value) {
  const root = page.getByTestId(id)
  if (await root.locator('[data-reka-date-field-segment="year"]').count()) {
    const [year, month, day] = value.split('-')
    for (const [part, text] of [
      ['year', year],
      ['month', month],
      ['day', day]
    ]) {
      const segment = root.locator(`[data-reka-date-field-segment="${part}"]`)
      await segment.click()
      // Focusing a Reka segment selects its value. Backspace only removes one
      // digit and changes its numeric-input buffer, so type the replacement.
      await segment.pressSequentially(text)
    }
    await root.locator('[data-reka-date-field-segment="day"]').press('Tab')
    await twoFrames(page)
    return
  }
  const field = await input(page, id)
  await field.fill(value)
  // Date-picker models commonly commit a typed value on Enter/blur.
  if ((await field.getAttribute('type')) !== 'date') await field.press('Enter')
  await field.press('Tab')
  await twoFrames(page)
}

async function choose(page, id, label, value) {
  const field = page.getByTestId(id)
  if (await field.evaluate((el) => el.tagName === 'SELECT')) {
    await field.selectOption(String(value))
  } else if (await field.locator('select').count()) {
    await field.locator('select').selectOption(String(value))
  } else {
    await field.click()
    const option = page.getByRole('option', { name: label, exact: true })
    if (await option.count()) {
      await option.first().click()
    } else {
      // Naive uses its own option DOM for some select configurations.
      await page.locator('.n-base-select-option:visible').filter({ hasText: label }).first().click()
    }
  }
  await twoFrames(page)
}

async function waitState(page, predicate, description) {
  const deadline = Date.now() + 4000
  let state
  do {
    state = await snapshot(page)
    if (predicate(state)) return state
    await page.waitForTimeout(30)
  } while (Date.now() < deadline)
  assert.fail(`${description}; last snapshot: ${JSON.stringify(state)}`)
}

async function openDialog(page) {
  await page.getByTestId('open-trade').click()
  await page.getByTestId('trade-dialog').waitFor({ state: 'visible' })
  await waitState(page, (state) => state.open, 'Trade dialog did not open')
}

async function expectError(page, field, text) {
  await waitState(page, (state) => state.errors[field] === text, `Missing validation for ${field}`)
  const message = page.getByText(text, { exact: true }).first()
  await message.waitFor({ state: 'visible', timeout: 3000 })
  const visible = await message.isVisible()
  assert(visible, `Validation exists in state but is not visible: ${text}`)
}

async function focusInside(page) {
  return page.getByTestId('trade-dialog').evaluate((el) => {
    const dialog = el.closest('[role="dialog"],[role="alertdialog"]') || el
    return dialog.contains(document.activeElement)
  })
}

async function returnedFocus(page) {
  await page.waitForFunction(
    () => {
      const trigger = document.querySelector('[data-testid="open-trade"]')
      return trigger?.contains(document.activeElement)
    },
    undefined,
    { timeout: 2000 }
  )
}

async function toggleExpand(page, variant, id) {
  const explicit = page.getByTestId(`expand-row-${id}`)
  if (await explicit.count()) return explicit.click()
  // Built-in expand columns generate their own trigger DOM.
  const row = page.getByTestId(`price-${id}`).locator('xpath=ancestor::tr[1]')
  if (variant === 'element') return row.locator('.el-table__expand-icon').click()
  if (variant === 'naive') return row.locator('.n-data-table-expand-trigger').click()
  throw new Error(`Missing accessible expansion trigger for row ${id}`)
}

const tests = [
  {
    name: 'financial-display',
    async run(page, variant) {
      assert.equal((await snapshot(page)).count, 50)
      for (const [id, expected] of [
        [1, '0.085'],
        [2, '1.409'],
        [3, '—'],
        [4, '0.00']
      ]) {
        const text = (await page.getByTestId(`price-${id}`).innerText()).split('\n')[0].trim()
        assert.equal(text, expected, `Price ${id}`)
      }
      const table = await page.getByTestId('holding-table').innerText()
      assert(table.includes('折 CNY —'), 'Missing FX must remain unknown')
      await page.screenshot({
        path: path.join(resultsDir, 'screenshots', `${variant}-desktop.png`)
      })
    }
  },
  {
    name: 'search-reset-and-no-results',
    async run(page) {
      await fill(page, 'search-input', '00001')
      assert.deepEqual(
        (await waitState(page, (state) => state.count === 1, 'Search did not narrow')).ids,
        [1]
      )
      assert(await page.getByTestId('price-1').isVisible())
      await fill(page, 'search-input', '不存在的测试代码')
      await waitState(page, (state) => state.count === 0, 'No-results search did not clear rows')
      assert.equal(await page.locator('[data-testid^="price-"]').count(), 0)
      await page.getByTestId('reset-filters').click()
      await waitState(
        page,
        (state) => state.count === 50 && state.query.keyword === '',
        'Reset did not restore rows'
      )
      assert.equal(await (await input(page, 'search-input')).inputValue(), '')
    }
  },
  {
    name: 'sample-count-control',
    async run(page) {
      await choose(page, 'sample-count', '200 条样本', 200)
      await waitState(page, (state) => state.count === 200, '200-row sample selector did not apply')
      assert.equal(await page.locator('[data-testid^="price-"]').count(), 200)
      await choose(page, 'sample-count', '50 条样本', 50)
      await waitState(page, (state) => state.count === 50, '50-row sample selector did not apply')
    }
  },
  {
    name: 'account-and-inclusive-date-boundaries',
    async run(page) {
      await date(page, 'start-date', '2026-09-02')
      await date(page, 'end-date', '2026-09-04')
      const all = await waitState(
        page,
        (state) => state.count === 6,
        'Inclusive date bounds did not apply'
      )
      assert.deepEqual(all.ids, [2, 3, 4, 30, 31, 32])
      await choose(page, 'account-filter', '演示账户 B', 'demo-b')
      const filtered = await waitState(
        page,
        (state) => state.count === 4,
        'Account filter did not combine with dates'
      )
      assert.deepEqual(filtered.ids, [2, 4, 30, 32])
      assert.equal(await page.locator('[data-testid^="price-"]').count(), 4)
      await page.getByTestId('reset-filters').click()
      const reset = await waitState(
        page,
        (state) => state.count === 50,
        'Combined filter reset failed'
      )
      assert.deepEqual(reset.query, { keyword: '', account: 'all', start: '', end: '' })
    }
  },
  {
    name: 'profit-sorting-keeps-unknown-last',
    async run(page) {
      for (const order of ['desc', 'asc']) {
        await page.getByTestId('sort-profit').click()
        const state = await waitState(page, (item) => item.sort === order, `Missing ${order} sort`)
        assert.equal(state.ids.at(-1), 3, 'Unknown profit was not last')
        const visibleOrder = await page
          .locator('[data-testid^="price-"]')
          .evaluateAll((els) => els.map((el) => Number(el.dataset.testid.split('-')[1])))
        assert.deepEqual(visibleOrder, state.ids, 'DOM row order differs from the sorted model')
      }
    }
  },
  {
    name: 'expand-and-row-edit',
    async run(page, variant) {
      await toggleExpand(page, variant, 1)
      const detail = page.getByTestId('expanded-row-1')
      await detail.waitFor({ state: 'visible' })
      assert.match(await detail.innerText(), /00001|演示账户 A/)
      await toggleExpand(page, variant, 1)
      await detail.waitFor({ state: 'hidden' })
      await page.getByTestId('edit-row-2').click()
      await page.getByTestId('trade-dialog').waitFor({ state: 'visible' })
      assert.equal(await (await input(page, 'trade-symbol')).inputValue(), '00002')
      assert.equal(Number(await (await input(page, 'trade-price')).inputValue()), 1.409)
      await page.getByTestId('cancel-trade').click()
      await waitState(page, (state) => !state.open, 'Row edit cancellation failed')
      assert.equal((await snapshot(page)).saved, null)
    }
  },
  {
    name: 'empty-validation-and-price-precision',
    async run(page, variant) {
      await openDialog(page)
      await fill(page, 'trade-symbol', '')
      await page.getByTestId('submit-trade').click()
      await expectError(page, 'symbol', '请输入标的代码')
      await expectError(page, 'quantity', '数量必须大于 0')
      await expectError(page, 'price', '价格必须大于 0')
      assert.equal((await snapshot(page)).saved, null)
      await fill(page, 'trade-symbol', '00001')
      await fill(page, 'trade-quantity', '100')
      await fill(page, 'trade-price', '0')
      await page.getByTestId('submit-trade').click()
      await expectError(page, 'price', '价格必须大于 0')
      await fill(page, 'trade-price', '0.085')
      await choose(page, 'trade-account', '演示账户 B', 'demo-b')
      await date(page, 'trade-date', '2026-09-02')
      assert.equal(Number(await (await input(page, 'trade-price')).inputValue()), 0.085)
      await page.screenshot({ path: path.join(resultsDir, 'screenshots', `${variant}-dialog.png`) })
      await page.getByTestId('submit-trade').click()
      const state = await waitState(
        page,
        (item) => !item.open && item.saved,
        'Valid trade did not submit'
      )
      assert.deepEqual(state.saved, {
        symbol: '00001',
        account: 'demo-b',
        date: '2026-09-02',
        quantity: 100,
        price: 0.085
      })
      assert(
        await page.locator('.saved-receipt').isVisible(),
        'Saved draft receipt must be visible'
      )
    }
  },
  {
    name: 'cancel-reopen-resets-draft-and-validation',
    async run(page) {
      await openDialog(page)
      await page.getByTestId('submit-trade').click()
      await expectError(page, 'quantity', '数量必须大于 0')
      await fill(page, 'trade-symbol', '99999')
      await fill(page, 'trade-quantity', '57')
      await fill(page, 'trade-price', '3.142')
      await page.getByTestId('cancel-trade').click()
      await waitState(page, (state) => !state.open, 'Cancel failed')
      await openDialog(page)
      assert.equal(await (await input(page, 'trade-symbol')).inputValue(), '00001')
      assert.equal(await (await input(page, 'trade-quantity')).inputValue(), '')
      assert.equal(await (await input(page, 'trade-price')).inputValue(), '')
      assert.deepEqual((await snapshot(page)).errors, {})
      assert.equal(await page.locator('.field-error:visible').count(), 0)
      assert.equal((await snapshot(page)).saved, null)
    }
  },
  {
    name: 'backdrop-keeps-entered-draft',
    async run(page) {
      await openDialog(page)
      await fill(page, 'trade-quantity', '123')
      await fill(page, 'trade-price', '0.085')
      const box = await page.getByTestId('trade-dialog').boundingBox()
      assert(box, 'Dialog has no bounding box')
      const candidates = [
        { x: 8, y: 8 },
        { x: 1430, y: 8 },
        { x: 8, y: 890 }
      ]
      const point = candidates.find(
        (p) => p.x < box.x || p.x > box.x + box.width || p.y < box.y || p.y > box.y + box.height
      )
      assert(point, 'Could not find a real point outside the dialog')
      await page.mouse.click(point.x, point.y)
      await twoFrames(page)
      assert((await snapshot(page)).open, 'Backdrop click dismissed the form')
      assert.equal(Number(await (await input(page, 'trade-quantity')).inputValue()), 123)
      assert.equal(Number(await (await input(page, 'trade-price')).inputValue()), 0.085)
    }
  },
  {
    name: 'dialog-keyboard-focus-and-cancel-return',
    async run(page) {
      await openDialog(page)
      await page.waitForTimeout(150)
      assert(await focusInside(page), 'Opening the dialog did not move focus inside')
      for (let index = 0; index < 18; index++) {
        await page.keyboard.press(index < 12 ? 'Tab' : 'Shift+Tab')
        await twoFrames(page)
        assert(
          await focusInside(page),
          `Focus escaped the dialog after ${index < 12 ? 'Tab' : 'Shift+Tab'} step ${index + 1}`
        )
      }
      await page.getByTestId('cancel-trade').click()
      await waitState(page, (state) => !state.open, 'Dialog cancellation failed')
      await returnedFocus(page)
    }
  },
  {
    name: 'escape-dismissal-and-focus-return',
    async run(page) {
      await openDialog(page)
      await page.keyboard.press('Escape')
      await waitState(page, (state) => !state.open, 'Escape did not close the dialog')
      await returnedFocus(page)
    }
  },
  {
    name: 'mobile-scroll-and-edit-reachability',
    viewport: { width: 393, height: 852 },
    async run(page, variant) {
      const overflow = await page.evaluate(() => ({
        viewport: innerWidth,
        html: document.documentElement.scrollWidth,
        body: document.body.scrollWidth
      }))
      assert(
        overflow.html <= 394 && overflow.body <= 394,
        `Whole-page horizontal overflow: ${JSON.stringify(overflow)}`
      )
      await page.screenshot({ path: path.join(resultsDir, 'screenshots', `${variant}-mobile.png`) })
      const target = page.getByTestId('edit-row-1')
      const cell = page.getByTestId('price-1')
      await cell.scrollIntoViewIfNeeded()
      const scroller = await target.evaluateHandle((el) => {
        let node = el.parentElement
        while (node) {
          const style = getComputedStyle(node)
          if (node.scrollWidth > node.clientWidth + 2 && /auto|scroll/.test(style.overflowX))
            return node
          node = node.parentElement
        }
        return null
      })
      const element = scroller.asElement()
      if (element) {
        const box = await element.boundingBox()
        const top = Math.max(0, box.y)
        const bottom = Math.min(852, box.y + box.height)
        assert(bottom > top, 'Table scroll container is not in the viewport')
        await page.mouse.move(
          Math.min(360, Math.max(30, box.x + box.width / 2)),
          top + Math.min(60, (bottom - top) / 2)
        )
        await page.mouse.wheel(2000, 0)
        await page.waitForTimeout(120)
      }
      // Native Playwright click may scroll a local container, but never force an obscured click.
      await target.click()
      await page.getByTestId('trade-dialog').waitFor({ state: 'visible' })
      assert.equal(await (await input(page, 'trade-symbol')).inputValue(), '00001')
      await page.getByTestId('cancel-trade').click()
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
      await scroller.dispose()
    }
  }
]

async function main() {
  await fs.mkdir(path.join(resultsDir, 'screenshots'), { recursive: true })
  const browser = await chromium.launch({ headless: true })
  const result = {
    at: new Date().toISOString(),
    baseURL,
    browser: browser.version(),
    method:
      'Synthetic data; real UI actions; model snapshots are assertions only. One fresh browser context per test. No force clicks.',
    cases: []
  }
  try {
    for (const variant of variants) {
      for (const test of tests) {
        const context = await browser.newContext({
          viewport: test.viewport || { width: 1440, height: 900 },
          reducedMotion: 'reduce',
          locale: 'zh-CN',
          timezoneId: 'Asia/Shanghai',
          serviceWorkers: 'block'
        })
        const page = await context.newPage()
        page.setDefaultTimeout(5000)
        page.setDefaultNavigationTimeout(30000)
        const errors = []
        page.on('pageerror', (error) => errors.push(error.message))
        const item = { variant, test: test.name, status: 'passed', pageErrors: errors }
        try {
          await ready(page, variant)
          await test.run(page, variant)
          assert.deepEqual(errors, [], 'Uncaught browser errors')
        } catch (error) {
          item.status = 'failed'
          item.error = error.stack || String(error)
          await page
            .screenshot({
              path: path.join(resultsDir, 'screenshots', `${variant}-${test.name}-failure.png`)
            })
            .catch(() => {})
        } finally {
          result.cases.push(item)
          console.log(`${item.status.toUpperCase()} ${variant}: ${test.name}`)
          await context.close()
          await fs.writeFile(path.join(resultsDir, 'checks.json'), JSON.stringify(result, null, 2))
        }
      }
    }
  } finally {
    await browser.close()
  }
  const failures = result.cases.filter((item) => item.status === 'failed').length
  console.log(
    `${result.cases.length - failures}/${result.cases.length} checks passed. Results: ${path.join(resultsDir, 'checks.json')}`
  )
  if (failures) process.exitCode = 1
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href)
  await main()
