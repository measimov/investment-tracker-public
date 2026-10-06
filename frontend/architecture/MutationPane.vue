<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { SourceFile } from './types'
import type {
  MutationComparison,
  QualitySourceLocation,
  ReportDetail,
  ReportListItem
} from './quality-types'

const props = defineProps<{
  report: ReportDetail
  reports: ReportListItem[]
  files: SourceFile[]
}>()
const emit = defineEmits<{ openSource: [location: QualitySourceLocation] }>()
const mutation = computed(() => props.report.mutation ?? null)
const visibleLimit = ref(20)
const beforeId = ref('')
const comparison = ref<MutationComparison | null>(null)
const comparing = ref(false)
const error = ref('')
let controller: AbortController | null = null
const statusNames: Record<string, string> = {
  completed: '已完成',
  incomplete: '未完成',
  failed: '失败',
  running: '运行中',
  passed: '通过',
  unknown: '未知',
  killed: '已检出',
  survived: '未检出',
  timeout: '超时',
  tool_error: '工具错误',
  no_coverage: '无覆盖',
  not_checked: '未执行'
}
const countKinds = [
  'killed',
  'survived',
  'timeout',
  'tool_error',
  'no_coverage',
  'not_checked',
  'unknown'
]
const phases = [
  { key: 'baseline', label: '原测试基线' },
  { key: 'mutation', label: '变异执行' }
] as const
const baselineOptions = computed(() => props.reports.filter((item) => item.id !== props.report.id))
const comparisonHasScores = computed(
  () =>
    comparison.value?.status === 'comparable' &&
    validScore(comparison.value.before_score) &&
    validScore(comparison.value.after_score) &&
    typeof comparison.value.delta === 'number' &&
    Number.isFinite(comparison.value.delta)
)
const score = computed(() => {
  const report = mutation.value
  return report?.status === 'completed' &&
    report.steps.baseline.status === 'passed' &&
    report.steps.mutation.status === 'passed' &&
    validScore(report.mutation_score)
    ? report.mutation_score
    : null
})
const navigationReason = computed(() => {
  const report = mutation.value
  if (!report?.source.can_navigate || report.source.status !== 'current')
    return '报告与当前源码未匹配，函数定位已禁用。'
  const hash = report.source_files[report.scope.path]
  if (!hash || props.files.find((file) => file.path === report.scope.path)?.sha256 !== hash)
    return '函数源码与当前索引的指纹不同，请重新扫描后核对。'
  if (report.line === null || !Number.isInteger(report.line) || report.line < 1)
    return '没有可核实的函数定义行，定位已禁用。'
  return ''
})

function label(status: string | null | undefined) {
  return status ? (statusNames[status] ?? status) : '未提供'
}
function validScore(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1
}
function percent(value: number | null) {
  return validScore(value) ? `${(value * 100).toFixed(1)}%` : '—'
}
function duration(value: number | null) {
  return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(2)} 秒` : '耗时未知'
}
function openSource() {
  const report = mutation.value
  if (!report || navigationReason.value) return
  emit('openSource', {
    path: report.scope.path,
    line: report.line!,
    sha256: report.source_files[report.scope.path]
  })
}
function clearComparison() {
  controller?.abort()
  controller = null
  comparing.value = false
  comparison.value = null
  error.value = ''
}
async function compareReports() {
  clearComparison()
  const before = beforeId.value
  const after = props.report.id
  if (!before || before === after) return
  const request = new AbortController()
  controller = request
  comparing.value = true
  try {
    const response = await fetch(`/api/compare?${new URLSearchParams({ before, after })}`, {
      signal: request.signal
    })
    const body = await response.json()
    if (request.signal.aborted || controller !== request) return
    if (!response.ok)
      throw new Error(typeof body.detail === 'string' ? body.detail : '变异比较读取失败')
    if (
      body.before?.id !== before ||
      body.after?.id !== after ||
      !body.mutation_comparison ||
      !Array.isArray(body.mutation_comparison.reasons) ||
      !['comparable', 'incomparable'].includes(body.mutation_comparison.status)
    )
      throw new Error('没有可用的变异比较结果。')
    comparison.value = body.mutation_comparison as MutationComparison
  } catch (failure) {
    if (controller === request && !request.signal.aborted)
      error.value = failure instanceof Error ? failure.message : '变异比较读取失败'
  } finally {
    if (controller === request && !request.signal.aborted) comparing.value = false
  }
}
watch(beforeId, clearComparison)
watch(
  () => props.report,
  () => {
    clearComparison()
    visibleLimit.value = 20
  }
)
onBeforeUnmount(clearComparison)
</script>

<template>
  <section class="mutation-pane" aria-label="按需变异试点报告">
    <header class="mutation-heading">
      <h3>按需变异试点</h3>
      <span class="mutation-badge">观察项 · 只读报告</span>
    </header>
    <p class="mutation-note">查看已经执行的局部试点，结果独立于 CRAP，不合并为质量总分。</p>
    <div v-if="!mutation" class="mutation-empty">
      <h4>这次运行没有变异报告</h4>
      <p>选择已有的试点报告查看；未运行不表示零变异或满分。</p>
    </div>
    <template v-else>
      <div class="mutation-scope">
        <div class="mutation-heading">
          <strong>{{ mutation.scope.function }}</strong>
          <button
            class="mutation-button"
            :disabled="!!navigationReason"
            :title="navigationReason || '查看函数定义'"
            @click="openSource"
          >
            查看函数源码
          </button>
        </div>
        <p class="mutation-path">
          {{ mutation.scope.path }}{{ mutation.line ? `:${mutation.line}` : '' }}
        </p>
        <p class="mutation-note">定位到已核实的函数定义；报告未提供每个变异的精确源码行。</p>
        <p v-if="navigationReason" class="mutation-notice">{{ navigationReason }}</p>
        <h4>固定测试范围</h4>
        <ul class="mutation-tests">
          <li v-for="test in mutation.scope.tests" :key="test">{{ test }}</li>
        </ul>
        <p class="mutation-note">
          mutmut {{ mutation.tool.mutmut ?? '版本未知' }} · Python
          {{ mutation.tool.python ?? '版本未知' }} · pytest {{ mutation.tool.pytest ?? '版本未知' }}
        </p>
      </div>
      <div class="mutation-summary">
        <div>
          <span>试点状态</span><strong>{{ label(mutation.status) }}</strong>
        </div>
        <div>
          <span>变异分数{{ mutation.source.status === 'stale' ? '（历史）' : '' }}</span
          ><strong class="mutation-score">{{ percent(score) }}</strong>
        </div>
        <div>
          <span>已记录变异</span><strong>{{ mutation.mutants.length }}</strong>
        </div>
      </div>
      <p class="mutation-note">
        只在原测试基线通过、变异执行与结果完整时展示 killed / (killed +
        survived)。未知结果不记为零。
      </p>
      <p v-if="mutation.reason" class="mutation-notice">{{ mutation.reason }}</p>
      <p v-if="score === null" class="mutation-notice">尚无可确认的完整结果，变异分数未知。</p>
      <ul class="mutation-phases" aria-label="原测试基线与变异执行">
        <li v-for="phase in phases" :key="phase.key">
          <strong>{{ phase.label }}</strong>
          <span>{{ label(mutation.steps[phase.key].status) }}</span>
          <small
            >{{ duration(mutation.steps[phase.key].duration_seconds) }} · 退出码
            {{ mutation.steps[phase.key].exit_code ?? '—' }}</small
          >
        </li>
      </ul>
      <div class="mutation-counts" aria-label="原生结果汇总">
        <p v-for="kind in countKinds" :key="kind">
          <span>{{ label(kind) }}</span
          ><strong>{{ mutation.counts[kind] ?? '—' }}</strong>
        </p>
      </div>
      <p class="mutation-note">
        未检出（survived）表示当前测试没有发现该变异，不代表它与原代码等价；排除等价变异需人工确认并记录理由。
      </p>
      <details class="mutation-results" open>
        <summary>原生变异结果 · {{ mutation.mutants.length }}</summary>
        <ol v-if="mutation.mutants.length">
          <li v-for="row in mutation.mutants.slice(0, visibleLimit)" :key="row.name">
            <div class="mutant-heading">
              <strong>{{ row.name }}</strong
              ><span class="mutation-badge">{{ label(row.status) }}</span>
            </div>
            <p>原生状态：{{ row.native_status ?? '未知' }} · 退出码 {{ row.exit_code ?? '—' }}</p>
            <p v-if="row.status === 'survived'" class="mutation-notice">等价性待人工确认。</p>
            <p v-else-if="row.equivalence">等价性记录：{{ row.equivalence }}</p>
          </li>
        </ol>
        <p v-else class="mutation-empty">尚未取得原生变异结果。</p>
        <button
          v-if="mutation.mutants.length > visibleLimit"
          class="mutation-button"
          @click="visibleLimit += 20"
        >
          继续显示原生结果（{{ visibleLimit }} / {{ mutation.mutants.length }}）
        </button>
      </details>
      <section class="mutation-comparison" aria-label="变异结果比较">
        <h4>两份变异结果比较</h4>
        <p class="mutation-note">
          仅比较工具身份、函数与测试范围兼容的完整结果。缺失、未知或未完成不能算改善。
        </p>
        <div class="mutation-compare-picker">
          <label
            >变异基线报告<select v-model="beforeId" aria-label="变异基线报告">
              <option value="">请选择另一份报告</option>
              <option v-for="item in baselineOptions" :key="item.id" :value="item.id">
                {{ item.id
                }}{{
                  item.mutation_status ? ` · ${label(item.mutation_status)}` : ' · 未提供变异状态'
                }}
              </option>
            </select></label
          >
          <button
            class="mutation-button"
            :disabled="!beforeId || comparing"
            @click="compareReports"
          >
            {{ comparing ? '读取比较…' : '比较变异结果' }}
          </button>
        </div>
        <p class="mutation-note">对照报告：{{ report.id }}</p>
        <p v-if="error" class="mutation-notice" role="alert">{{ error }}</p>
        <p v-if="comparing" class="mutation-note" role="status">正在核对两份变异报告的口径…</p>
        <div v-else-if="comparison" class="mutation-comparison-result">
          <strong>{{
            comparisonHasScores ? '两份变异结果口径兼容' : '变异结果不可直接比较'
          }}</strong>
          <p v-for="reason in comparison.reasons" :key="reason" class="mutation-notice">
            {{ reason }}
          </p>
          <details v-if="comparison.changed_test_files?.length" class="mutation-results">
            <summary>测试内容有变更 · {{ comparison.changed_test_files.length }}</summary>
            <ul class="mutation-tests">
              <li v-for="path in comparison.changed_test_files" :key="path">{{ path }}</li>
            </ul>
          </details>
          <dl v-if="comparisonHasScores">
            <div>
              <dt>基线分数</dt>
              <dd>{{ percent(comparison.before_score) }}</dd>
            </div>
            <div>
              <dt>对照分数</dt>
              <dd>{{ percent(comparison.after_score) }}</dd>
            </div>
            <div>
              <dt>变化</dt>
              <dd>
                {{
                  typeof comparison.delta === 'number' && Number.isFinite(comparison.delta)
                    ? `${comparison.delta > 0 ? '+' : ''}${(comparison.delta * 100).toFixed(1)} 个百分点`
                    : '未知'
                }}
              </dd>
            </div>
          </dl>
          <p v-if="comparisonHasScores" class="mutation-note">
            {{
              {
                improved: '同口径变异分数提高。',
                worsened: '同口径变异分数降低。',
                unchanged: '同口径变异分数未变化。',
                unknown: '分数变化未知，不能推断改善。'
              }[comparison.change] ?? '分数变化未知，不能推断改善。'
            }}
          </p>
          <p v-else class="mutation-note">保留未知，不推断分数改善。</p>
        </div>
      </section>
    </template>
  </section>
</template>

<style scoped>
.mutation-pane {
  min-width: 0;
  font-size: 13px;
}
.mutation-heading,
.mutant-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 14px;
}
.mutation-heading h3 {
  font-size: 16px;
}
.mutation-heading strong,
.mutant-heading strong {
  overflow-wrap: anywhere;
  min-width: 0;
}
.mutation-badge {
  padding: 4px 7px;
  border-radius: 5px;
  background: #edf2ed;
  font-size: 11px;
  flex-shrink: 0;
}
.mutation-note,
.mutation-notice {
  margin: 10px 0;
  line-height: 1.8;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.mutation-note {
  color: var(--muted);
}
.mutation-notice {
  color: #856221;
}
.mutation-scope {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
  margin: 18px 0;
}
.mutation-path,
.mutation-tests {
  font-family: ui-monospace, monospace;
  font-size: 12px;
  line-height: 1.8;
  overflow-wrap: anywhere;
}
.mutation-path {
  margin-top: 8px;
}
.mutation-tests {
  padding-left: 18px;
  margin: 8px 0;
}
.mutation-scope h4 {
  font-size: 12px;
  margin-top: 18px;
}
.mutation-button {
  min-height: 38px;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 12px;
  background: transparent;
  color: var(--accent);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.mutation-button:disabled {
  cursor: default;
  color: var(--muted);
}
.mutation-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  border: 1px solid var(--border);
  border-radius: 8px;
}
.mutation-summary > div {
  display: grid;
  gap: 8px;
  padding: 16px;
}
.mutation-summary > div + div {
  border-left: 1px solid var(--border);
}
.mutation-summary span,
.mutation-comparison dt {
  font-size: 11px;
  color: var(--muted);
}
.mutation-summary strong {
  font-size: 22px;
}
.mutation-phases {
  list-style: none;
  margin: 20px 0;
  padding: 0;
}
.mutation-phases li {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 8px;
  padding: 14px 0;
  border-bottom: 1px solid var(--border);
}
.mutation-phases small {
  grid-column: 1 / -1;
  color: var(--muted);
}
.mutation-counts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
}
.mutation-counts p {
  display: flex;
  gap: 9px;
  line-height: 1.8;
}
.mutation-counts span {
  color: var(--muted);
}
.mutation-results {
  margin: 20px 0;
}
.mutation-results summary {
  cursor: pointer;
  padding: 10px 0;
}
.mutation-results ol {
  margin: 0;
  padding-left: 22px;
}
.mutation-results li {
  padding: 14px 0;
  border-bottom: 1px solid var(--border);
}
.mutation-results li p {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.8;
  overflow-wrap: anywhere;
}
.mutation-comparison {
  border-top: 1px solid var(--border);
  margin-top: 26px;
  padding-top: 22px;
}
.mutation-comparison h4 {
  font-size: 15px;
}
.mutation-compare-picker {
  display: flex;
  align-items: end;
  gap: 12px;
}
.mutation-compare-picker label {
  display: grid;
  gap: 8px;
  min-width: 0;
  flex: 1;
  font-size: 12px;
  color: var(--muted);
}
.mutation-compare-picker select {
  min-width: 0;
  width: 100%;
  min-height: 38px;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: white;
  color: var(--text);
  font: inherit;
}
.mutation-comparison-result {
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-top: 16px;
}
.mutation-comparison dl {
  display: flex;
  flex-wrap: wrap;
  gap: 20px 36px;
  margin: 16px 0 0;
}
.mutation-comparison dd {
  margin: 7px 0 0;
}
.mutation-empty {
  padding: 36px 16px;
  text-align: center;
  color: var(--muted);
  line-height: 1.9;
}
@media (max-width: 600px) {
  .mutation-heading,
  .mutant-heading {
    align-items: flex-start;
    flex-wrap: wrap;
    gap: 10px;
  }
  .mutation-summary > div {
    padding: 12px 9px;
  }
  .mutation-summary strong {
    font-size: 20px;
  }
  .mutation-compare-picker {
    align-items: stretch;
    flex-direction: column;
  }
  .mutation-compare-picker label {
    width: 100%;
  }
}
</style>
