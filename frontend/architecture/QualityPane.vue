<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import MutationPane from './MutationPane.vue'
import type { SourceFile } from './types'
import type {
  ChangeBucket,
  ComparisonResponse,
  QualityFunction,
  QualityGroup,
  QualitySelection,
  QualitySourceLocation,
  ReportDetail,
  ReportList
} from './quality-types'

const props = defineProps<{ files: SourceFile[]; snapshotId: string }>()
const emit = defineEmits<{
  openSource: [location: QualitySourceLocation]
  qualityChange: [selection: QualitySelection]
}>()
const groups: QualityGroup[] = ['backend', 'frontend']
const groupNames = { backend: 'Python 后端', frontend: '前端' }
const statusNames: Record<string, string> = {
  current: '有效报告',
  stale: '来源已变化',
  partial: '部分结果',
  unknown: '未知',
  measured: '已测量',
  not_applicable: '不适用',
  passed: '通过',
  failed: '失败',
  error: '错误',
  skipped: '跳过',
  pending: '未运行',
  running: '运行中',
  incomplete: '未完成'
}
const guardNames: Record<string, string> = {
  portfolio_purity: '计算内核纯度',
  private_imports: '跨模块私有导入',
  no_implicit_clock: '隐式时钟',
  deploy_config_sync: '部署配置一致性',
  env_contract: '环境配置约定'
}
const checkNames: Record<string, string> = {
  'optional-xueqiu-absent': '雪球可选依赖缺席',
  'backend-lint': '后端 Ruff',
  'backend-format': '后端格式',
  'backend-tests': '后端测试',
  'architecture-lint': '架构和 CI 工具 Ruff',
  'architecture-format': '架构和 CI 工具格式',
  'architecture-tests': '架构和 CI 工具测试',
  'coverage-json': 'Python 覆盖率 JSON',
  'coverage-xml': 'Python 覆盖率 XML',
  'frontend-api-generate': '生成 API 类型',
  'frontend-api-drift': 'API 类型同步',
  'frontend-format': '前端格式',
  'frontend-typecheck': '前端应用与工具类型检查',
  'frontend-tests': '前端单元测试',
  'frontend-build': '前端构建',
  'architecture-frontend-format': '架构界面格式',
  'architecture-frontend-typecheck': '架构界面类型检查',
  'architecture-build': '架构界面构建',
  'architecture-node-tests': '前端静态分析器测试',
  'frontend-e2e': 'Playwright E2E'
}
const changeNames: Record<ChangeBucket, string> = {
  worsened: '恶化',
  improved: '改善',
  added: '新增',
  removed: '移除',
  unknown: '未知',
  unchanged: '不变'
}
const changeBuckets: ChangeBucket[] = [
  'worsened',
  'improved',
  'added',
  'removed',
  'unknown',
  'unchanged'
]
const reports = ref<ReportList>({ reports: [], errors: [], truncated: false })
const selectedId = ref('')
const beforeId = ref('')
const detail = ref<ReportDetail | null>(null)
const compared = ref<ComparisonResponse | null>(null)
const listLoading = ref(false)
const reportLoading = ref(false)
const comparing = ref(false)
const listError = ref('')
const reportError = ref('')
const compareError = ref('')
const tab = ref<'functions' | 'checks' | 'compare' | 'mutation'>('functions')
const groupFilter = ref<'all' | QualityGroup>('all')
const search = ref('')
const filter = ref<'all' | 'risk' | 'unknown'>('all')
const threshold = ref<number | string>(30)
const page = ref(0)
const guardPage = ref(0)
const changePage = ref(0)
const changeBucket = ref<ChangeBucket>('worsened')
const guardChangePage = ref(0)
const guardChangeBucket = ref<ChangeBucket>('worsened')
const pageSize = 50
let listController: AbortController | null = null
let reportController: AbortController | null = null
let compareController: AbortController | null = null

const quality = computed(() => detail.value?.quality ?? null)
const displayedSource = computed(() =>
  tab.value === 'mutation' && detail.value?.mutation
    ? detail.value.mutation.source
    : detail.value?.source
)
const currentHashes = computed(() => new Map(props.files.map((file) => [file.path, file.sha256])))
const riskThreshold = computed(() => {
  const value = Number(threshold.value)
  return Number.isFinite(value) && value >= 0 ? value : 30
})
watch(
  [detail, riskThreshold],
  ([report, threshold]) => {
    emit('qualityChange', { report, threshold })
  },
  { immediate: true }
)
const scopedFunctions = computed(() =>
  (quality.value?.functions ?? []).filter(
    (row) =>
      (groupFilter.value === 'all' || row.group === groupFilter.value) &&
      `${row.name} ${row.path}`
        .toLocaleLowerCase()
        .includes(search.value.trim().toLocaleLowerCase())
  )
)
function measured(row: QualityFunction) {
  return (
    quality.value?.groups[row.group]?.status === 'current' &&
    row.status === 'measured' &&
    typeof row.crap === 'number' &&
    Number.isFinite(row.crap) &&
    typeof row.coverage === 'number' &&
    Number.isFinite(row.coverage)
  )
}
const assessed = computed(() => scopedFunctions.value.filter(measured))
const notApplicable = computed(
  () => scopedFunctions.value.filter((row) => row.status === 'not_applicable').length
)
const applicableCount = computed(() => scopedFunctions.value.length - notApplicable.value)
const unknownCount = computed(() => applicableCount.value - assessed.value.length)
const riskCount = computed(
  () => assessed.value.filter((row) => row.crap! >= riskThreshold.value).length
)
const effectiveRatio = computed(() =>
  applicableCount.value
    ? `${((assessed.value.length / applicableCount.value) * 100).toFixed(1)}%`
    : '—'
)
const filteredFunctions = computed(() =>
  scopedFunctions.value
    .filter(
      (row) =>
        filter.value === 'all' ||
        (filter.value === 'risk'
          ? measured(row) && row.crap! >= riskThreshold.value
          : !measured(row) && row.status !== 'not_applicable')
    )
    .slice()
    .sort(
      (a, b) =>
        Number(measured(b)) - Number(measured(a)) ||
        (measured(a) && measured(b) ? b.crap! - a.crap! : 0) ||
        a.path.localeCompare(b.path) ||
        a.line - b.line
    )
)
const visibleFunctions = computed(() =>
  filteredFunctions.value.slice(page.value * pageSize, (page.value + 1) * pageSize)
)
const guards = computed(() => detail.value?.guards ?? [])
const visibleGuards = computed(() =>
  guards.value.slice(guardPage.value * pageSize, (guardPage.value + 1) * pageSize)
)
const changes = computed(() => compared.value?.comparison[changeBucket.value] ?? [])
const visibleChanges = computed(() =>
  changes.value.slice(changePage.value * pageSize, (changePage.value + 1) * pageSize)
)
const guardChanges = computed(
  () => compared.value?.guard_comparison?.[guardChangeBucket.value] ?? []
)
const visibleGuardChanges = computed(() =>
  guardChanges.value.slice(guardChangePage.value * pageSize, (guardChangePage.value + 1) * pageSize)
)
const diagnostics = computed(() => [
  ...reports.value.errors,
  ...(detail.value?.errors ?? []),
  ...(quality.value?.errors ?? [])
])

function label(status: string | null | undefined) {
  return status ? (statusNames[status] ?? status) : '未提供'
}
function number(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : '—'
}
function percent(value: number | null) {
  return typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : '—'
}
function date(value: string | null | undefined) {
  if (!value) return '时间未知'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '时间未知' : parsed.toLocaleString('zh-CN')
}
function reportLabel(id: string) {
  const item = reports.value.reports.find((row) => row.id === id)
  return `${id} · ${date(item?.finished_at)}`
}
function navigationReason(row: QualityFunction) {
  if (!detail.value?.source.can_navigate) return '报告与当前源码尚未确认匹配，无法跳转'
  const hash = quality.value?.source.files[row.path]
  if (!hash || currentHashes.value.get(row.path) !== hash)
    return '此文件与当前索引不同，请重新扫描后核对'
  if (!Number.isInteger(row.line) || row.line < 1) return '没有有效的源码行号'
  return ''
}
function openSource(row: QualityFunction) {
  if (navigationReason(row)) return
  emit('openSource', {
    path: row.path,
    line: row.line,
    sha256: quality.value!.source.files[row.path]
  })
}
async function readJson<T>(url: string, controller: AbortController): Promise<T> {
  const response = await fetch(url, { signal: controller.signal })
  const body = await response.json()
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '报告读取失败')
  return body as T
}
function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : '无法读取报告，请稍后重试'
}

async function loadReports() {
  listController?.abort()
  const request = new AbortController()
  listController = request
  listLoading.value = true
  listError.value = ''
  try {
    const body = await readJson<ReportList>('/api/reports', request)
    if (listController !== request || request.signal.aborted) return
    if (!Array.isArray(body.reports) || !Array.isArray(body.errors))
      throw new Error('报告列表格式不完整')
    reports.value = body
    if (!body.reports.some((row) => row.id === selectedId.value))
      selectedId.value = body.reports[0]?.id ?? ''
    else if (selectedId.value) void loadReport(selectedId.value)
    if (!body.reports.some((row) => row.id === beforeId.value))
      beforeId.value = body.reports.find((row) => row.id !== selectedId.value)?.id ?? ''
  } catch (error) {
    if (listController === request && !request.signal.aborted) listError.value = errorMessage(error)
  } finally {
    if (listController === request && !request.signal.aborted) listLoading.value = false
  }
}
async function loadReport(id: string) {
  clearComparison()
  reportController?.abort()
  const request = new AbortController()
  reportController = request
  detail.value = null
  reportError.value = ''
  reportLoading.value = !!id
  page.value = guardPage.value = 0
  if (!id) return
  try {
    const body = await readJson<ReportDetail>(`/api/report?${new URLSearchParams({ id })}`, request)
    if (reportController !== request || request.signal.aborted) return
    if (
      body.id !== id ||
      !body.source ||
      !body.ci ||
      !Array.isArray(body.guards) ||
      !Array.isArray(body.errors) ||
      (body.quality &&
        (!Array.isArray(body.quality.functions) ||
          !Array.isArray(body.quality.errors) ||
          !body.quality.source?.files ||
          !groups.every((group) => Array.isArray(body.quality?.groups?.[group]?.reasons)) ||
          body.quality.functions.some(
            (row) =>
              !row ||
              !groups.includes(row.group) ||
              typeof row.path !== 'string' ||
              typeof row.name !== 'string'
          )))
    )
      throw new Error('报告内容与选择不一致或格式不完整')
    detail.value = body
    if (tab.value === 'functions' && !body.quality && body.mutation) tab.value = 'mutation'
  } catch (error) {
    if (reportController === request && !request.signal.aborted)
      reportError.value = errorMessage(error)
  } finally {
    if (reportController === request && !request.signal.aborted) reportLoading.value = false
  }
}
function clearComparison() {
  compareController?.abort()
  compareController = null
  comparing.value = false
  compared.value = null
  compareError.value = ''
  changePage.value = 0
  guardChangePage.value = 0
}
async function runComparison() {
  clearComparison()
  if (!beforeId.value || !selectedId.value || beforeId.value === selectedId.value) return
  const before = beforeId.value
  const after = selectedId.value
  const request = new AbortController()
  compareController = request
  comparing.value = true
  try {
    const body = await readJson<ComparisonResponse>(
      `/api/compare?${new URLSearchParams({ before, after })}`,
      request
    )
    if (compareController !== request || request.signal.aborted) return
    if (
      body.before?.id !== before ||
      body.after?.id !== after ||
      !changeBuckets.every((key) => Array.isArray(body.comparison?.[key]))
    )
      throw new Error('基线比较格式不完整')
    if (
      body.guard_comparison &&
      (!Array.isArray(body.guard_comparison.reasons) ||
        !changeBuckets.every((key) => Array.isArray(body.guard_comparison?.[key])))
    )
      throw new Error('守卫比较格式不完整')
    compared.value = body
    changeBucket.value = changeBuckets.find((key) => body.comparison[key].length > 0) ?? 'worsened'
    guardChangeBucket.value =
      changeBuckets.find((key) => (body.guard_comparison?.[key].length ?? 0) > 0) ?? 'worsened'
  } catch (error) {
    if (compareController === request && !request.signal.aborted)
      compareError.value = errorMessage(error)
  } finally {
    if (compareController === request && !request.signal.aborted) comparing.value = false
  }
}
watch([selectedId, () => props.snapshotId], () => {
  clearComparison()
  void loadReport(selectedId.value)
})
watch(beforeId, clearComparison)
watch([groupFilter, search, filter, threshold], () => {
  page.value = 0
})
watch(changeBucket, () => {
  changePage.value = 0
})
watch(guardChangeBucket, () => {
  guardChangePage.value = 0
})
onMounted(loadReports)
onBeforeUnmount(() => {
  listController?.abort()
  reportController?.abort()
  compareController?.abort()
})
</script>

<template>
  <main id="quality-workspace" class="quality-pane" tabindex="-1" aria-label="质量与检查报告">
    <header class="quality-heading">
      <div>
        <p class="eyebrow">QUALITY &amp; EVIDENCE</p>
        <h2>质量与检查</h2>
        <p class="quality-subtitle">查看已采集的报告，追踪函数风险与检查结果。</p>
      </div>
      <button class="button" :disabled="listLoading" @click="loadReports">
        {{ listLoading ? '读取中…' : '刷新报告' }}
      </button>
    </header>
    <div v-if="listError" class="message error-message" role="alert">{{ listError }}</div>
    <div class="report-picker" v-if="reports.reports.length">
      <label for="quality-report">查看报告</label>
      <select id="quality-report" v-model="selectedId">
        <option v-for="item in reports.reports" :key="item.id" :value="item.id">
          {{ reportLabel(item.id) }}
        </option>
      </select>
      <span
        v-if="displayedSource"
        class="quality-badge"
        :class="displayedSource.can_navigate ? 'positive' : 'warning'"
        >{{
          displayedSource.can_navigate
            ? '与当前源码匹配'
            : displayedSource.status === 'stale'
              ? '历史源码 · 定位已禁用'
              : '源码匹配未知 · 定位已禁用'
        }}</span
      >
    </div>
    <p v-if="reports.truncated" class="quality-note">报告数量较多，当前列表未展示全部记录。</p>
    <div v-if="listLoading && !reports.reports.length" class="quality-empty" role="status">
      正在读取本地报告列表…
    </div>
    <div v-else-if="!reports.reports.length && !listError" class="quality-empty">
      <h3>尚无报告</h3>
      <p>完成一次检查与质量采集后，在本地架构服务指定报告目录，再刷新此处。</p>
    </div>
    <details v-if="diagnostics.length" class="quality-diagnostics">
      <summary>报告提示 · {{ diagnostics.length }}</summary>
      <p v-for="(item, index) in diagnostics.slice(0, 10)" :key="index">
        {{ item.path ? `${item.path}：` : '' }}{{ item.message }}
      </p>
      <p v-if="diagnostics.length > 10">仅展示前 10 条提示。</p>
    </details>
    <div v-if="reportLoading" class="quality-empty" role="status">正在读取所选报告…</div>
    <div v-else-if="reportError" class="message error-message" role="alert">
      <span>{{ reportError }}</span
      ><button class="text-button" @click="loadReport(selectedId)">重试</button>
    </div>
    <template v-else-if="detail">
      <nav class="quality-tabs" aria-label="报告内容">
        <button
          :class="{ active: tab === 'functions' }"
          :aria-pressed="tab === 'functions'"
          @click="tab = 'functions'"
        >
          函数风险</button
        ><button
          :class="{ active: tab === 'checks' }"
          :aria-pressed="tab === 'checks'"
          @click="tab = 'checks'"
        >
          CI 与既有守卫</button
        ><button
          :class="{ active: tab === 'compare' }"
          :aria-pressed="tab === 'compare'"
          @click="tab = 'compare'"
        >
          基线比较
        </button>
        <button
          :class="{ active: tab === 'mutation' }"
          :aria-pressed="tab === 'mutation'"
          @click="tab = 'mutation'"
        >
          变异试点
        </button>
      </nav>
      <section v-if="tab === 'functions'" aria-label="函数风险报告">
        <div v-if="!quality" class="quality-empty">
          <h3>这次运行没有质量报告</h3>
          <p>CI 检查结果仍可在“CI 与既有守卫”中查看；缺少覆盖率不表示零风险。</p>
        </div>
        <template v-else>
          <div class="quality-section-heading">
            <h3>函数 CRAP</h3>
            <span>{{ label(quality.status) }} · {{ date(quality.generated_at) }}</span>
          </div>
          <p class="quality-note">
            分数结合圈复杂度（CC）与函数行覆盖率，越高越值得查看。提示阈值仅用于筛选，未知结果不记为
            0。
          </p>
          <div class="risk-filters">
            <label
              >范围<select v-model="groupFilter">
                <option value="all">全部语言</option>
                <option value="backend">Python 后端</option>
                <option value="frontend">前端</option>
              </select></label
            ><label class="risk-search"
              >函数或文件<input
                v-model="search"
                type="search"
                placeholder="搜索限定名称或路径" /></label
            ><label
              >提示阈值<input
                v-model="threshold"
                type="number"
                min="0"
                step="1"
                aria-label="CRAP 提示阈值" /></label
            ><label
              >显示<select v-model="filter">
                <option value="all">全部函数</option>
                <option value="risk">达到提示阈值</option>
                <option value="unknown">未知结果</option>
              </select></label
            >
          </div>
          <div class="quality-stats" aria-label="当前筛选范围的评估概况">
            <div>
              <span>有效评估比例</span><strong>{{ effectiveRatio }}</strong
              ><small>{{ assessed.length }} / {{ applicableCount }} 个可评估函数</small>
            </div>
            <div>
              <span>达到阈值</span><strong>{{ riskCount }}</strong
              ><small>CRAP ≥ {{ riskThreshold }}</small>
            </div>
            <div>
              <span>未知</span><strong>{{ unknownCount }}</strong
              ><small>另有 {{ notApplicable }} 个不适用函数</small>
            </div>
          </div>
          <details
            v-for="group in groups.filter((item) => quality?.groups[item]?.reasons.length)"
            :key="group"
            class="quality-diagnostics"
          >
            <summary>{{ groupNames[group] }} · {{ label(quality.groups[group].status) }}</summary>
            <p v-for="reason in quality.groups[group].reasons" :key="reason">{{ reason }}</p>
          </details>
          <div v-if="visibleFunctions.length" class="quality-table-scroll">
            <table class="quality-table risk-table">
              <caption class="visually-hidden">
                按 CRAP 从高到低排列的函数风险
              </caption>
              <thead>
                <tr>
                  <th>函数 / 来源</th>
                  <th>CRAP</th>
                  <th>CC</th>
                  <th>行覆盖率</th>
                  <th>评估状态</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(row, index) in visibleFunctions"
                  :key="`${row.path}:${row.line}:${index}`"
                >
                  <td>
                    <button
                      class="function-link"
                      :disabled="!!navigationReason(row)"
                      :title="navigationReason(row) || '查看当前源码'"
                      @click="openSource(row)"
                    >
                      {{ row.name
                      }}<span v-if="!navigationReason(row)" aria-hidden="true"> ↗</span></button
                    ><small class="source-caption">{{ row.path }}:{{ row.line }}</small
                    ><small v-if="row.reason" class="row-reason">{{ row.reason }}</small>
                  </td>
                  <td
                    data-label="CRAP"
                    class="score-cell"
                    :class="{ 'risk-score': measured(row) && row.crap! >= riskThreshold }"
                  >
                    {{ measured(row) ? number(row.crap) : '—' }}
                  </td>
                  <td data-label="CC">{{ number(row.cc) }}</td>
                  <td data-label="行覆盖率">{{ measured(row) ? percent(row.coverage) : '—' }}</td>
                  <td data-label="评估状态">
                    <span class="quality-badge" :class="measured(row) ? 'positive' : 'neutral'">{{
                      measured(row) ? '已测量' : label(row.status)
                    }}</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="quality-empty">
            {{ quality.functions.length ? '当前筛选条件下没有函数。' : '报告中没有可展示的函数。' }}
          </p>
          <div v-if="filteredFunctions.length" class="quality-pagination">
            <span
              >{{ page * pageSize + 1 }}–{{
                Math.min((page + 1) * pageSize, filteredFunctions.length)
              }}
              / {{ filteredFunctions.length }} 个函数</span
            ><button class="button" :disabled="page === 0" @click="page--">上一页</button
            ><button
              class="button"
              :disabled="(page + 1) * pageSize >= filteredFunctions.length"
              @click="page++"
            >
              下一页
            </button>
          </div>
        </template>
      </section>
      <section v-else-if="tab === 'checks'" aria-label="CI 与既有守卫结果">
        <p class="quality-note">检查状态直接来自本次原生报告。跳过、缺失和未完成均保留原状态。</p>
        <div class="check-groups">
          <details v-for="group in groups" :key="group" class="check-group" open>
            <summary>
              <strong>{{ groupNames[group] }}</strong
              ><span
                class="quality-badge"
                :class="detail.ci[group]?.status === 'passed' ? 'positive' : 'warning'"
                >{{ label(detail.ci[group]?.status) }}</span
              >
            </summary>
            <template v-if="detail.ci[group]"
              ><p class="quality-note">
                运行 {{ detail.ci[group]!.run_id }} · 第 {{ detail.ci[group]!.run_attempt }} 次尝试
                · {{ detail.ci[group]!.source.dirty ? '包含工作区变更' : '干净源码' }}
              </p>
              <p v-if="!detail.ci[group]!.finished_at" class="quality-note warning-copy">
                没有结束记录，不能认定检查已完成。
              </p>
              <ul class="check-steps">
                <li v-for="step in detail.ci[group]!.steps" :key="step.id">
                  <span>{{ checkNames[step.id] ?? step.id }}</span
                  ><span :class="{ 'warning-copy': step.status !== 'passed' }">{{
                    label(step.status)
                  }}</span
                  ><small
                    >{{ number(step.duration_seconds) }} 秒 · 退出码
                    {{ step.exit_code ?? '—' }}</small
                  >
                </li>
              </ul></template
            >
            <p v-else class="quality-empty">本次运行未提供这个分组的检查报告。</p>
          </details>
        </div>
        <div class="quality-section-heading">
          <h3>既有架构守卫</h3>
          <span>{{ guards.length }} 条原生用例结果</span>
        </div>
        <p class="quality-note">展示现有守卫测试的结果；缺少用例报告时保留未知。</p>
        <ul v-if="visibleGuards.length" class="guard-list">
          <li v-for="(guard, index) in visibleGuards" :key="index">
            <div>
              <strong>{{ guardNames[guard.rule] ?? guard.rule }}</strong
              ><span
                class="quality-badge"
                :class="guard.status === 'passed' ? 'positive' : 'warning'"
                >{{ label(guard.status) }}</span
              >
            </div>
            <p v-if="guard.test" class="source-caption">{{ guard.test }}</p>
            <p v-if="guard.path" class="source-caption">
              {{ guard.path }}{{ guard.line ? `:${guard.line}` : '' }}
            </p>
            <p v-if="guard.message" class="row-reason">{{ guard.message }}</p>
          </li>
        </ul>
        <p v-else class="quality-empty">没有可展示的守卫用例报告。</p>
        <div v-if="guards.length > pageSize" class="quality-pagination">
          <span
            >{{ guardPage * pageSize + 1 }}–{{
              Math.min((guardPage + 1) * pageSize, guards.length)
            }}
            / {{ guards.length }}</span
          ><button class="button" :disabled="guardPage === 0" @click="guardPage--">上一页</button
          ><button
            class="button"
            :disabled="(guardPage + 1) * pageSize >= guards.length"
            @click="guardPage++"
          >
            下一页
          </button>
        </div>
      </section>
      <MutationPane
        v-else-if="tab === 'mutation'"
        :report="detail"
        :reports="reports.reports"
        :files="files"
        @open-source="emit('openSource', $event)"
      />
      <section v-else aria-label="基线比较">
        <p class="quality-note">
          选择两份已有报告，分别比较函数 CRAP
          和既有守卫结果。口径不兼容时保留未知，缺失或消失不算改善。
        </p>
        <div class="comparison-picker">
          <label
            >基线报告<select v-model="beforeId">
              <option value="">请选择基线</option>
              <option v-for="item in reports.reports" :key="item.id" :value="item.id">
                {{ reportLabel(item.id) }}
              </option>
            </select></label
          ><span aria-hidden="true">→</span
          ><label
            >对照报告<select v-model="selectedId">
              <option v-for="item in reports.reports" :key="item.id" :value="item.id">
                {{ reportLabel(item.id) }}
              </option>
            </select></label
          ><button
            class="button primary"
            :disabled="comparing || !beforeId || beforeId === selectedId"
            @click="runComparison"
          >
            {{ comparing ? '比较中…' : '比较报告' }}
          </button>
        </div>
        <p v-if="beforeId && beforeId === selectedId" class="quality-note">
          请选择两份不同的报告。
        </p>
        <div v-if="compareError" class="message error-message" role="alert">{{ compareError }}</div>
        <div v-if="comparing" class="quality-empty" role="status">
          正在核对两份报告的口径与函数身份…
        </div>
        <template v-else-if="compared">
          <div class="quality-section-heading"><h3>函数 CRAP 差异</h3></div>
          <div class="comparison-provenance">
            <strong>{{
              {
                comparable: '两组口径兼容',
                partial: '部分分组不可比较',
                incomparable: '报告不可直接比较'
              }[compared.comparison.status]
            }}</strong>
            <div v-for="group in groups" :key="group">
              <span
                >{{ groupNames[group] }}：{{
                  compared.comparison.groups[group].status === 'comparable' ? '可比较' : '不可比较'
                }}</span
              >
              <p v-for="reason in compared.comparison.groups[group].reasons" :key="reason">
                {{ reason }}
              </p>
            </div>
          </div>
          <details v-if="compared.errors.length" class="quality-diagnostics">
            <summary>比较提示 · {{ compared.errors.length }}</summary>
            <p v-for="(error, index) in compared.errors.slice(0, 10)" :key="index">
              {{ error.message }}
            </p>
          </details>
          <nav class="change-filters" aria-label="差异类型">
            <button
              v-for="bucket in changeBuckets"
              :key="bucket"
              :class="{ active: changeBucket === bucket }"
              :aria-pressed="changeBucket === bucket"
              @click="changeBucket = bucket"
            >
              {{ changeNames[bucket] }} <strong>{{ compared.comparison[bucket].length }}</strong>
            </button>
          </nav>
          <div v-if="visibleChanges.length" class="quality-table-scroll">
            <table class="quality-table comparison-table">
              <thead>
                <tr>
                  <th>函数 / 来源</th>
                  <th>基线 CRAP</th>
                  <th>对照 CRAP</th>
                  <th>变化</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(change, index) in visibleChanges" :key="index">
                  <td>
                    <button
                      v-if="change.after"
                      class="function-link"
                      :disabled="
                        !compared.after.source.can_navigate || !!navigationReason(change.after)
                      "
                      :title="navigationReason(change.after) || '查看当前源码'"
                      @click="openSource(change.after)"
                    >
                      {{ change.name ?? change.after.name }}</button
                    ><strong v-else class="function-name">{{
                      change.name ?? '无法匹配的函数'
                    }}</strong
                    ><small class="source-caption">{{ change.path ?? '来源未知' }}</small
                    ><small class="row-reason">{{ change.reason }}</small>
                  </td>
                  <td data-label="基线 CRAP">{{ number(change.before?.crap) }}</td>
                  <td data-label="对照 CRAP">{{ number(change.after?.crap) }}</td>
                  <td
                    data-label="变化"
                    :class="{ 'risk-score': change.delta !== null && change.delta > 0 }"
                  >
                    {{
                      change.delta === null
                        ? '—'
                        : `${change.delta > 0 ? '+' : ''}${number(change.delta)}`
                    }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="quality-empty">这两份报告没有“{{ changeNames[changeBucket] }}”条目。</p>
          <div v-if="changes.length > pageSize" class="quality-pagination">
            <span
              >{{ changePage * pageSize + 1 }}–{{
                Math.min((changePage + 1) * pageSize, changes.length)
              }}
              / {{ changes.length }}</span
            ><button class="button" :disabled="changePage === 0" @click="changePage--">
              上一页</button
            ><button
              class="button"
              :disabled="(changePage + 1) * pageSize >= changes.length"
              @click="changePage++"
            >
              下一页
            </button>
          </div>
          <section class="guard-comparison" aria-label="既有守卫基线差异">
            <div class="quality-section-heading">
              <h3>既有守卫差异</h3>
              <span>按规则与原生用例名称匹配</span>
            </div>
            <p class="quality-note">
              只有同一守卫用例由失败变为通过才计为改善。跳过、缺失和执行错误保留未知；用例消失不代表风险消除。
            </p>
            <template v-if="compared.guard_comparison">
              <div class="comparison-provenance">
                <strong>{{
                  compared.guard_comparison.status === 'comparable'
                    ? '守卫测试与采集口径兼容'
                    : '守卫结果不可直接比较'
                }}</strong>
                <p v-for="reason in compared.guard_comparison.reasons" :key="reason">
                  {{ reason }}
                </p>
              </div>
              <nav class="change-filters" aria-label="守卫差异类型">
                <button
                  v-for="bucket in changeBuckets"
                  :key="bucket"
                  :class="{ active: guardChangeBucket === bucket }"
                  :aria-pressed="guardChangeBucket === bucket"
                  @click="guardChangeBucket = bucket"
                >
                  {{ bucket === 'removed' ? '消失' : changeNames[bucket] }}
                  <strong>{{ compared.guard_comparison[bucket].length }}</strong>
                </button>
              </nav>
              <ul v-if="visibleGuardChanges.length" class="guard-list">
                <li v-for="(change, index) in visibleGuardChanges" :key="index">
                  <div>
                    <strong>{{ guardNames[change.rule] ?? change.rule }}</strong
                    ><span
                      class="quality-badge"
                      :class="
                        guardChangeBucket === 'improved'
                          ? 'positive'
                          : guardChangeBucket === 'worsened'
                            ? 'warning'
                            : 'neutral'
                      "
                      >{{
                        guardChangeBucket === 'removed' ? '消失' : changeNames[guardChangeBucket]
                      }}</span
                    >
                  </div>
                  <p class="source-caption">{{ change.test ?? '原生用例未知' }}</p>
                  <p class="quality-note">
                    基线：{{ label(change.before?.status) }} → 对照：{{
                      label(change.after?.status)
                    }}
                  </p>
                  <p class="row-reason">{{ change.reason }}</p>
                </li>
              </ul>
              <p v-else class="quality-empty">
                这两份报告没有“{{
                  guardChangeBucket === 'removed' ? '消失' : changeNames[guardChangeBucket]
                }}”守卫条目。
              </p>
              <div v-if="guardChanges.length > pageSize" class="quality-pagination">
                <span
                  >{{ guardChangePage * pageSize + 1 }}–{{
                    Math.min((guardChangePage + 1) * pageSize, guardChanges.length)
                  }}
                  / {{ guardChanges.length }}</span
                ><button
                  class="button"
                  :disabled="guardChangePage === 0"
                  @click="guardChangePage--"
                >
                  上一页</button
                ><button
                  class="button"
                  :disabled="(guardChangePage + 1) * pageSize >= guardChanges.length"
                  @click="guardChangePage++"
                >
                  下一页
                </button>
              </div>
            </template>
            <p v-else class="quality-empty">未提供守卫比较结果，不能推断改善。</p>
          </section>
        </template>
        <p v-else-if="!comparing" class="quality-empty">选好基线后点击“比较报告”，查看本次差异。</p>
      </section>
    </template>
  </main>
</template>

<style scoped>
.quality-pane {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 26px;
  min-height: 480px;
}
.quality-heading,
.quality-section-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 18px;
}
.quality-heading {
  margin-bottom: 25px;
}
.quality-heading h2 {
  font-size: 22px;
}
.quality-subtitle,
.quality-note {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.8;
}
.quality-subtitle {
  margin-top: 8px;
}
.quality-note {
  margin: 12px 0;
}
.report-picker {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 12px;
}
select,
input {
  min-width: 0;
  color: var(--text);
  border: 1px solid #d5dfd6;
  border-radius: 5px;
  background: white;
  padding: 9px 10px;
  font-size: 12px;
}
.report-picker select {
  width: min(430px, 100%);
}
.quality-badge {
  display: inline-block;
  border-radius: 4px;
  padding: 4px 7px;
  font-size: 10px;
  line-height: 1.5;
  white-space: nowrap;
}
.positive {
  background: #e8f1e9;
  color: #31604b;
}
.warning {
  background: #faf0dc;
  color: #856221;
}
.neutral {
  background: #eaf0f0;
  color: #64797a;
}
.quality-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
  margin: 22px 0 24px;
  border-bottom: 1px solid var(--border);
}
.quality-tabs button {
  border: 0;
  background: none;
  border-bottom: 2px solid transparent;
  color: var(--muted);
  padding: 14px 0;
  font-size: 13px;
}
.quality-tabs .active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}
.quality-section-heading {
  margin-top: 20px;
}
.quality-section-heading h3 {
  font-size: 16px;
}
.quality-section-heading > span {
  color: var(--muted);
  font-size: 11px;
}
.risk-filters {
  display: grid;
  grid-template-columns: 150px minmax(180px, 1fr) 105px 145px;
  gap: 13px;
  margin: 22px 0;
}
.risk-filters label,
.comparison-picker label {
  display: grid;
  gap: 7px;
  color: var(--muted);
  font-size: 11px;
  min-width: 0;
}
.quality-stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  border: 1px solid var(--border);
  border-radius: 7px;
  margin-bottom: 23px;
}
.quality-stats > div {
  padding: 18px 20px;
  display: grid;
  gap: 7px;
}
.quality-stats > div + div {
  border-left: 1px solid var(--border);
}
.quality-stats span,
.quality-stats small {
  font-size: 11px;
  color: var(--muted);
}
.quality-stats strong {
  font-size: 25px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.quality-table-scroll {
  overflow-x: auto;
  width: 100%;
}
.quality-table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
  font-size: 12px;
  table-layout: fixed;
}
.quality-table th {
  font-size: 10px;
  font-weight: 500;
  color: var(--muted);
  padding: 12px 10px;
  border-bottom: 1px solid var(--border);
  background: #f2f6ef;
}
.quality-table td {
  padding: 15px 10px;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
  font-variant-numeric: tabular-nums;
}
.quality-table th:first-child {
  width: 54%;
}
.quality-table th:not(:first-child),
.quality-table td:not(:first-child) {
  text-align: right;
}
.quality-table th:last-child {
  width: 90px;
}
.function-link,
.function-name {
  font-size: 12px;
  line-height: 1.7;
  text-align: left;
  overflow-wrap: anywhere;
  font-weight: 550;
}
.function-link {
  border: 0;
  padding: 0;
  background: none;
  color: var(--accent);
}
.function-link:hover:not(:disabled) {
  text-decoration: underline;
}
.function-link:disabled {
  cursor: not-allowed;
  color: var(--text);
  opacity: 1;
}
.source-caption {
  display: block;
  color: var(--muted);
  font-size: 10px;
  overflow-wrap: anywhere;
  line-height: 1.8;
  margin-top: 4px;
}
.row-reason {
  display: block;
  margin-top: 6px;
  line-height: 1.7;
  color: #92784d;
  font-size: 11px;
  overflow-wrap: anywhere;
}
.score-cell {
  font-weight: 650;
}
.risk-score,
.warning-copy {
  color: #946123;
}
.quality-pagination {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 9px;
  margin-top: 18px;
}
.quality-pagination span {
  margin-right: auto;
  color: var(--muted);
  font-size: 11px;
}
.quality-pagination .button {
  padding: 7px 12px;
}
.quality-empty {
  padding: 48px 20px;
  color: var(--muted);
  font-size: 13px;
  text-align: center;
  line-height: 1.9;
}
.quality-empty h3 {
  color: #526d5d;
  font-size: 16px;
  margin-bottom: 8px;
}
.quality-diagnostics {
  color: #8a6c3e;
  font-size: 11px;
  margin: 16px 0;
}
.quality-diagnostics summary {
  cursor: pointer;
  padding: 5px 0;
}
.quality-diagnostics p {
  line-height: 1.8;
  margin-top: 7px;
  overflow-wrap: anywhere;
}
.check-groups {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
}
.check-group {
  border: 1px solid var(--border);
  border-radius: 7px;
  padding: 18px;
  min-width: 0;
}
.check-group summary {
  cursor: pointer;
  font-size: 13px;
}
.check-group summary .quality-badge {
  float: right;
}
.check-steps {
  padding: 0;
  margin: 0;
  list-style: none;
}
.check-steps li {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 6px;
  padding: 11px 0;
  border-top: 1px solid var(--border);
  font-size: 11px;
}
.check-steps small {
  grid-column: 1 / -1;
  color: var(--muted);
}
.guard-list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.guard-list li {
  padding: 15px 0;
  border-bottom: 1px solid var(--border);
}
.guard-list li > div {
  display: flex;
  align-items: center;
  gap: 15px;
}
.guard-list strong {
  font-size: 12px;
  font-weight: 500;
}
.comparison-picker {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr) auto;
  align-items: end;
  gap: 15px;
  margin: 22px 0;
}
.comparison-picker > span {
  padding-bottom: 9px;
  color: var(--muted);
}
.comparison-provenance {
  padding: 17px 20px;
  background: #f0f4ed;
  border-radius: 7px;
  font-size: 12px;
  line-height: 1.8;
}
.comparison-provenance > div {
  margin-top: 7px;
}
.comparison-provenance p {
  font-size: 11px;
  color: #8a6c3e;
}
.change-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 20px 0;
}
.change-filters button {
  border: 1px solid var(--border);
  border-radius: 5px;
  background: white;
  padding: 8px 12px;
  font-size: 11px;
}
.change-filters button.active {
  background: #e7f0e7;
  border-color: #adccba;
  color: var(--accent);
}
.change-filters strong {
  margin-left: 5px;
  font-variant-numeric: tabular-nums;
}
@media (max-width: 760px) {
  .quality-pane {
    padding: 19px 15px;
  }
  .quality-heading {
    align-items: flex-start;
  }
  .quality-heading h2 {
    font-size: 20px;
  }
  .quality-heading .button {
    flex-shrink: 0;
  }
  .report-picker {
    align-items: stretch;
  }
  .report-picker label {
    flex-basis: 100%;
  }
  .report-picker select {
    width: 100%;
  }
  .quality-tabs {
    gap: 21px;
  }
  .quality-tabs button {
    font-size: 12px;
  }
  .quality-section-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 8px;
  }
  .risk-filters {
    grid-template-columns: 1fr 1fr;
  }
  .risk-search {
    grid-column: 1 / -1;
    grid-row: 1;
  }
  .risk-filters label:last-child {
    grid-column: 1 / -1;
  }
  .quality-stats > div {
    padding: 13px 10px;
  }
  .quality-stats strong {
    font-size: 22px;
  }
  .quality-stats small {
    line-height: 1.7;
    font-size: 10px;
  }
  .quality-table {
    display: block;
    min-width: 0;
  }
  .quality-table thead {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }
  .quality-table tbody {
    display: block;
  }
  .quality-table tr {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    padding: 14px 0;
    border-bottom: 1px solid var(--border);
  }
  .comparison-table tr {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
  .quality-table td {
    border: 0;
    padding: 5px 3px;
    min-width: 0;
  }
  .quality-table td:first-child {
    grid-column: 1 / -1;
    padding-bottom: 12px;
  }
  .quality-table td:not(:first-child) {
    text-align: left;
  }
  .quality-table td[data-label]::before {
    display: block;
    content: attr(data-label);
    margin-bottom: 7px;
    font-size: 10px;
    color: var(--muted);
    font-weight: 400;
  }
  .check-groups {
    grid-template-columns: 1fr;
  }
  .comparison-picker {
    grid-template-columns: 1fr;
    gap: 12px;
  }
  .comparison-picker > span {
    display: none;
  }
  .comparison-picker .button {
    justify-self: start;
  }
  .quality-empty {
    padding: 35px 6px;
  }
}
</style>
