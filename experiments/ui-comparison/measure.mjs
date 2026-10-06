import { chromium } from '../../frontend/node_modules/playwright/index.mjs'
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { baseURL, resultsDir, variants, ready } from './check.mjs'

const measuredRounds = Number(process.env.UI_BENCH_ROUNDS || 5)
assert(
  Number.isInteger(measuredRounds) && measuredRounds >= 5,
  'Use at least five measured rounds per group'
)
const viewport = { width: 1440, height: 900 }
const sampleCounts = [50, 200]
const cacheModes = ['cold-context', 'warm-revisit']

function stats(values) {
  const sorted = values.filter(Number.isFinite).sort((a, b) => a - b)
  if (!sorted.length) return { samples: 0, median: null, min: null, max: null }
  const mid = Math.floor(sorted.length / 2)
  return {
    samples: sorted.length,
    median: sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2,
    min: sorted[0],
    max: sorted.at(-1)
  }
}

function summarize(trials) {
  const output = []
  for (const variant of variants)
    for (const rows of sampleCounts)
      for (const cacheMode of cacheModes) {
        const selected = trials.filter(
          (item) =>
            item.variant === variant &&
            item.rows === rows &&
            item.cacheMode === cacheMode &&
            !item.excludedWarmup &&
            item.status === 'passed'
        )
        const metrics = {}
        for (const key of [
          'readyMs',
          'navigationToDomContentLoadedMs',
          'firstContentfulPaintMs',
          'domElements',
          'jsTransferBytes',
          'cssTransferBytes',
          'jsEncodedBodyBytes',
          'cssEncodedBodyBytes',
          'readyLongTaskCount',
          'readyLongTaskDurationMs'
        ]) {
          metrics[key] = stats(selected.map((item) => item.loading[key]))
        }
        const operationNames = [
          ...new Set(selected.flatMap((item) => item.operations.map((operation) => operation.name)))
        ]
        const operations = Object.fromEntries(
          operationNames.map((name) => [
            name,
            {
              elapsedIncludingTwoAnimationFramesMs: stats(
                selected.map(
                  (item) => item.operations.find((operation) => operation.name === name)?.elapsedMs
                )
              ),
              longTaskCount: stats(
                selected.map(
                  (item) =>
                    item.operations.find((operation) => operation.name === name)?.longTaskCount
                )
              ),
              longTaskDurationMs: stats(
                selected.map(
                  (item) =>
                    item.operations.find((operation) => operation.name === name)?.longTaskDurationMs
                )
              )
            }
          ])
        )
        output.push({
          variant,
          rows,
          cacheMode,
          measuredTrials: selected.length,
          loading: metrics,
          operations
        })
      }
  return output
}

async function instrument(context) {
  await context.addInitScript(() => {
    window.__comparisonLongTasks = []
    window.__comparisonLongTasksSupported =
      PerformanceObserver.supportedEntryTypes.includes('longtask')
    window.__comparisonDrainLongTasks = () => {}
    if (window.__comparisonLongTasksSupported) {
      const record = (entries) => {
        for (const entry of entries)
          window.__comparisonLongTasks.push({
            startTime: entry.startTime,
            duration: entry.duration
          })
      }
      const observer = new PerformanceObserver((list) => record(list.getEntries()))
      observer.observe({ type: 'longtask', buffered: true })
      window.__comparisonDrainLongTasks = () => record(observer.takeRecords())
    }
    performance.setResourceTimingBufferSize(2000)
  })
}

async function loadingMetrics(page, rows) {
  return page.evaluate(async (expectedRows) => {
    // Flush observer delivery without including this pause in the ready milestone.
    await new Promise((resolve) => setTimeout(resolve, 0))
    window.__comparisonDrainLongTasks()
    const mark = performance.getEntriesByName('comparison-ready').at(-1)
    if (!mark) throw new Error('Missing comparison-ready performance mark')
    const state = window.__uiBench.snapshot()
    if (state.count !== expectedRows)
      throw new Error(`Expected ${expectedRows} rows, got ${state.count}`)
    const navigation = performance.getEntriesByType('navigation')[0]
    const resources = performance
      .getEntriesByType('resource')
      .filter((entry) => entry.responseEnd <= mark.startTime)
      .map((entry) => ({
        url: entry.name,
        initiator: entry.initiatorType,
        startTime: entry.startTime,
        duration: entry.duration,
        transferSize: entry.transferSize,
        encodedBodySize: entry.encodedBodySize,
        decodedBodySize: entry.decodedBodySize
      }))
    const asset = (extension) =>
      resources.filter((entry) => new URL(entry.url).pathname.endsWith(extension))
    const sum = (entries, key) => entries.reduce((total, entry) => total + entry[key], 0)
    const js = asset('.js'),
      css = asset('.css')
    const tasks = window.__comparisonLongTasks
      .filter((entry) => entry.startTime < mark.startTime)
      .map((entry) => ({
        ...entry,
        overlapDurationMs:
          Math.min(entry.startTime + entry.duration, mark.startTime) - entry.startTime
      }))
    return {
      readyMs: mark.startTime,
      navigationToDomContentLoadedMs: navigation?.domContentLoadedEventEnd ?? null,
      firstContentfulPaintMs:
        performance.getEntriesByName('first-contentful-paint')[0]?.startTime ?? null,
      domElements: document.querySelectorAll('*').length,
      renderedPriceCells: document.querySelectorAll('[data-testid^="price-"]').length,
      jsTransferBytes: sum(js, 'transferSize'),
      cssTransferBytes: sum(css, 'transferSize'),
      jsEncodedBodyBytes: sum(js, 'encodedBodySize'),
      cssEncodedBodyBytes: sum(css, 'encodedBodySize'),
      zeroTransferAssetsWithBody: [...js, ...css].filter(
        (entry) => entry.transferSize === 0 && entry.decodedBodySize > 0
      ).length,
      readyLongTaskCount: tasks.length,
      readyLongTaskDurationMs: sum(tasks, 'overlapDurationMs'),
      longTasksSupported: window.__comparisonLongTasksSupported,
      navigation: navigation
        ? {
            type: navigation.type,
            transferSize: navigation.transferSize,
            encodedBodySize: navigation.encodedBodySize
          }
        : null,
      resources,
      longTasks: tasks
    }
  }, rows)
}

async function measureOperation(page, operation, expectedRows) {
  return page.evaluate(
    async ({ name, expectedRows }) => {
      const api = window.__uiBench
      const settle = () =>
        new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
      let mechanism = 'shared-model API + nextTick + two RAFs'
      let observedTarget = null
      const start = performance.now()
      if (name === 'filter-account') await api.filter({ account: 'demo-b' })
      else if (name === 'reset-filters') await api.reset()
      else if (name === 'sort-desc' || name === 'sort-asc') await api.sort()
      else if (name === 'open-dialog') await api.openTrade()
      else if (name === 'close-dialog') await api.closeTrade()
      else if (name === 'expand-first-row' || name === 'collapse-first-row') {
        const cell = document.querySelector('[data-testid^="price-"]')
        const row = cell?.closest('tr')
        const id = cell?.getAttribute('data-testid')?.replace('price-', '')
        const trigger = row?.querySelector(
          '[data-testid^="expand-row-"],.el-table__expand-icon,.n-data-table-expand-trigger'
        )
        if (!trigger || !id)
          throw new Error('Could not locate the first native row expansion trigger')
        trigger.click()
        await settle()
        const detail = document.querySelector(`[data-testid="expanded-row-${id}"]`)
        const expanded = detail !== null && detail.getClientRects().length > 0
        if (expanded !== (name === 'expand-first-row'))
          throw new Error(`Row expansion state did not match ${name}`)
        mechanism = 'native expansion trigger DOM click + two RAFs'
        observedTarget = { rowId: Number(id), expanded }
      } else if (name === 'scroll-table-down' || name === 'scroll-table-top') {
        const table = document.querySelector('[data-testid="holding-table"]')
        const candidates = table ? [table, ...table.querySelectorAll('*')] : []
        const scroller = candidates.find(
          (el) =>
            el.scrollHeight > el.clientHeight + 2 &&
            /auto|scroll/.test(getComputedStyle(el).overflowY)
        )
        if (!scroller)
          throw new Error('Could not locate a real vertically scrollable table container')
        scroller.scrollTop =
          name === 'scroll-table-top'
            ? 0
            : Math.min(560, scroller.scrollHeight - scroller.clientHeight)
        await settle()
        if (name === 'scroll-table-down' && scroller.scrollTop <= 0)
          throw new Error('Table did not scroll down')
        if (name === 'scroll-table-top' && scroller.scrollTop !== 0)
          throw new Error('Table did not return to the top')
        mechanism = 'native table scrollTop change + two RAFs'
        observedTarget = {
          scrollTop: scroller.scrollTop,
          viewportHeight: scroller.clientHeight,
          scrollHeight: scroller.scrollHeight
        }
      } else throw new Error(`Unknown operation ${name}`)
      const end = performance.now()
      // The timed promise includes Vue nextTick + two RAFs, shared by all variants.
      await new Promise((resolve) => setTimeout(resolve, 0))
      window.__comparisonDrainLongTasks()
      const state = api.snapshot()
      if (state.count !== (name === 'filter-account' ? expectedRows / 2 : expectedRows))
        throw new Error(`Incorrect row count after ${name}`)
      if (name === 'sort-desc' && (state.sort !== 'desc' || state.ids.at(-1) !== 3))
        throw new Error('Descending sort contract failed')
      if (name === 'sort-asc' && (state.sort !== 'asc' || state.ids.at(-1) !== 3))
        throw new Error('Ascending sort contract failed')
      if (name === 'open-dialog' && !state.open) throw new Error('Dialog did not open')
      if (name === 'close-dialog' && state.open) throw new Error('Dialog did not close')
      // An evaluate task can start before our start timestamp and still contain the update.
      const tasks = window.__comparisonLongTasks
        .filter((entry) => entry.startTime < end && entry.startTime + entry.duration > start)
        .map((entry) => ({
          ...entry,
          overlapDurationMs:
            Math.min(entry.startTime + entry.duration, end) - Math.max(entry.startTime, start)
        }))
      return {
        name,
        mechanism,
        observedTarget,
        elapsedMs: end - start,
        startTime: start,
        endTime: end,
        longTaskCount: tasks.length,
        longTaskDurationMs: tasks.reduce((total, task) => total + task.overlapDurationMs, 0),
        longTasks: tasks,
        domElementsAfter: document.querySelectorAll('*').length,
        rowCountAfter: state.count
      }
    },
    { name: operation, expectedRows }
  )
}

async function main() {
  await fs.mkdir(resultsDir, { recursive: true })
  const browser = await chromium.launch({ headless: true })
  const result = {
    at: new Date().toISOString(),
    baseURL,
    environment: {
      browser: browser.version(),
      node: process.version,
      platform: process.platform,
      arch: process.arch,
      cpu: os.cpus()[0]?.model,
      logicalCpus: os.cpus().length,
      totalMemoryGiB: Number((os.totalmem() / 1024 ** 3).toFixed(2)),
      viewport,
      reducedMotion: 'reduce',
      locale: 'zh-CN',
      timezoneId: 'Asia/Shanghai',
      cpuThrottling: 'none',
      networkThrottling: 'none',
      execution: 'serial'
    },
    method: {
      samples: [50, 200],
      measuredRounds,
      warmup:
        'Round zero is recorded but excluded separately for every variant × row count × cache mode.',
      order:
        'Variants rotate each group and counts/cache-mode order alternate by round; all trials run serially.',
      cold: 'Fresh isolated browser context; no priming navigation.',
      warm: 'Fresh browser context first visits the same URL, then about:blank, then revisits the URL. Cache hit evidence is recorded; no assumption that all resources were cached.',
      loading:
        'comparison-ready mark after Vue nextTick and two requestAnimationFrame callbacks; resources completed by that mark; long tasks observed in the page.',
      longTasks:
        'Observer records are drained after a non-timed event-loop turn. Tasks are attributed when their intervals overlap the measured interval, including tasks that began before the operation start; duration sums count only the overlap. Raw task timing and overlap are retained. This is observed main-thread work during the interval, not proof of exclusive causation by the operation.',
      transfer:
        'Resource Timing transferSize includes response headers; encodedBodySize is encoded body size even when served from cache. These are localhost server transfer metrics, not compressed production bundle estimates.',
      operations:
        'Programmatic shared-model filter/reset/sort/open/close latency includes nextTick + two RAFs; native row expansion clicks and table scrollTop changes include two RAFs. Each observation records its mechanism. These are not INP or independent validation of UI controls; check.mjs covers real UI.',
      exclusion:
        'Failed and warmup trials are retained but excluded from summaries. No performance threshold implies a migration decision.'
    },
    trials: [],
    summary: []
  }
  try {
    for (let round = 0; round <= measuredRounds; round++) {
      const counts = round % 2 ? [...sampleCounts].reverse() : sampleCounts
      const modes = round % 2 ? [...cacheModes].reverse() : cacheModes
      for (const [countIndex, rows] of counts.entries())
        for (const [modeIndex, cacheMode] of modes.entries()) {
          const offset = (round + countIndex + modeIndex) % variants.length
          const ordered = [...variants.slice(offset), ...variants.slice(0, offset)]
          for (const variant of ordered) {
            const context = await browser.newContext({
              viewport,
              reducedMotion: 'reduce',
              locale: 'zh-CN',
              timezoneId: 'Asia/Shanghai',
              serviceWorkers: 'block'
            })
            await instrument(context)
            const page = await context.newPage()
            page.setDefaultTimeout(30000)
            page.setDefaultNavigationTimeout(30000)
            const pageErrors = []
            page.on('pageerror', (error) => pageErrors.push(error.message))
            const trial = {
              sequence: result.trials.length + 1,
              round,
              excludedWarmup: round === 0,
              variant,
              rows,
              cacheMode,
              status: 'passed',
              operations: [],
              pageErrors
            }
            try {
              if (cacheMode === 'warm-revisit') {
                await ready(page, variant, rows)
                await page.goto('about:blank')
              }
              await ready(page, variant, rows)
              trial.loading = await loadingMetrics(page, rows)
              assert.equal(
                trial.loading.renderedPriceCells,
                rows,
                'Comparable sample must render all requested rows'
              )
              for (const operation of [
                'filter-account',
                'reset-filters',
                'sort-desc',
                'sort-asc',
                'expand-first-row',
                'collapse-first-row',
                'scroll-table-down',
                'scroll-table-top',
                'open-dialog',
                'close-dialog'
              ]) {
                trial.operations.push(await measureOperation(page, operation, rows))
              }
              assert.deepEqual(pageErrors, [], 'Uncaught browser errors')
            } catch (error) {
              trial.status = 'failed'
              trial.error = error.stack || String(error)
            } finally {
              await context.close()
              result.trials.push(trial)
              result.summary = summarize(result.trials)
              await fs.writeFile(
                path.join(resultsDir, 'measurements.json'),
                JSON.stringify(result, null, 2)
              )
              console.log(
                `${trial.status.toUpperCase()} ${trial.excludedWarmup ? 'warmup' : `round ${round}`} ${variant} rows=${rows} ${cacheMode}${trial.loading ? ` ready=${trial.loading.readyMs.toFixed(1)}ms` : ''}`
              )
            }
          }
        }
    }
  } finally {
    await browser.close()
  }
  const failures = result.trials.filter((trial) => trial.status === 'failed').length
  console.log(
    `${result.trials.length - failures}/${result.trials.length} trials completed. Results: ${path.join(resultsDir, 'measurements.json')}`
  )
  if (failures) process.exitCode = 1
}

await main()
