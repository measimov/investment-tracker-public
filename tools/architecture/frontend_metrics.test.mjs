import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { runInNewContext } from 'node:vm'

const require = createRequire(new URL('../../frontend/package.json', import.meta.url))
const analyzer = fileURLToPath(new URL('./frontend_metrics.mjs', import.meta.url))

function fixture(t, sources) {
  const root = mkdtempSync(path.join(tmpdir(), 'architecture-metrics-test-'))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  for (const [file, content] of Object.entries(sources)) {
    mkdirSync(path.dirname(path.join(root, file)), { recursive: true })
    writeFileSync(path.join(root, file), content)
  }
  return { root, files: Object.keys(sources) }
}

function run(request) {
  const execution = spawnSync(process.execPath, [analyzer], {
    input: JSON.stringify(request),
    encoding: 'utf8',
    cwd: tmpdir(),
    timeout: 30000
  })
  assert.equal(execution.error, undefined)
  assert.equal(execution.status, 0, execution.stderr || execution.stdout)
  assert.equal(execution.stderr, '')
  return JSON.parse(execution.stdout)
}

test('JSON input can arrive in chunks larger than a pipe buffer', async (t) => {
  const request = fixture(t, { 'src/example.ts': 'function example() { return 1 }' })
  const input = JSON.stringify({ ...request, coverage: { unrelated: 'x'.repeat(1024 * 1024) } })
  const child = spawn(process.execPath, [analyzer], { cwd: tmpdir(), timeout: 30000 })
  const output = []
  const errors = []
  child.stdout.on('data', (chunk) => output.push(chunk))
  child.stderr.on('data', (chunk) => errors.push(chunk))
  const finished = new Promise((resolve, reject) => {
    child.on('error', reject)
    child.on('close', resolve)
  })
  child.stdin.end(input)
  assert.equal(await finished, 0, Buffer.concat(errors).toString())
  const report = JSON.parse(Buffer.concat(output).toString())
  assert.deepEqual(report.errors, [])
  assert.equal(report.functions[0].status, 'unknown')
})

test('native classic complexity handles TS arrows, methods, nesting and duplicate names', (t) => {
  const file = 'frontend/src/metrics.ts'
  const source = [
    '/* eslint-disable metrics/complexity */',
    'throw new Error("analyzer must never execute source")',
    'export const arrow = (value: number = 0) => value > 0 ? 1 : 0',
    'class Example {',
    '  field = true || false',
    '  static { if (true) {} }',
    '  callback = (value: number) => value || 0',
    '  method(value: number) {',
    '    function nested(flag: boolean) { return flag ? 1 : 2 }',
    '    if (value > 1) return nested(true)',
    '    return 0',
    '  }',
    '}',
    'const obj = { method(value: number) { return value && 1 } }',
    '{ function same() { return 1 } }',
    '{ function same() { return 2 } }',
    'function cases(value: number) {',
    '  switch(value) { case 1: return 1; case 2: return 2; default: return 0 }',
    '}',
    'const anonymous = [1].map((value) => value + 1)',
    'interface Shape { method(): void }'
  ].join('\n')
  const request = fixture(t, { [file]: source })
  assert.equal(existsSync(path.join(request.root, 'node_modules')), false)
  const report = run(request)
  assert.deepEqual(report.errors, [])
  assert.equal(report.tools.eslint, '9.39.1')
  assert.equal(report.tools['@typescript-eslint/parser'], '8.22.0')
  assert.equal(report.tools['vue-eslint-parser'], '10.2.0')
  assert.deepEqual(
    report.functions.map(({ name, line, end_line, cc }) => ({ name, line, end_line, cc })),
    [
      { name: 'arrow', line: 3, end_line: 3, cc: 3 },
      { name: 'Example.callback', line: 7, end_line: 7, cc: 2 },
      { name: 'Example.method', line: 8, end_line: 12, cc: 2 },
      { name: 'Example.method.nested', line: 9, end_line: 9, cc: 2 },
      { name: 'obj.method', line: 14, end_line: 14, cc: 2 },
      { name: 'same', line: 15, end_line: 15, cc: 1 },
      { name: 'same', line: 16, end_line: 16, cc: 1 },
      { name: 'cases', line: 17, end_line: 19, cc: 3 },
      { name: '<anonymous@20:27>', line: 20, end_line: 20, cc: 1 }
    ]
  )
  const arrow = report.functions[0]
  assert.equal(arrow.column, source.split('\n')[2].indexOf('(value') + 1)
  assert.equal(arrow.end_column, source.split('\n')[2].length + 1)
  for (const fn of report.functions) {
    assert.equal(fn.status, 'unknown')
    assert.equal(fn.coverage, null)
    assert.equal(fn.crap, null)
    assert.match(fn.reason, /未提供/)
  }
})

test('native Istanbul statement lines belong only to their innermost function', (t) => {
  const file = 'frontend/src/nested.js'
  const source = [
    'function outer(flag) {',
    '  function inner(value) {',
    '    if (value) {',
    '      return 1',
    '    }',
    '    return 0',
    '  }',
    '  if (flag) return inner(true)',
    '  return 2',
    '}',
    'const first = () => 1; const second = () => 2;',
    'outer(true); first();'
  ].join('\n')
  const request = fixture(t, { [file]: source })
  const { createInstrumenter } = require('istanbul-lib-instrument')
  const instrumented = createInstrumenter().instrumentSync(source, path.join(request.root, file))
  const context = {}
  runInNewContext(instrumented, context)
  const coverage = JSON.parse(JSON.stringify(context.__coverage__))
  // Deliberately misleading function invocation counters must have no effect.
  for (const entry of Object.values(coverage)) {
    for (const id of Object.keys(entry.f)) entry.f[id] = 999
  }
  const report = run({ ...request, coverage })
  assert.deepEqual(report.errors, [])
  assert.deepEqual(
    report.functions.map(({ name, cc, coverage, status }) => ({ name, cc, coverage, status })),
    [
      { name: 'outer', cc: 2, coverage: 0.5, status: 'measured' },
      { name: 'outer.inner', cc: 2, coverage: 2 / 3, status: 'measured' },
      { name: 'first', cc: 1, coverage: 1, status: 'measured' },
      { name: 'second', cc: 1, coverage: 0, status: 'measured' }
    ]
  )
  assert.equal(report.functions[0].crap, 2.5)
  assert.equal(report.functions[3].crap, 2)
  assert.notEqual(report.functions[2].column, report.functions[3].column)
})

test('zero statement hits are measured but absent or unreliable mappings stay unknown', (t) => {
  const file = 'frontend/src/example.ts'
  const source = 'function example() {\n  return 1\n}\nfunction empty() {}\n'
  const request = fixture(t, { [file]: source })
  const data = {
    path: file,
    statementMap: { 0: { start: { line: 2, column: 2 }, end: { line: 2, column: 10 } } },
    s: { 0: 0 },
    fnMap: { 0: { name: 'example' } },
    f: { 0: 25 }
  }
  const zero = run({ ...request, coverage: { [file]: data } })
  assert.equal(zero.functions[0].coverage, 0)
  assert.equal(zero.functions[0].crap, 2)
  assert.equal(zero.functions[0].status, 'measured')
  assert.equal(zero.functions[1].status, 'unknown')
  const variants = [
    {},
    { [path.join('/another-checkout', file)]: data },
    { [file]: data, [path.join(request.root, file)]: data },
    { [file]: { ...data, path: 'other.ts' } },
    { [file]: { ...data, s: {} } },
    { [file]: { ...data, s: { 0: -1 } } },
    { [file]: { ...data, inputSourceMap: { version: 3 } } },
    {
      [file]: {
        ...data,
        statementMap: { 0: { start: { line: 20, column: 0 }, end: { line: 20, column: 1 } } }
      }
    },
    {
      [file]: {
        ...data,
        statementMap: { 0: { start: { line: 2, column: 40 }, end: { line: 2, column: 41 } } }
      }
    },
    {
      [file]: {
        ...data,
        statementMap: { 0: { start: { line: 2, column: 2 }, end: { line: 4, column: 1 } } }
      }
    }
  ]
  for (const coverage of variants) {
    const report = run({ ...request, coverage })
    assert.deepEqual(report.errors, [])
    assert.ok(
      report.functions.every(
        (fn) => fn.status === 'unknown' && fn.coverage === null && fn.crap === null
      )
    )
  }
})

test('parse failures remain visible without discarding other files', (t) => {
  const request = fixture(t, {
    'frontend/src/bad.ts': 'export function broken( {',
    'frontend/src/ok.ts': 'export function valid() { return 1 }'
  })
  const report = run(request)
  assert.equal(report.errors.length, 1)
  assert.equal(report.errors[0].path, 'frontend/src/bad.ts')
  assert.deepEqual(
    report.functions.map((fn) => fn.name),
    ['valid']
  )
})

test('real Vitest Istanbul maps Vue normal/setup scripts and unimported files to source', (t) => {
  const measured = [
    '<script lang="ts">',
    'export function normal(value: number) {',
    '  if (value) {',
    '    return 1',
    '  }',
    '  return 0',
    '}',
    '</script>',
    '<script setup lang="ts">',
    'const choose = (value: number) => {',
    '  if (value > 0) {',
    '    return 1',
    '  }',
    '  return 0',
    '}',
    'defineExpose({ choose })',
    '</script>',
    '<template><p>{{ choose(1) }}</p></template>'
  ].join('\n')
  const unimported = [
    '<script setup lang="ts">',
    'function untouched(value: number) {',
    '  return value ? 1 : 0',
    '}',
    '</script>',
    '<template><p>未导入</p></template>'
  ].join('\n')
  const request = fixture(t, {
    'src/Measured.vue': measured,
    'src/Unimported.vue': unimported,
    'src/measured.spec.ts': [
      "import { expect, test } from 'vitest'",
      "import { createSSRApp } from 'vue'",
      "import { renderToString } from 'vue/server-renderer'",
      "import Component, { normal } from './Measured.vue'",
      "test('exercise normal and setup functions', async () => {",
      '  expect(normal(0)).toBe(0)',
      '  expect(await renderToString(createSSRApp(Component))).toContain("<p>1</p>")',
      '})'
    ].join('\n')
  })
  const importUrl = (name) => pathToFileURL(require.resolve(name)).href
  const config = [
    `import { defineConfig } from ${JSON.stringify(importUrl('vitest/config'))}`,
    `import vue from ${JSON.stringify(importUrl('@vitejs/plugin-vue'))}`,
    'export default defineConfig({',
    '  plugins: [vue()],',
    `  resolve: { alias: ${JSON.stringify({
      'vue/server-renderer': require.resolve('vue/server-renderer'),
      vue: require.resolve('vue'),
      '@vitest/coverage-istanbul': require.resolve('@vitest/coverage-istanbul')
    })} },`,
    '  test: {',
    '    pool: "forks", poolOptions: { forks: { singleFork: true } }, fileParallelism: false,',
    '    coverage: { enabled: true, provider: "istanbul", all: true, include: ["src/**/*.vue"], reporter: ["json"] }',
    '  }',
    '})'
  ].join('\n')
  writeFileSync(path.join(request.root, 'vitest.config.mjs'), config)
  const cli = path.join(path.dirname(require.resolve('vitest/package.json')), 'vitest.mjs')
  const execution = spawnSync(process.execPath, [cli, 'run', '--config', 'vitest.config.mjs'], {
    cwd: request.root,
    encoding: 'utf8',
    timeout: 60000
  })
  assert.equal(execution.error, undefined)
  assert.equal(execution.status, 0, `${execution.stdout}\n${execution.stderr}`)
  const coverage = JSON.parse(
    readFileSync(path.join(request.root, 'coverage/coverage-final.json'), 'utf8')
  )
  const report = run({ ...request, files: ['src/Measured.vue', 'src/Unimported.vue'], coverage })
  assert.deepEqual(report.errors, [])
  assert.deepEqual(
    report.functions.map(({ name, line, end_line, cc, coverage, status }) => ({
      name,
      line,
      end_line,
      cc,
      coverage,
      status
    })),
    [
      { name: 'normal', line: 2, end_line: 7, cc: 2, coverage: 2 / 3, status: 'measured' },
      { name: 'choose', line: 10, end_line: 15, cc: 2, coverage: 2 / 3, status: 'measured' },
      { name: 'untouched', line: 2, end_line: 4, cc: 2, coverage: 0, status: 'measured' }
    ]
  )
  assert.equal(report.functions[1].column, measured.split('\n')[9].indexOf('(value') + 1)
  assert.equal(report.functions[1].end_column, 2)
})
