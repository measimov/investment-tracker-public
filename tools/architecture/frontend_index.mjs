/** Static frontend index. JSON request/response contract: see CONTRACT.md. */
import { readFileSync, existsSync } from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

// The source root may be a snapshot without installed dependencies. Tools always
// come from this checkout, never from executable configuration in the snapshot.
const frontend = fileURLToPath(new URL('../../frontend/', import.meta.url))
const require = createRequire(path.join(frontend, 'package.json'))
const ts = require('typescript')
const vue = require('@vue/compiler-sfc')
const cruiserDirectory = path.join(frontend, 'node_modules/dependency-cruiser')
const cruiserPackage = JSON.parse(readFileSync(path.join(cruiserDirectory, 'package.json'), 'utf8'))
const { cruise } = await import(pathToFileURL(path.join(cruiserDirectory, cruiserPackage.main)))

const result = {
  symbols: [],
  dependencies: [],
  cycles: [],
  cycles_truncated: false,
  errors: [],
  tools: {
    'dependency-cruiser': cruiserPackage.version,
    typescript: ts.version,
    '@vue/compiler-sfc': vue.version
  }
}

function error(file, message) {
  result.errors.push({ path: file, message })
}

function cycleHints(modules) {
  // Native witnesses only, not every simple cycle. paths repeats its first
  // member at the end; edges retain original runtime/type/dynamic evidence.
  // Runtime cycles may include dynamic imports. These are hints, never errors.
  const pairs = new Map()
  for (const edge of result.dependencies) {
    if (edge.target === null || edge.external) continue
    const key = JSON.stringify([edge.source, edge.target])
    if (!pairs.has(key)) pairs.set(key, [])
    pairs.get(key).push(edge)
  }
  const seen = new Set()
  for (const module of modules) {
    for (const dependency of module.dependencies) {
      if (!dependency.circular || !dependency.cycle?.length) continue
      const paths = [module.source, ...dependency.cycle.map((edge) => edge.name)]
      if (paths[0] !== paths.at(-1)) continue
      const edges = dependency.cycle.map((native, index) => {
        const candidates = pairs.get(JSON.stringify([paths[index], native.name])) ?? []
        const isType = native.dependencyTypes?.includes('type-only')
        return candidates.find((edge) => (edge.kind === 'type') === Boolean(isType))
      })
      if (edges.some((edge) => !edge)) continue // No excluded/unresolved edges in a witness.
      const kinds = new Set(edges.map((edge) => (edge.kind === 'type' ? 'type' : 'runtime')))
      const kind = kinds.size > 1 ? 'mixed' : kinds.has('type') ? 'type-only' : 'runtime'
      const body = paths.slice(0, -1)
      const first = body.indexOf([...body].sort()[0])
      const rotated = [...body.slice(first), ...body.slice(0, first)]
      const closed = [...rotated, rotated[0]]
      const key = JSON.stringify([kind, closed])
      if (seen.has(key)) continue
      if (result.cycles.length >= 200) {
        result.cycles_truncated = true
        return
      }
      seen.add(key)
      result.cycles.push({
        tool: 'dependency-cruiser',
        kind,
        paths: closed,
        edges: [...edges.slice(first), ...edges.slice(0, first)].map(
          ({ source, target, kind, line }) => ({ source, target, kind, line })
        )
      })
    }
  }
  result.cycles.sort(
    (a, b) =>
      a.kind.localeCompare(b.kind) || JSON.stringify(a.paths).localeCompare(JSON.stringify(b.paths))
  )
}

function importKinds(clause, elements) {
  if (clause?.isTypeOnly) return ['type']
  if (!elements?.length) return ['runtime']
  const kinds = new Set(elements.map((element) => (element.isTypeOnly ? 'type' : 'runtime')))
  if (clause?.name) kinds.add('runtime')
  return [...kinds]
}

function sourceEvidence(file, content, offset, language) {
  const scriptKind = {
    ts: ts.ScriptKind.TS,
    tsx: ts.ScriptKind.TSX,
    js: ts.ScriptKind.JS,
    jsx: ts.ScriptKind.JSX
  }[language]
  const ast = ts.createSourceFile(file, content, ts.ScriptTarget.Latest, true, scriptKind)
  const imports = []
  const symbols = []
  const line = (position) => ast.getLineAndCharacterOfPosition(position).line + 1 + offset

  for (const diagnostic of ast.parseDiagnostics) {
    error(
      file,
      `第 ${line(diagnostic.start ?? 0)} 行：${ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n')}`
    )
  }
  if (ast.parseDiagnostics.length) return { imports, symbols, valid: false }

  function dependency(node, argument, kinds, syntax) {
    const literal =
      argument && (ts.isStringLiteralLike(argument) || ts.isNoSubstitutionTemplateLiteral(argument))
    for (const kind of kinds) {
      imports.push({
        specifier: literal ? argument.text : (argument?.getText(ast) ?? '<unknown>'),
        line: line(node.getStart(ast)),
        kind,
        syntax,
        literal: Boolean(literal)
      })
    }
  }

  function declarationName(node) {
    if (ts.isConstructorDeclaration(node)) return 'constructor'
    if (node.name) return node.name.getText(ast)
    if (node.parent?.initializer === node && node.parent.name) return node.parent.name.getText(ast)
    if (ts.isExportAssignment(node.parent)) return 'default'
    const position = ast.getLineAndCharacterOfPosition(node.getStart(ast))
    return `<anonymous@${position.line + 1 + offset}:${position.character + 1}>`
  }

  function walk(node, scope = []) {
    if (ts.isImportDeclaration(node)) {
      dependency(
        node,
        node.moduleSpecifier,
        importKinds(node.importClause, node.importClause?.namedBindings?.elements),
        'import'
      )
    } else if (ts.isExportDeclaration(node) && node.moduleSpecifier) {
      dependency(
        node,
        node.moduleSpecifier,
        importKinds(node, node.exportClause?.elements),
        'export'
      )
    } else if (
      ts.isImportEqualsDeclaration(node) &&
      ts.isExternalModuleReference(node.moduleReference)
    ) {
      dependency(
        node,
        node.moduleReference.expression,
        node.isTypeOnly ? ['type'] : ['runtime'],
        'import-equals'
      )
    } else if (ts.isImportTypeNode(node) && ts.isLiteralTypeNode(node.argument)) {
      dependency(node, node.argument.literal, ['type'], 'type-import')
    } else if (ts.isCallExpression(node)) {
      if (node.expression.kind === ts.SyntaxKind.ImportKeyword) {
        dependency(node, node.arguments[0], ['dynamic'], 'dynamic-import')
      } else if (ts.isIdentifier(node.expression) && node.expression.text === 'require') {
        dependency(node, node.arguments[0], ['runtime'], 'require')
      }
    }

    const isClass = ts.isClassDeclaration(node) || ts.isClassExpression(node)
    const isFunction = ts.isFunctionLike(node) && Boolean(node.body)
    let childScope = scope
    if (isClass || isFunction) {
      const name = [...scope, declarationName(node)].join('.')
      const start = line(node.getStart(ast))
      symbols.push({
        id: `${file}:${name}:${start}`,
        path: file,
        name,
        kind: isClass ? 'class' : 'function',
        line: start,
        end_line: line(Math.max(node.getStart(ast), node.getEnd() - 1))
      })
      childScope = [...scope, declarationName(node)]
    } else if (ts.isModuleDeclaration(node)) {
      childScope = [...scope, declarationName(node)]
    } else if (ts.isObjectLiteralExpression(node) && node.parent?.name) {
      childScope = [...scope, node.parent.name.getText(ast)]
    }
    ts.forEachChild(node, (child) => walk(child, childScope))
  }

  walk(ast)
  return { imports, symbols, valid: true }
}

function inspect(file) {
  const content = readFileSync(file, 'utf8')
  if (!file.endsWith('.vue')) {
    const extension = path.extname(file).slice(1)
    return sourceEvidence(
      file,
      content,
      0,
      ['js', 'jsx', 'tsx'].includes(extension) ? extension : 'ts'
    )
  }

  const { descriptor, errors } = vue.parse(content, { filename: file })
  for (const issue of errors) error(file, String(issue.message ?? issue))
  const evidence = { imports: [], symbols: [], valid: errors.length === 0 }
  for (const block of [descriptor.script, descriptor.scriptSetup].filter(Boolean)) {
    if (block.src) {
      // compiler-sfc exposes the content range, not the src attribute range.
      evidence.imports.push({ specifier: block.src, line: null, kind: 'runtime', literal: true })
      error(file, `无法定位外部 script 的 src 属性行号：${block.src}`)
    }
    const language = block.lang ?? 'js'
    if (!['ts', 'tsx', 'js', 'jsx'].includes(language)) {
      error(file, `不支持的 Vue script 语言：${language}`)
      evidence.valid = false
      continue
    }
    const parsed = sourceEvidence(file, block.content, block.loc.start.line - 1, language)
    evidence.imports.push(...parsed.imports)
    evidence.symbols.push(...parsed.symbols)
    evidence.valid &&= parsed.valid
  }
  return evidence
}

async function analyze({ root, files }) {
  if (
    typeof root !== 'string' ||
    !Array.isArray(files) ||
    files.some((file) => typeof file !== 'string')
  ) {
    throw new Error('请求必须包含 root 字符串与 files 数组')
  }
  process.chdir(root)
  const allowed = new Set(files)
  const evidence = new Map()
  for (const file of [...allowed].sort()) {
    if (!/\.(?:[cm]?[jt]sx?|vue)$/.test(file)) continue
    try {
      const parsed = inspect(file)
      evidence.set(file, parsed)
      result.symbols.push(...parsed.symbols)
    } catch (issue) {
      error(file, `读取或解析失败：${issue.message}`)
    }
  }

  const validFiles = [...evidence].filter(([, parsed]) => parsed.valid).map(([file]) => file)
  let modules = []
  if (validFiles.length) {
    const configFile = path.resolve('frontend/tsconfig.app.json')
    let tsConfig
    if (existsSync(configFile)) {
      const config = ts.readConfigFile(configFile, ts.sys.readFile)
      if (config.error)
        throw new Error(ts.flattenDiagnosticMessageText(config.error.messageText, '\n'))
      // Alias resolution remains entirely in dependency-cruiser's tsconfig
      // plugin. Compiler flags are read as data; no Vite/config JS is executed.
      const converted = ts.convertCompilerOptionsFromJson(
        config.config.compilerOptions ?? {},
        path.dirname(configFile)
      )
      for (const issue of converted.errors)
        error(
          'frontend/tsconfig.app.json',
          ts.flattenDiagnosticMessageText(issue.messageText, '\n')
        )
      tsConfig = { options: converted.options }
    }
    try {
      const output = await cruise(
        validFiles,
        {
          baseDir: process.cwd(),
          doNotFollow: '.*',
          tsPreCompilationDeps: true,
          parser: 'tsc',
          skipAnalysisNotInRules: true,
          validate: false,
          ruleSet: {
            forbidden: [
              {
                name: 'architecture-cycle-hint',
                severity: 'info',
                from: {},
                to: { circular: true }
              }
            ]
          },
          cache: false
        },
        {
          ...(tsConfig ? { tsConfig: configFile } : {}),
          modules: [path.join(frontend, 'node_modules'), 'node_modules'],
          exportsFields: ['exports'],
          conditionNames: ['import', 'require', 'node', 'default']
        },
        { tsConfig }
      )
      modules = output.output.modules
    } catch (issue) {
      error(null, `dependency-cruiser 分析失败：${issue.message}`)
    }
  }

  const bySource = new Map(
    modules
      .filter((module) => evidence.has(module.source))
      .map((module) => [module.source, module.dependencies])
  )
  for (const [file, parsed] of evidence) {
    const native = bySource.get(file) ?? []
    const matched = new Set()
    for (const occurrence of parsed.imports) {
      const candidates = native.filter(
        (item) => `${item.protocol ?? ''}${item.module}` === occurrence.specifier
      )
      const syntactic = candidates.filter((item) =>
        item.dependencyTypes?.includes(occurrence.syntax)
      )
      const dependency =
        syntactic.find(
          (item) => item.dependencyTypes.includes('type-only') === (occurrence.kind === 'type')
        ) ??
        syntactic[0] ??
        candidates.find((item) => Boolean(item.dynamic) === (occurrence.kind === 'dynamic')) ??
        candidates[0]
      if (dependency) matched.add(dependency)
      const external = Boolean(
        dependency?.coreModule ||
        dependency?.dependencyTypes?.some((kind) => kind.startsWith('npm'))
      )
      let target = null
      if (!occurrence.literal) {
        error(file, `第 ${occurrence.line} 行的导入目标无法静态确定：${occurrence.specifier}`)
      } else if (!dependency || dependency.couldNotResolve) {
        error(file, `无法解析依赖：${occurrence.specifier}（行 ${occurrence.line ?? '未知'}）`)
      } else if (!external) {
        if (allowed.has(dependency.resolved)) target = dependency.resolved
        else error(file, `依赖目标不在允许的文件集合中：${occurrence.specifier}`)
      }
      result.dependencies.push({
        source: file,
        target,
        specifier: occurrence.specifier,
        line: occurrence.line,
        kind: occurrence.kind,
        external
      })
    }
    for (const dependency of native) {
      if (matched.has(dependency)) continue
      const external = Boolean(
        dependency.coreModule || dependency.dependencyTypes?.some((kind) => kind.startsWith('npm'))
      )
      result.dependencies.push({
        source: file,
        target:
          !dependency.couldNotResolve && !external && allowed.has(dependency.resolved)
            ? dependency.resolved
            : null,
        specifier: `${dependency.protocol ?? ''}${dependency.module}`,
        line: null,
        kind: dependency.dynamic
          ? 'dynamic'
          : dependency.dependencyTypes?.includes('type-only')
            ? 'type'
            : 'runtime',
        external
      })
      error(file, `无法定位依赖的原始行号：${dependency.module}`)
    }
  }
  cycleHints(modules)
}

try {
  let request = ''
  process.stdin.setEncoding('utf8')
  for await (const chunk of process.stdin) request += chunk
  await analyze(JSON.parse(request))
} catch (issue) {
  error(null, issue.message)
  process.exitCode = 1
}
result.symbols.sort(
  (a, b) => a.path.localeCompare(b.path) || a.line - b.line || a.name.localeCompare(b.name)
)
result.dependencies.sort(
  (a, b) =>
    a.source.localeCompare(b.source) ||
    (a.line ?? Infinity) - (b.line ?? Infinity) ||
    a.specifier.localeCompare(b.specifier) ||
    a.kind.localeCompare(b.kind)
)
process.stdout.write(`${JSON.stringify(result)}\n`)
