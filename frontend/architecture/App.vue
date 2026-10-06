<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { use } from 'echarts/core'
import { GraphChart } from 'echarts/charts'
import { TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'
import SourcePane from './SourcePane.vue'
import CyclesPane from './CyclesPane.vue'
import QualityPane from './QualityPane.vue'
import type { ArchitectureIndex, Dependency } from './types'
import type { QualityFunction, QualitySelection, QualitySourceLocation } from './quality-types'

use([GraphChart, TooltipComponent, CanvasRenderer])
const UmlGraph = defineAsyncComponent(() => import('./UmlGraph.vue'))

type Panel = 'directory' | 'graph' | 'source'
interface DirectoryEntry {
  path: string
  directory: boolean
  count: number
}
interface NodeQuality {
  measured: number
  applicable: number
  unknown: number
  notApplicable: number
  aboveThreshold: number
  maxCrap: number | null
}

const snapshot = ref<ArchitectureIndex | null>(null)
const loading = ref(true)
const loadError = ref('')
const directory = ref('')
const selectedFile = ref<string | null>(null)
const sourceFile = ref<string | null>(null)
const sourceLine = ref<number | null>(null)
const query = ref('')
const panel = ref<Panel>('graph')
const graphView = ref<'relations' | 'uml'>('relations')
const view = ref<'structure' | 'cycles' | 'quality'>('structure')
const qualitySelection = ref<QualitySelection>({ report: null, threshold: 30 })
const riskColors = ref(false)
const direction = ref<'outgoing' | 'incoming'>('outgoing')
const dependencyKind = ref<'all' | Dependency['kind']>('all')
const focusedEdges = ref<Dependency[] | null>(null)
const diagnosticsLimit = ref(100)
const evidenceLimit = ref(100)
const moduleLinkLimit = ref(30)
let controller: AbortController | null = null

const files = computed(() => snapshot.value?.files ?? [])
const fileHashes = computed(() => new Map(files.value.map((file) => [file.path, file.sha256])))
const qualityUnavailable = computed(() => {
  const report = qualitySelection.value.report
  if (!report?.quality) return '请选择一份已采集函数指标的报告。'
  if (!report.source.can_navigate || report.source.status !== 'current') {
    return '所选报告与当前源码未匹配，保留结构颜色。'
  }
  if (
    report.quality.functions.some(
      (row) =>
        !report.quality!.source.files[row.path] ||
        fileHashes.value.get(row.path) !== report.quality!.source.files[row.path]
    )
  )
    return '报告指纹与当前索引不一致，请重新扫描后核对。'
  return ''
})
const riskOverlay = computed(() => riskColors.value && !qualityUnavailable.value)
watch(qualityUnavailable, (reason) => {
  if (reason) riskColors.value = false
})
const filePaths = computed(() => new Set(files.value.map((file) => file.path)))
const sourceHash = computed(
  () => files.value.find((file) => file.path === sourceFile.value)?.sha256 ?? null
)
const allEdges = computed(() => snapshot.value?.dependencies ?? [])
const edges = computed(() =>
  allEdges.value.filter(
    (edge) => dependencyKind.value === 'all' || edge.kind === dependencyKind.value
  )
)
const currentSymbols = computed(() =>
  (snapshot.value?.symbols ?? []).filter((symbol) => symbol.path === sourceFile.value)
)
const outgoing = computed(() => edges.value.filter((edge) => edge.source === selectedFile.value))
const incoming = computed(() => edges.value.filter((edge) => edge.target === selectedFile.value))
const visibleEdges = computed(
  () => focusedEdges.value ?? (direction.value === 'outgoing' ? outgoing.value : incoming.value)
)
const totalResolved = computed(() => allEdges.value.filter((edge) => edge.target).length)
const searchText = computed(() => query.value.trim().toLocaleLowerCase())
const matchedFiles = computed(() =>
  searchText.value
    ? files.value.filter((file) => file.path.toLocaleLowerCase().includes(searchText.value))
    : []
)
const matchedSymbols = computed(() =>
  searchText.value
    ? (snapshot.value?.symbols ?? []).filter((symbol) =>
        `${symbol.name} ${symbol.path}`.toLocaleLowerCase().includes(searchText.value)
      )
    : []
)
const breadcrumbs = computed(() => {
  const parts = directory.value ? directory.value.split('/') : []
  return parts.map((name, index) => ({ name, path: parts.slice(0, index + 1).join('/') }))
})
const scannedAt = computed(() => {
  if (!snapshot.value) return ''
  const timestamp = new Date(snapshot.value.scanned_at)
  return Number.isNaN(timestamp.getTime()) ? '时间未知' : timestamp.toLocaleString('zh-CN')
})
const scopedDiagnostics = computed(() =>
  (snapshot.value?.errors ?? []).filter((error) => {
    if (!error.path) return true
    if (selectedFile.value) return error.path === selectedFile.value
    return (
      !directory.value ||
      error.path === directory.value ||
      error.path.startsWith(`${directory.value}/`)
    )
  })
)

function nameOf(path: string) {
  return path.split('/').at(-1) || path
}

function parentOf(path: string) {
  return path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : ''
}

function bucket(path: string) {
  const prefix = directory.value ? `${directory.value}/` : ''
  if (!path.startsWith(prefix)) return null
  return prefix + path.slice(prefix.length).split('/')[0]
}

const children = computed<DirectoryEntry[]>(() => {
  const entries = new Map<string, DirectoryEntry>()
  for (const file of files.value) {
    const path = bucket(file.path)
    if (!path) continue
    const entry = entries.get(path) ?? { path, directory: path !== file.path, count: 0 }
    entry.count++
    entries.set(path, entry)
  }
  return [...entries.values()].sort(
    (a, b) => Number(b.directory) - Number(a.directory) || a.path.localeCompare(b.path)
  )
})

const graph = computed(() => {
  const nodes: DirectoryEntry[] = []
  const groupedEdges = new Map<string, { source: string; target: string; evidence: Dependency[] }>()
  if (selectedFile.value) {
    const neighbors = new Set([selectedFile.value])
    for (const edge of [...outgoing.value, ...incoming.value]) {
      if (edge.target && filePaths.value.has(edge.target)) neighbors.add(edge.target)
      if (filePaths.value.has(edge.source)) neighbors.add(edge.source)
    }
    nodes.push(...[...neighbors].map((path) => ({ path, directory: false, count: 1 })))
  } else {
    nodes.push(...children.value)
  }
  const displayed = nodes.slice(0, 45)
  const ids = new Set(displayed.map((node) => node.path))
  for (const edge of edges.value) {
    if (!edge.target) continue
    if (
      selectedFile.value &&
      edge.source !== selectedFile.value &&
      edge.target !== selectedFile.value
    )
      continue
    const source = selectedFile.value ? edge.source : bucket(edge.source)
    const target = selectedFile.value ? edge.target : bucket(edge.target)
    if (!source || !target || source === target || !ids.has(source) || !ids.has(target)) continue
    const key = `${source}\0${target}`
    const group = groupedEdges.get(key) ?? { source, target, evidence: [] }
    group.evidence.push(edge)
    groupedEdges.set(key, group)
  }
  return { nodes: displayed, total: nodes.length, links: [...groupedEdges.entries()] }
})

const graphQuality = computed(() => {
  const report = qualitySelection.value.report?.quality
  const result = new Map<string, NodeQuality>()
  if (!report || qualityUnavailable.value) return result
  const byFile = new Map<string, QualityFunction[]>()
  for (const row of report.functions) {
    const rows = byFile.get(row.path) ?? []
    rows.push(row)
    byFile.set(row.path, rows)
  }
  for (const node of graph.value.nodes) {
    const stats: NodeQuality = {
      measured: 0,
      applicable: 0,
      unknown: 0,
      notApplicable: 0,
      aboveThreshold: 0,
      maxCrap: null
    }
    for (const [path, rows] of byFile) {
      if (node.directory ? !path.startsWith(`${node.path}/`) : path !== node.path) continue
      for (const row of rows) {
        if (row.status === 'not_applicable') {
          stats.notApplicable++
          continue
        }
        stats.applicable++
        if (
          report.groups[row.group]?.status === 'current' &&
          row.status === 'measured' &&
          typeof row.crap === 'number' &&
          Number.isFinite(row.crap) &&
          row.crap >= 0 &&
          typeof row.coverage === 'number' &&
          Number.isFinite(row.coverage) &&
          row.coverage >= 0 &&
          row.coverage <= 1
        ) {
          stats.measured++
          stats.maxCrap = Math.max(stats.maxCrap ?? row.crap, row.crap)
          if (row.crap >= qualitySelection.value.threshold) stats.aboveThreshold++
        } else stats.unknown++
      }
    }
    result.set(node.path, stats)
  }
  return result
})

function riskColor(path: string) {
  const stats = graphQuality.value.get(path)
  if (stats?.aboveThreshold) return '#c1843e'
  if (!stats?.measured || stats.unknown) return '#89979e'
  return '#6e9b83'
}

function qualityDescription(path: string) {
  const stats = graphQuality.value.get(path)
  if (!stats || (!stats.applicable && !stats.notApplicable)) return '没有函数指标，风险未知'
  if (!stats.applicable) return `${stats.notApplicable} 个函数不适用，未赋风险分数`
  const ratio = `${((stats.measured / stats.applicable) * 100).toFixed(1)}%`
  return `最高函数 CRAP ${stats.maxCrap === null ? '未知' : stats.maxCrap.toFixed(2)} · 达到阈值 ${stats.aboveThreshold} 个\n已评估 ${stats.measured}/${stats.applicable}（${ratio}）· 未知 ${stats.unknown}${stats.notApplicable ? ` · 不适用 ${stats.notApplicable}` : ''}`
}

const graphOption = computed(() => ({
  animation: false,
  tooltip: { trigger: 'item', renderMode: 'richText', confine: true },
  series: [
    {
      type: 'graph',
      layout: 'force',
      roam: true,
      draggable: true,
      force: { repulsion: 280, edgeLength: [90, 150], gravity: 0.12, layoutAnimation: false },
      scaleLimit: { min: 0.4, max: 2.5 },
      edgeSymbol: ['none', 'arrow'],
      edgeSymbolSize: [0, 6],
      label: { show: true, position: 'bottom', color: '#41524f', fontSize: 11, distance: 8 },
      lineStyle: { color: '#a8bdb7', width: 1.3, curveness: 0.12, opacity: 0.7 },
      emphasis: { focus: 'adjacency', lineStyle: { color: '#176b58', width: 2.5 } },
      data: graph.value.nodes.map((node) => ({
        id: node.path,
        name: riskOverlay.value ? `${node.path}\n${qualityDescription(node.path)}` : node.path,
        nodePath: node.path,
        directory: node.directory,
        symbol: node.directory ? 'roundRect' : 'circle',
        symbolSize: node.directory ? [48, 32] : node.path === selectedFile.value ? 31 : 18,
        label: {
          formatter:
            nameOf(node.path).length > 19 ? `${nameOf(node.path).slice(0, 17)}…` : nameOf(node.path)
        },
        itemStyle: {
          color: riskOverlay.value
            ? riskColor(node.path)
            : node.path === selectedFile.value
              ? '#dc9d45'
              : node.directory
                ? '#176b58'
                : '#83a59a',
          borderWidth: 3,
          borderColor:
            riskOverlay.value && node.path === selectedFile.value ? '#243b35' : '#ffffff',
          borderType:
            riskOverlay.value && graphQuality.value.get(node.path)?.unknown ? 'dashed' : 'solid'
        }
      })),
      links: graph.value.links.map(([key, group]) => ({
        source: group.source,
        target: group.target,
        name: `${group.source} → ${group.target} · ${group.evidence.length} 条导入`,
        edgeKey: key,
        lineStyle: { width: Math.min(1 + Math.log2(group.evidence.length + 1), 4) }
      }))
    }
  ]
}))

function openDirectory(path: string) {
  directory.value = path
  selectedFile.value = null
  sourceFile.value = null
  sourceLine.value = null
  focusedEdges.value = null
  diagnosticsLimit.value = 100
  evidenceLimit.value = 100
  moduleLinkLimit.value = 30
  query.value = ''
  panel.value = 'graph'
}

function openFile(path: string, line: number | null = null) {
  if (!filePaths.value.has(path)) return
  directory.value = parentOf(path)
  selectedFile.value = path
  sourceFile.value = path
  sourceLine.value = line
  focusedEdges.value = null
  diagnosticsLimit.value = 100
  evidenceLimit.value = 100
  query.value = ''
  panel.value = line === null ? 'graph' : 'source'
}

function showEvidence(edge: Dependency) {
  sourceFile.value = edge.source
  sourceLine.value = edge.line
  panel.value = 'source'
}

function openVerifiedSource(location: QualitySourceLocation) {
  if (files.value.find((file) => file.path === location.path)?.sha256 !== location.sha256) return
  openFile(location.path, location.line)
  view.value = 'structure'
}

function handleGraphClick(event: { dataType?: string; data?: unknown }) {
  const data = event.data as
    | { nodePath?: string; directory?: boolean; edgeKey?: string }
    | undefined
  if (event.dataType === 'edge' && data?.edgeKey) {
    selectLink(data.edgeKey)
  } else if (data?.nodePath) {
    if (data.directory) openDirectory(data.nodePath)
    else openFile(data.nodePath)
  }
}

function selectLink(key: string) {
  const group = graph.value.links.find(([edgeKey]) => edgeKey === key)?.[1]
  if (!group) return
  focusedEdges.value = group.evidence
  evidenceLimit.value = 100
  if (group.evidence[0]) showEvidence(group.evidence[0])
}

function setDirection(value: 'outgoing' | 'incoming') {
  direction.value = value
  focusedEdges.value = null
  evidenceLimit.value = 100
}

function returnToDirectory() {
  selectedFile.value = null
  focusedEdges.value = null
}

function kindLabel(kind: Dependency['kind']) {
  return { runtime: '运行时', type: '仅类型', dynamic: '延迟 / 动态' }[kind]
}

async function loadIndex(rebuild = false) {
  controller?.abort()
  const requestController = new AbortController()
  controller = requestController
  loading.value = true
  loadError.value = ''
  try {
    const response = await fetch(rebuild ? '/api/reindex' : '/api/index', {
      method: rebuild ? 'POST' : 'GET',
      signal: requestController.signal
    })
    if (controller !== requestController || requestController.signal.aborted) return
    const body = await response.json()
    if (controller !== requestController || requestController.signal.aborted) return
    if (!response.ok)
      throw new Error(typeof body.detail === 'string' ? body.detail : '索引读取失败')
    if (
      !Array.isArray(body.files) ||
      !Array.isArray(body.dependencies) ||
      !Array.isArray(body.symbols)
    ) {
      throw new Error('服务返回的索引格式不完整')
    }
    snapshot.value = body as ArchitectureIndex
    focusedEdges.value = null
    if (selectedFile.value && !filePaths.value.has(selectedFile.value)) openDirectory('')
    if (
      directory.value &&
      !files.value.some((file) => file.path.startsWith(`${directory.value}/`))
    ) {
      openDirectory('')
    }
  } catch (error) {
    if (controller !== requestController || requestController.signal.aborted) return
    if (error instanceof DOMException && error.name === 'AbortError') return
    loadError.value = error instanceof Error ? error.message : '无法连接架构服务，请稍后重试'
  } finally {
    if (controller === requestController && !requestController.signal.aborted) loading.value = false
  }
}

onMounted(() => loadIndex())
onBeforeUnmount(() => controller?.abort())
</script>

<template>
  <div class="architecture-app">
    <a class="skip-link" :href="view === 'structure' ? '#workspace' : `#${view}-workspace`"
      >跳到浏览区</a
    >
    <header class="app-header">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true">⌘</span>
        <div>
          <p class="eyebrow">INVESTMENT TRACKER</p>
          <h1>项目架构</h1>
        </div>
      </div>
      <div class="header-context">
        <span class="snapshot-badge">工作区快照</span>
        <span v-if="snapshot" class="scan-time">扫描于 {{ scannedAt }}</span>
      </div>
      <button class="button primary" :disabled="loading" @click="loadIndex(true)">
        {{ loading ? '正在扫描…' : '重新扫描' }}
      </button>
    </header>

    <div v-if="loadError" class="message error-message" role="alert">
      <div>
        <strong>{{ snapshot ? '重新扫描失败' : '无法加载索引' }}</strong> · {{ loadError }}
      </div>
      <button class="text-button" :disabled="loading" @click="loadIndex(!!snapshot)">重试</button>
    </div>
    <div v-if="loading" class="message loading-message" role="status">
      <span class="loading-dot" aria-hidden="true"></span>
      {{ snapshot ? '正在重新扫描，当前展示上次快照。' : '正在读取项目文件并建立静态索引…' }}
    </div>

    <template v-if="snapshot">
      <section class="overview" aria-label="索引概况">
        <p>
          <strong>{{ files.length.toLocaleString() }}</strong> 个文件
        </p>
        <span class="overview-divider"></span>
        <p>
          <strong>{{ snapshot.symbols.length.toLocaleString() }}</strong> 个符号
        </p>
        <span class="overview-divider"></span>
        <p>
          <strong>{{ totalResolved.toLocaleString() }}</strong> 条项目内依赖
        </p>
        <details v-if="scopedDiagnostics.length" class="index-diagnostics">
          <summary>
            当前范围 · 未解析 / 分析提示
            <span>{{ scopedDiagnostics.length.toLocaleString() }}</span>
          </summary>
          <div class="diagnostics-popover">
            <p class="muted">这些项目尚未完整解析；空目标不会被当作已确认的文件依赖。</p>
            <ol>
              <li
                v-for="(error, index) in scopedDiagnostics.slice(0, diagnosticsLimit)"
                :key="index"
              >
                <button
                  v-if="error.path && filePaths.has(error.path)"
                  class="text-button"
                  @click="openFile(error.path)"
                >
                  {{ error.path }}
                </button>
                <strong v-else>{{ error.path || '扫描器' }}</strong>
                <p>{{ error.message }}</p>
              </li>
            </ol>
            <button
              v-if="scopedDiagnostics.length > diagnosticsLimit"
              class="button"
              @click="diagnosticsLimit += 100"
            >
              继续显示提示（已显示 {{ diagnosticsLimit }} 条）
            </button>
          </div>
        </details>
      </section>

      <nav class="workspace-views" aria-label="工作区视图">
        <button
          :class="{ active: view === 'structure' }"
          :aria-pressed="view === 'structure'"
          @click="view = 'structure'"
        >
          结构浏览
        </button>
        <button
          :class="{ active: view === 'cycles' }"
          :aria-pressed="view === 'cycles'"
          @click="view = 'cycles'"
        >
          循环提示
        </button>
        <button
          :class="{ active: view === 'quality' }"
          :aria-pressed="view === 'quality'"
          @click="view = 'quality'"
        >
          质量与检查
        </button>
      </nav>
      <CyclesPane
        v-show="view === 'cycles'"
        :cycles="snapshot.cycles"
        :truncated="snapshot.cycles_truncated ?? false"
        :files="files"
        @open-source="openVerifiedSource"
      />
      <QualityPane
        v-show="view === 'quality'"
        :files="files"
        :snapshot-id="snapshot.scanned_at"
        @open-source="openVerifiedSource"
        @quality-change="qualitySelection = $event"
      />

      <nav v-show="view === 'structure'" class="panel-tabs" aria-label="浏览面板">
        <button
          :class="{ active: panel === 'directory' }"
          :aria-pressed="panel === 'directory'"
          @click="panel = 'directory'"
        >
          目录与搜索
        </button>
        <button
          :class="{ active: panel === 'graph' }"
          :aria-pressed="panel === 'graph'"
          @click="panel = 'graph'"
        >
          模块关系
        </button>
        <button
          :class="{ active: panel === 'source' }"
          :aria-pressed="panel === 'source'"
          @click="panel = 'source'"
        >
          源码详情
        </button>
      </nav>

      <main
        v-show="view === 'structure'"
        id="workspace"
        class="workspace"
        :class="[`show-${panel}`, { 'uml-layout': graphView === 'uml' }]"
        tabindex="-1"
      >
        <aside class="directory-pane pane" aria-label="项目目录与搜索">
          <div class="pane-heading">
            <h2>项目目录</h2>
            <span class="small-label">只读</span>
          </div>
          <div class="search-field">
            <label class="visually-hidden" for="source-search">搜索文件或符号</label>
            <input
              id="source-search"
              v-model="query"
              type="search"
              placeholder="搜索文件或符号…"
              autocomplete="off"
            />
          </div>
          <div v-if="searchText" class="search-results">
            <p class="result-heading">文件 · {{ matchedFiles.length }}</p>
            <button
              v-for="file in matchedFiles.slice(0, 50)"
              :key="file.path"
              class="search-result"
              @click="openFile(file.path)"
            >
              <strong>{{ nameOf(file.path) }}</strong
              ><small>{{ file.path }}</small>
            </button>
            <p v-if="matchedFiles.length > 50" class="muted small-copy">
              显示前 50 项，请缩小搜索范围。
            </p>
            <p class="result-heading">符号 · {{ matchedSymbols.length }}</p>
            <button
              v-for="symbol in matchedSymbols.slice(0, 50)"
              :key="symbol.id"
              class="search-result"
              @click="openFile(symbol.path, symbol.line)"
            >
              <strong>{{ symbol.name }}</strong
              ><small>{{ symbol.path }}:{{ symbol.line }}</small>
            </button>
            <p v-if="matchedSymbols.length > 50" class="muted small-copy">
              显示前 50 项，请缩小搜索范围。
            </p>
            <p v-if="!matchedFiles.length && !matchedSymbols.length" class="empty-copy">
              没有匹配的文件或符号。
            </p>
          </div>
          <div v-else class="directory-list">
            <button
              v-if="directory"
              class="parent-directory"
              @click="openDirectory(parentOf(directory))"
            >
              ← 上一级目录
            </button>
            <p class="directory-location" :title="directory || '项目根目录'">
              {{ directory || '项目根目录' }}
            </p>
            <button
              v-for="entry in children"
              :key="entry.path"
              class="directory-entry"
              :class="{ selected: selectedFile === entry.path }"
              :title="entry.path"
              @click="entry.directory ? openDirectory(entry.path) : openFile(entry.path)"
            >
              <span class="entry-icon" :class="{ folder: entry.directory }" aria-hidden="true">{{
                entry.directory ? '▰' : '·'
              }}</span>
              <span class="entry-name">{{ nameOf(entry.path) }}</span>
              <small v-if="entry.directory">{{ entry.count }}</small>
            </button>
            <p v-if="!children.length" class="empty-copy">这个目录没有可浏览的文件。</p>
          </div>
          <p class="directory-footnote">目录中的数字表示允许浏览的文件数。</p>
        </aside>

        <section class="graph-pane pane" aria-label="模块与文件依赖">
          <nav class="breadcrumbs" aria-label="当前目录">
            <button @click="openDirectory('')">项目总览</button>
            <template v-for="crumb in breadcrumbs" :key="crumb.path"
              ><span aria-hidden="true">/</span
              ><button @click="openDirectory(crumb.path)">{{ crumb.name }}</button></template
            >
          </nav>
          <div class="graph-heading">
            <div>
              <p class="eyebrow">{{ selectedFile ? '文件关系' : '模块下钻' }}</p>
              <h2 :title="selectedFile || directory">
                {{ selectedFile ? nameOf(selectedFile) : nameOf(directory) || '从项目结构开始' }}
              </h2>
            </div>
            <button v-if="selectedFile" class="text-button" @click="returnToDirectory">
              返回目录
            </button>
          </div>
          <div class="graph-toolbar graph-mode" role="group" aria-label="图形视图">
            <button
              class="button"
              :class="{ primary: graphView === 'relations' }"
              :aria-pressed="graphView === 'relations'"
              @click="graphView = 'relations'"
            >
              关系图
            </button>
            <button
              class="button"
              :class="{ primary: graphView === 'uml' }"
              :aria-pressed="graphView === 'uml'"
              @click="graphView = 'uml'"
            >
              UML 图
            </button>
            <template v-if="graphView === 'uml'">
              <button
                v-if="files.some((file) => file.path.startsWith('backend/app/'))"
                class="text-button"
                @click="openDirectory('backend/app')"
              >
                后端模块
              </button>
              <button
                v-if="files.some((file) => file.path.startsWith('frontend/src/'))"
                class="text-button"
                @click="openDirectory('frontend/src')"
              >
                前端模块
              </button>
            </template>
          </div>
          <div class="graph-toolbar">
            <label for="dependency-kind">依赖类型</label>
            <select id="dependency-kind" v-model="dependencyKind" @change="focusedEdges = null">
              <option value="all">全部</option>
              <option value="runtime">运行时</option>
              <option value="type">仅类型</option>
              <option value="dynamic">延迟 / 动态</option>
            </select>
            <span v-if="!riskOverlay && graphView === 'relations'" class="graph-legend"
              ><i class="legend-directory"></i>目录 <i class="legend-file"></i>文件</span
            >
          </div>
          <div class="graph-quality-toolbar">
            <label
              ><input
                v-model="riskColors"
                type="checkbox"
                :disabled="!!qualityUnavailable"
              />函数风险着色</label
            >
            <button class="text-button" @click="view = 'quality'">选择质量报告</button>
            <p v-if="qualityUnavailable">{{ qualityUnavailable }}</p>
            <p v-else>
              报告 {{ qualitySelection.report?.id }} · 提示阈值 CRAP ≥
              {{ qualitySelection.threshold }}
            </p>
            <div v-if="riskOverlay" class="risk-legend" aria-label="函数风险图例">
              <span><i class="risk-high"></i>达到阈值</span
              ><span><i class="risk-low"></i>已评估且低于阈值</span
              ><span><i class="risk-unknown"></i>含未知 / 无数据</span>
            </div>
          </div>
          <UmlGraph
            v-if="graphView === 'uml'"
            :nodes="graph.nodes"
            :links="graph.links"
            :selected="selectedFile"
            :colors="
              riskOverlay
                ? Object.fromEntries(graph.nodes.map((node) => [node.path, riskColor(node.path)]))
                : undefined
            "
            @open-node="(node) => (node.directory ? openDirectory(node.path) : openFile(node.path))"
          />
          <div
            v-else
            class="graph-canvas"
            role="img"
            :aria-label="`${selectedFile || directory || '项目'}依赖图，可通过目录和下方列表访问相同内容`"
          >
            <VChart
              v-if="graph.nodes.length"
              :option="graphOption"
              autoresize
              @click="handleGraphClick"
            />
            <div v-else class="graph-empty">
              <strong>还没有可展示的节点</strong>
              <p>当前快照没有允许浏览的源码文件。</p>
            </div>
          </div>
          <p class="graph-hint">
            <template v-if="graph.total > 45"
              >图中显示 {{ graph.nodes.length }} /
              {{ graph.total }} 个节点，请使用左侧目录或搜索继续浏览。</template
            >
            <template v-else-if="!graph.links.length"
              >当前范围没有已解析的跨节点依赖。点击节点浏览文件或子目录。</template
            >
            <template v-else-if="graphView === 'relations'"
              >点击节点下钻，点击连线查看导入证据；可拖动节点或缩放。</template
            >
          </p>
          <details v-if="riskOverlay" class="module-quality" open>
            <summary>节点函数评估 · {{ graph.nodes.length }} 项</summary>
            <p>
              颜色取模块内已知函数的最高 CRAP。低分但含未知的模块显示灰色；灰色不代表低风险。
              {{ graphView === 'uml' ? '节点注解区分目录与文件。' : '形状仍区分目录与文件。' }}
            </p>
            <button
              v-for="node in graph.nodes"
              :key="node.path"
              @click="node.directory ? openDirectory(node.path) : openFile(node.path)"
            >
              <strong
                ><i :style="{ background: riskColor(node.path) }"></i
                >{{ nameOf(node.path) }}</strong
              ><span>{{ qualityDescription(node.path) }}</span>
            </button>
          </details>
          <details v-if="!selectedFile && graph.links.length" class="module-relations">
            <summary>模块连线列表 · {{ graph.links.length }} 组</summary>
            <button
              v-for="[key, link] in graph.links.slice(0, moduleLinkLimit)"
              :key="key"
              @click="selectLink(key)"
            >
              {{ nameOf(link.source) }} → {{ nameOf(link.target) }}
              <small>{{ link.evidence.length }} 条导入</small>
            </button>
            <button v-if="graph.links.length > moduleLinkLimit" @click="moduleLinkLimit += 30">
              显示更多连线（已显示 {{ moduleLinkLimit }} 组）
            </button>
          </details>

          <section v-if="selectedFile || focusedEdges" class="relations" aria-label="依赖证据列表">
            <div class="relation-tabs">
              <template v-if="selectedFile"
                ><button
                  :class="{ active: direction === 'outgoing' && !focusedEdges }"
                  @click="setDirection('outgoing')"
                >
                  依赖谁 <span>{{ outgoing.length }}</span></button
                ><button
                  :class="{ active: direction === 'incoming' && !focusedEdges }"
                  @click="setDirection('incoming')"
                >
                  谁依赖它 <span>{{ incoming.length }}</span>
                </button></template
              >
              <strong v-if="focusedEdges" class="focused-label"
                >已选连线 · {{ focusedEdges.length }} 条导入</strong
              >
              <button v-if="focusedEdges" class="text-button" @click="focusedEdges = null">
                清除选择
              </button>
            </div>
            <ul v-if="visibleEdges.length" class="dependency-list">
              <li
                v-for="(edge, index) in visibleEdges.slice(0, evidenceLimit)"
                :key="`${edge.source}:${edge.target}:${edge.line}:${index}`"
              >
                <div class="dependency-description">
                  <button
                    v-if="direction === 'incoming' && !focusedEdges"
                    class="dependency-path"
                    @click="openFile(edge.source)"
                  >
                    {{ edge.source }}
                  </button>
                  <button
                    v-else-if="edge.target"
                    class="dependency-path"
                    @click="openFile(edge.target)"
                  >
                    {{ edge.target }}
                  </button>
                  <strong v-else class="dependency-path unresolved-path">{{
                    edge.specifier
                  }}</strong>
                  <span class="dependency-kind">{{ kindLabel(edge.kind) }}</span>
                  <span v-if="edge.external" class="dependency-state">外部库</span
                  ><span v-else-if="!edge.target" class="dependency-state unknown">未解析</span>
                </div>
                <button
                  class="evidence-link"
                  :title="edge.statement || edge.specifier"
                  @click="showEvidence(edge)"
                >
                  {{ focusedEdges ? edge.source + ' · ' : ''
                  }}{{ edge.line ? '查看导入 · L' + edge.line : '打开来源 · 行号未知' }} →
                </button>
              </li>
            </ul>
            <p v-else class="empty-copy">
              {{
                direction === 'outgoing'
                  ? '当前类型下没有记录到此文件的导入。'
                  : '当前类型下没有记录到引用此文件的导入。'
              }}
            </p>
            <button
              v-if="visibleEdges.length > evidenceLimit"
              class="button load-more"
              @click="evidenceLimit += 100"
            >
              显示更多导入（已显示 {{ evidenceLimit }} / {{ visibleEdges.length }} 条）
            </button>
          </section>
          <div v-else class="explore-note">
            <strong>沿着结构查看代码</strong>
            <p>选择左侧目录或图中节点，逐层查看模块，再定位到具体文件和导入行。</p>
          </div>
        </section>

        <aside class="source-pane pane" aria-label="源码详情">
          <SourcePane
            :key="`${snapshot.scanned_at}:${sourceFile}`"
            :file-path="sourceFile"
            :file-hash="sourceHash"
            :symbols="currentSymbols"
            :line="sourceLine"
          />
        </aside>
      </main>
    </template>
    <main v-else-if="!loading" class="initial-empty">
      <h2>索引尚未就绪</h2>
      <p>连接架构服务后重试，即可浏览允许范围内的项目源码。</p>
    </main>
  </div>
</template>

<style scoped>
.graph-mode {
  flex-wrap: wrap;
  padding-bottom: 12px;
}
.graph-mode .button {
  padding: 7px 10px;
}
</style>
