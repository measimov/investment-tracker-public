import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { once } from 'node:events'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'

const analyzer = fileURLToPath(new URL('./frontend_index.mjs', import.meta.url))
const repository = fileURLToPath(new URL('../../', import.meta.url))

function fixture(t, sources) {
  const root = mkdtempSync(path.join(tmpdir(), 'architecture-frontend-test-'))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  const files = {
    'frontend/tsconfig.app.json': JSON.stringify({
      extends: '@vue/tsconfig/tsconfig.dom.json',
      compilerOptions: { baseUrl: '.', paths: { '@/*': ['./src/*'] } }
    }),
    ...sources
  }
  for (const [file, content] of Object.entries(files)) {
    mkdirSync(path.dirname(path.join(root, file)), { recursive: true })
    writeFileSync(path.join(root, file), content)
  }
  return { root, files: Object.keys(files) }
}

function run(request) {
  const execution = spawnSync(process.execPath, [analyzer], {
    input: JSON.stringify(request),
    encoding: 'utf8',
    cwd: tmpdir(),
    timeout: 30000
  })
  assert.equal(execution.status, 0, execution.stderr || execution.stdout)
  assert.equal(execution.stderr, '')
  return JSON.parse(execution.stdout)
}

test(
  'large file lists arrive in delayed stdin chunks through EOF',
  { timeout: 30000 },
  async (t) => {
    const file = 'frontend/src/composables/useAliveGuard.ts'
    const request = fixture(t, {
      [file]: readFileSync(path.join(repository, file), 'utf8'),
      'frontend/src/entry.ts':
        "import { useAliveGuard } from './composables/useAliveGuard'\nexport const guard = useAliveGuard\n"
    })
    request.files.unshift(
      ...Array.from({ length: 6000 }, (_, i) => `docs/资料-${i}-${'snapshot-'.repeat(8)}.md`)
    )
    const input = Buffer.from(JSON.stringify(request))
    assert.ok(input.length > 256 * 1024)
    const child = spawn(process.execPath, [analyzer], { cwd: tmpdir() })
    t.after(() => child.kill())
    let stdout = ''
    let stderr = ''
    child.stdout.setEncoding('utf8').on('data', (chunk) => (stdout += chunk))
    child.stderr.setEncoding('utf8').on('data', (chunk) => (stderr += chunk))
    // Preserve the analyzer's error output if it closes stdin before the final chunk.
    child.stdin.on('error', () => {})
    const closed = once(child, 'close')
    for (let offset = 0; offset < input.length && child.exitCode === null; offset += 4096) {
      child.stdin.write(input.subarray(offset, offset + 4096))
      await delay(10)
    }
    child.stdin.end()
    const [code] = await closed
    assert.equal(code, 0, stderr || stdout)
    assert.equal(stderr, '')
    const index = JSON.parse(stdout)
    assert.deepEqual(index.errors, [])
    assert.ok(index.symbols.some((item) => item.path === file && item.name === 'useAliveGuard'))
    assert.ok(
      index.dependencies.some(
        (item) =>
          item.source === file && item.specifier === 'vue' && item.external && item.line === 1
      )
    )
    assert.ok(
      index.dependencies.some(
        (item) =>
          item.source === 'frontend/src/entry.ts' &&
          item.target === file &&
          item.kind === 'runtime' &&
          !item.external &&
          item.line === 1
      )
    )
  }
)

test('snapshot resolves aliases, package entries, re-exports and type/runtime/dynamic evidence', (t) => {
  const request = fixture(t, {
    'frontend/src/entry.ts': [
      'import {',
      '  value',
      "} from '@/lib'",
      "import type { Item } from '@/types'",
      "import { type Item as Other, value as other } from '@/mixed'",
      "export { value as forwarded } from './lib'",
      "export type { Item } from './types'",
      "type Imported = import('./types').Item",
      "import { ref } from 'vue'",
      "export const lazy = () => import('@/lib')",
      "import values = require('./lib')",
      'export const result: Item = { value: value + other }',
      'void ref; void values;'
    ].join('\n'),
    'frontend/src/lib/index.ts': 'export const value = 1\n',
    'frontend/src/types.ts': 'export interface Item { value: number }\n',
    'frontend/src/mixed.ts': "export type { Item } from './types'\nexport { value } from './lib'\n"
  })
  assert.equal(existsSync(path.join(request.root, 'frontend/node_modules')), false)
  const index = run(request)
  assert.deepEqual(index.errors, [])
  assert.equal(index.tools['dependency-cruiser'], '16.10.4')
  const dependencies = index.dependencies.filter((item) => item.source === 'frontend/src/entry.ts')
  assert.deepEqual(
    dependencies.map(({ target, specifier, line, kind, external }) => ({
      target,
      specifier,
      line,
      kind,
      external
    })),
    [
      {
        target: 'frontend/src/lib/index.ts',
        specifier: '@/lib',
        line: 1,
        kind: 'runtime',
        external: false
      },
      {
        target: 'frontend/src/types.ts',
        specifier: '@/types',
        line: 4,
        kind: 'type',
        external: false
      },
      {
        target: 'frontend/src/mixed.ts',
        specifier: '@/mixed',
        line: 5,
        kind: 'runtime',
        external: false
      },
      {
        target: 'frontend/src/mixed.ts',
        specifier: '@/mixed',
        line: 5,
        kind: 'type',
        external: false
      },
      {
        target: 'frontend/src/lib/index.ts',
        specifier: './lib',
        line: 6,
        kind: 'runtime',
        external: false
      },
      {
        target: 'frontend/src/types.ts',
        specifier: './types',
        line: 7,
        kind: 'type',
        external: false
      },
      {
        target: 'frontend/src/types.ts',
        specifier: './types',
        line: 8,
        kind: 'type',
        external: false
      },
      { target: null, specifier: 'vue', line: 9, kind: 'runtime', external: true },
      {
        target: 'frontend/src/lib/index.ts',
        specifier: '@/lib',
        line: 10,
        kind: 'dynamic',
        external: false
      },
      {
        target: 'frontend/src/lib/index.ts',
        specifier: './lib',
        line: 11,
        kind: 'runtime',
        external: false
      }
    ]
  )
})

test('Vue normal script and script setup retain original lines and only source functions', (t) => {
  const request = fixture(t, {
    'frontend/src/Panel.vue': [
      '<template>',
      '  <button @click="increment">{{ count }}</button>',
      '</template>',
      '<script lang="ts">',
      "import type { Item } from '@/types'",
      'export function original(item: Item) {',
      '  return item.value',
      '}',
      '</script>',
      '<script setup lang="ts">',
      "import { ref } from 'vue'",
      "import { value } from '@/lib'",
      'const count = ref(value)',
      'const increment = () => {',
      '  count.value++',
      '}',
      '</script>',
      '<style scoped>button { color: red; }</style>'
    ].join('\n'),
    'frontend/src/types.ts': 'export interface Item { value: number }\n',
    'frontend/src/lib.ts': 'export const value = 1\n'
  })
  const index = run(request)
  assert.deepEqual(index.errors, [])
  assert.deepEqual(
    index.dependencies.map(({ specifier, line, kind }) => ({ specifier, line, kind })),
    [
      { specifier: '@/types', line: 5, kind: 'type' },
      { specifier: 'vue', line: 11, kind: 'runtime' },
      { specifier: '@/lib', line: 12, kind: 'runtime' }
    ]
  )
  assert.deepEqual(
    index.symbols.map(({ name, line, end_line }) => ({ name, line, end_line })),
    [
      { name: 'original', line: 6, end_line: 8 },
      { name: 'increment', line: 14, end_line: 16 }
    ]
  )
})

test('classes, nested methods and duplicate local names have qualified source ranges', (t) => {
  const request = fixture(t, {
    'frontend/src/symbols.ts': [
      'export class Counter {',
      '  increment() {',
      '    function nested() { return 1 }',
      '    return nested()',
      '  }',
      '  decrement = () => 0',
      '}',
      'export function other() {',
      '  const nested = function () { return 2 }',
      '  return nested()',
      '}',
      'const handlers = {',
      '  run() { return 3 }',
      '}',
      'declare function signature(): void'
    ].join('\n')
  })
  const index = run(request)
  assert.deepEqual(index.errors, [])
  assert.deepEqual(
    index.symbols.map(({ name, kind, line, end_line }) => ({ name, kind, line, end_line })),
    [
      { name: 'Counter', kind: 'class', line: 1, end_line: 7 },
      { name: 'Counter.increment', kind: 'function', line: 2, end_line: 5 },
      { name: 'Counter.increment.nested', kind: 'function', line: 3, end_line: 3 },
      { name: 'Counter.decrement', kind: 'function', line: 6, end_line: 6 },
      { name: 'other', kind: 'function', line: 8, end_line: 11 },
      { name: 'other.nested', kind: 'function', line: 9, end_line: 9 },
      { name: 'handlers.run', kind: 'function', line: 13, end_line: 13 }
    ]
  )
  assert.equal(new Set(index.symbols.map((item) => item.id)).size, index.symbols.length)
})

test('unresolved targets and invalid syntax stay explicit without executing or following source', (t) => {
  const request = fixture(t, {
    'frontend/src/entry.js': [
      "const { writeFileSync } = require('node:fs')",
      "writeFileSync('executed.txt', 'must never execute')",
      "const missing = require('./missing')",
      'const lazy = (name) => import(`./${name}.js`)',
      "import './excluded.js'",
      "throw new Error('must never execute')"
    ].join('\n'),
    'frontend/src/excluded.js': "import './nested-missing.js'\n",
    'frontend/src/broken.ts': 'export function broken( {',
    'frontend/src/broken.vue': '<template><div></template>',
    'frontend/src/valid.ts': 'export function stillIndexed() { return 1 }'
  })
  request.files = request.files.filter((file) => !file.endsWith('excluded.js'))
  const index = run(request)
  assert.equal(existsSync(path.join(request.root, 'executed.txt')), false)
  assert.equal(
    index.symbols.some((item) => item.name === 'stillIndexed'),
    true
  )
  assert.equal(
    index.symbols.some((item) => item.name === 'broken'),
    false
  )
  assert.equal(
    index.dependencies.some((item) => item.specifier === './nested-missing.js'),
    false
  )
  assert.equal(index.dependencies.find((item) => item.specifier === 'node:fs').external, true)
  assert.equal(index.dependencies.find((item) => item.specifier === './missing').target, null)
  assert.equal(index.dependencies.find((item) => item.specifier === '`./${name}.js`').line, 4)
  assert.equal(index.dependencies.find((item) => item.specifier === './excluded.js').target, null)
  assert.ok(
    index.errors.some(
      (item) => item.path === 'frontend/src/broken.ts' && item.message.includes('第 1 行')
    )
  )
  assert.ok(index.errors.some((item) => item.path === 'frontend/src/broken.vue'))
  assert.ok(index.errors.some((item) => item.message.includes('无法静态确定')))
  assert.ok(index.errors.some((item) => item.message.includes('不在允许的文件集合')))
})

test('project composable, Vue page and router work in a source-only snapshot', (t) => {
  const names = [
    'frontend/tsconfig.app.json',
    'frontend/src/composables/useAliveGuard.ts',
    'frontend/src/views/Dashboard.vue',
    'frontend/src/router/index.ts'
  ]
  const sources = Object.fromEntries(
    names.map((file) => [file, readFileSync(path.join(repository, file), 'utf8')])
  )
  const request = fixture(t, sources)
  const index = run(request)
  assert.ok(index.symbols.some((item) => item.name === 'useAliveGuard'))
  assert.ok(
    index.dependencies.some(
      (item) =>
        item.source === 'frontend/src/views/Dashboard.vue' &&
        item.specifier === 'vue' &&
        item.external
    )
  )
  assert.ok(
    index.dependencies.some(
      (item) =>
        item.source === 'frontend/src/router/index.ts' && item.kind === 'dynamic' && item.line > 0
    )
  )
  assert.equal(
    index.dependencies.some((item) => item.line === null),
    false
  )
  assert.equal(
    index.errors.some((item) => item.message.includes('分析失败')),
    false
  )
})

test('native cycles are deduplicated hints with runtime, type-only, mixed and Vue evidence', (t) => {
  const request = fixture(t, {
    'frontend/src/runtime/a.ts': "import { b } from '@/runtime/b'\nexport const a = () => b\n",
    'frontend/src/runtime/b.ts': "import { a } from './a'\nexport const b = () => a\n",
    'frontend/src/types/a.ts': "import type { B } from './b'\nexport interface A { b: B }\n",
    'frontend/src/types/b.ts': "import type { A } from './a'\nexport interface B { a: A }\n",
    'frontend/src/mixed/a.ts':
      "import type { B } from './b'\nexport type A = B\nexport const a = 1\n",
    'frontend/src/mixed/b.ts':
      "import { a } from './a'\nexport interface B { value: number }\nexport const b = a\n",
    'frontend/src/vue/A.vue':
      '<script setup lang="ts">\nimport { b } from "./b"\n</script>\n<template>{{ b }}</template>',
    'frontend/src/vue/b.ts': "import A from './A.vue'\nexport const b = A\n",
    'frontend/src/dynamic/a.js': "export const a = () => import('./b.js')\n",
    'frontend/src/dynamic/b.js': "import { a } from './a.js'\nexport const b = a\n"
  })
  const index = run(request)
  assert.deepEqual(index.errors, [])
  assert.equal(index.cycles_truncated, false)
  assert.equal(index.cycles.length, 5)
  assert.deepEqual(
    new Set(index.cycles.map((item) => item.kind)),
    new Set(['runtime', 'type-only', 'mixed'])
  )
  for (const cycle of index.cycles) {
    assert.equal(cycle.tool, 'dependency-cruiser')
    assert.equal(cycle.paths[0], cycle.paths.at(-1))
    assert.equal(cycle.paths[0], [...cycle.paths].sort()[0])
    assert.equal(cycle.edges.length, cycle.paths.length - 1)
    for (const edge of cycle.edges) {
      assert.ok(edge.line > 0)
      assert.ok(
        index.dependencies.some((dependency) =>
          Object.entries(edge).every(([key, value]) => dependency[key] === value)
        )
      )
    }
  }
  const vue = index.cycles.find((item) => item.paths.includes('frontend/src/vue/A.vue'))
  assert.equal(vue.edges.find((edge) => edge.source.endsWith('A.vue')).line, 2)
  assert.ok(index.cycles.some((item) => item.edges.some((edge) => edge.kind === 'dynamic')))
  assert.deepEqual(run(request).cycles, index.cycles)
})

test('excluded source cannot close an allowed cycle and acyclic graphs return no hints', (t) => {
  const request = fixture(t, {
    'frontend/src/a.ts': "import { b } from './b'\nexport const a = 1\n",
    'frontend/src/b.ts': "import { a } from './a'\nexport const b = a\n"
  })
  request.files = request.files.filter((file) => !file.endsWith('/b.ts'))
  const index = run(request)
  assert.deepEqual(index.cycles, [])
  assert.equal(index.cycles_truncated, false)
  assert.ok(index.errors.some((item) => item.message.includes('不在允许的文件集合')))
})

test('native cycle hint output is bounded without turning cycles into errors', (t) => {
  const sources = {}
  for (let i = 0; i < 201; i++) {
    const name = `${i}`.padStart(3, '0')
    sources[`frontend/src/a${name}.ts`] =
      `import { b } from './b${name}'\nexport const a = () => b\n`
    sources[`frontend/src/b${name}.ts`] =
      `import { a } from './a${name}'\nexport const b = () => a\n`
  }
  const index = run(fixture(t, sources))
  assert.equal(index.cycles.length, 200)
  assert.equal(index.cycles_truncated, true)
  assert.deepEqual(index.errors, [])
})
