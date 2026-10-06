/** Function metrics for allowed source snapshots; stdin/stdout are JSON only. */
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'

// The source snapshot need not have node_modules. Never load its lint config.
const require = createRequire(new URL('../../frontend/package.json', import.meta.url))
const { Linter } = require('eslint')
const { builtinRules } = require('eslint/use-at-your-own-risk')
const tsParser = require('@typescript-eslint/parser')
const vueParser = require('vue-eslint-parser')
const nativeComplexity = builtinRules.get('complexity')
const functionTypes = new Set([
  'FunctionDeclaration',
  'FunctionExpression',
  'ArrowFunctionExpression'
])
const result = {
  functions: [],
  errors: [],
  tools: Object.fromEntries(
    ['eslint', '@typescript-eslint/parser', 'vue-eslint-parser'].map((name) => [
      name,
      require(`${name}/package.json`).version
    ])
  )
}

function localName(node, source) {
  if (node.id) return source.getText(node.id)
  const parent = node.parent
  if (parent?.type === 'VariableDeclarator') return source.getText(parent.id)
  if (parent?.key && parent.value === node) return source.getText(parent.key)
  if (parent?.type === 'ExportDefaultDeclaration') return 'default'
  return `<anonymous@${node.loc.start.line}:${node.loc.start.column + 1}>`
}

function qualifiedName(node, source) {
  const names = [localName(node, source)]
  for (let parent = node.parent; parent; parent = parent.parent) {
    if (
      functionTypes.has(parent.type) ||
      parent.type === 'ClassDeclaration' ||
      parent.type === 'ClassExpression' ||
      parent.type === 'TSModuleDeclaration'
    ) {
      names.unshift(localName(parent, source))
    } else if (
      parent.type === 'ObjectExpression' &&
      (parent.parent?.type === 'VariableDeclarator' || parent.parent?.key)
    ) {
      names.unshift(localName(parent, source))
    }
  }
  return names.join('.')
}

function collectFunctions(file, content) {
  const functions = []
  const rule = {
    meta: nativeComplexity.meta,
    create(context) {
      let origin
      // ESLint's JSON formatter drops data.complexity and reports only the head
      // location. Intercept the native descriptor, retaining the complete node.
      const delegated = Object.create(context, {
        report: {
          value(report) {
            // A field callback is reported once as a real function and again as
            // a field initializer, so checking node.type alone is insufficient.
            if (origin !== 'function') return
            const { node, data } = report
            if (!functionTypes.has(node.type) || !Number.isInteger(data.complexity)) {
              throw new Error('ESLint complexity 接口无法识别函数范围或复杂度')
            }
            functions.push({
              node,
              row: {
                path: file,
                name: qualifiedName(node, context.sourceCode),
                line: node.loc.start.line,
                end_line: node.loc.end.line,
                column: node.loc.start.column + 1,
                end_column: node.loc.end.column + 1,
                cc: data.complexity,
                coverage: null,
                crap: null,
                status: 'unknown',
                reason: null
              }
            })
          }
        }
      })
      const listeners = nativeComplexity.create(delegated)
      return {
        ...listeners,
        onCodePathEnd(codePath, node) {
          origin = codePath.origin
          try {
            return listeners.onCodePathEnd(codePath, node)
          } finally {
            origin = undefined
          }
        }
      }
    }
  }
  const messages = new Linter().verify(
    content,
    [
      {
        files: ['**/*.{js,jsx,ts,tsx,mjs,cjs,mts,cts,vue}'],
        languageOptions: {
          parser: file.endsWith('.vue') ? vueParser : tsParser,
          parserOptions: {
            parser: tsParser,
            ecmaVersion: 'latest',
            sourceType: 'module',
            ecmaFeatures: { jsx: true }
          }
        },
        plugins: { metrics: { rules: { complexity: rule } } },
        rules: { 'metrics/complexity': ['error', { max: 0, variant: 'classic' }] }
      }
    ],
    { filename: file, allowInlineConfig: false }
  )
  if (messages.length) {
    throw new Error(messages.map((item) => `第 ${item.line ?? '?'} 行：${item.message}`).join('; '))
  }
  return functions
}

function record(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function reportPath(root, file) {
  if (typeof file !== 'string') return null
  const relative = path.isAbsolute(file) ? path.relative(root, file) : file
  if (relative.startsWith('../') || relative === '..' || path.isAbsolute(relative)) return null
  return relative
}

function fileCoverage(root, file, coverage) {
  if (coverage == null) return { reason: '未提供覆盖率报告' }
  if (!record(coverage)) return { reason: 'Istanbul 覆盖率文件数据无效' }
  const matches = Object.entries(coverage).filter(([key]) => reportPath(root, key) === file)
  if (!matches.length) return { reason: '覆盖率报告不包含该文件' }
  if (matches.length !== 1 || !record(matches[0][1])) {
    return { reason: '覆盖率文件身份不明确或数据无效' }
  }
  const data = matches[0][1]
  if (data.path !== undefined && reportPath(root, data.path) !== file) {
    return { reason: '覆盖率文件路径与报告键不一致' }
  }
  if (data.inputSourceMap != null) {
    return { reason: '覆盖率仍包含待处理的 source map，需先映射回原始源码' }
  }
  return { data }
}

function assignCoverage(functions, content, data) {
  if (!record(data.statementMap) || !record(data.s)) return '覆盖率报告缺少语句位置或命中数据'
  const ids = Object.keys(data.statementMap)
  if (ids.length !== Object.keys(data.s).length) return '覆盖率语句与命中数据不完整'
  const lines = content.split(/\r\n|[\n\r\u2028\u2029]/u)
  const compare = (a, b) => a.line - b.line || a.column - b.column
  const validPosition = (position) =>
    record(position) &&
    Number.isInteger(position.line) &&
    position.line >= 1 &&
    position.line <= lines.length &&
    Number.isInteger(position.column) &&
    position.column >= 0 &&
    position.column <= lines[position.line - 1].length
  const bySize = [...functions].sort(
    (a, b) => a.node.range[1] - a.node.range[0] - (b.node.range[1] - b.node.range[0])
  )
  const hits = new Map(functions.map((fn) => [fn, new Map()]))
  for (const id of ids) {
    const location = data.statementMap[id]
    const count = data.s[id]
    // istanbul-lib-source-maps uses Infinity for an original range extending
    // to the line end; JSON serializes that column as null. Start columns must
    // remain exact, since they determine the executable line and its owner.
    const openEnd = record(location?.end) && location.end.column === null
    const end = openEnd
      ? { ...location.end, column: lines[location.end.line - 1]?.length }
      : location?.end
    if (
      !record(location) ||
      !validPosition(location.start) ||
      !validPosition(end) ||
      compare(location.start, end) >= 0 ||
      !Number.isSafeInteger(count) ||
      count < 0
    ) {
      return '覆盖率语句位置或命中次数无效，不能可靠映射原始源码'
    }
    const owner = bySize.find(
      ({ node }) =>
        // Istanbul also counts creation of a function expression, using the
        // whole function as the initializer's location. That belongs to the
        // enclosing scope, not to execution of the newly created function.
        compare(node.loc.start, location.start) < 0 && compare(location.start, node.loc.end) < 0
    )
    if (!owner) continue // Module scope and Vue template are outside real functions.
    if (end.line > owner.node.loc.end.line || (!openEnd && compare(end, owner.node.loc.end) > 0)) {
      return '覆盖率语句跨越函数边界，不能可靠分配执行行'
    }
    // Match Istanbul line semantics: statement *start* lines, with any hit on
    // the line counting as covered. fnMap/f invocation counters are not coverage.
    const observed = hits.get(owner)
    observed.set(location.start.line, Math.max(observed.get(location.start.line) ?? 0, count))
  }
  for (const { row } of functions) row.reason = '覆盖率报告不包含该函数的可执行行范围'
  for (const [fn, observed] of hits) {
    if (!observed.size) continue
    const ratio = [...observed.values()].filter((count) => count > 0).length / observed.size
    Object.assign(fn.row, {
      coverage: ratio,
      crap: fn.row.cc ** 2 * (1 - ratio) ** 3 + fn.row.cc,
      status: 'measured',
      reason: null
    })
  }
  return null
}

function analyze({ root, files, coverage }) {
  if (
    typeof root !== 'string' ||
    !Array.isArray(files) ||
    files.some((file) => typeof file !== 'string')
  ) {
    throw new Error('请求必须包含 root 字符串与 files 数组')
  }
  root = path.resolve(root)
  for (const file of [...new Set(files)].sort()) {
    if (!/\.(?:[cm]?[jt]sx?|vue)$/.test(file)) continue
    try {
      const content = readFileSync(path.join(root, file), 'utf8')
      const functions = collectFunctions(file, content)
      const matched = fileCoverage(root, file, coverage)
      const reason = matched.reason ?? assignCoverage(functions, content, matched.data)
      if (reason) for (const { row } of functions) row.reason = reason
      result.functions.push(...functions.map(({ row }) => row))
    } catch (issue) {
      result.errors.push({ path: file, message: `前端指标解析失败：${issue.message}` })
    }
  }
  result.functions.sort(
    (a, b) => a.path.localeCompare(b.path) || a.line - b.line || a.column - b.column
  )
}

try {
  let request = ''
  process.stdin.setEncoding('utf8')
  for await (const chunk of process.stdin) request += chunk
  analyze(JSON.parse(request))
} catch (issue) {
  result.errors.push({ path: null, message: issue.message })
  process.exitCode = 1
}
process.stdout.write(`${JSON.stringify(result)}\n`)
