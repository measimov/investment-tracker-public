<script setup lang="ts">
/**
 * 「报表」tab：财报摘要（LLM 分档摘要）、港股报表抽取进度、美/港股核心科目透视表、
 * A股利润与现金流摘要。
 */
import { computed, h, ref, type Directive } from 'vue'
import { NAlert, NButton, NDataTable, NEmpty, NSwitch, NTag, type DataTableColumns } from 'naive-ui'
import ResearchNote from './ResearchNote.vue'
import { EMPTY, formatDateTime } from '@/utils/helpers'
import {
  buildNotesText,
  currencySwitchText,
  epsNoteText,
  formatStatementPeriodKey,
  isCurrencyOutlier,
  isScrubbedCell,
  mergeHkPivotRows,
  sourceLabel,
  summarizePivotCurrency,
  suspectTooltipText,
  type HkPivotRow
} from '@/utils/hkStatements'
import {
  annualOnly,
  digestSourceHref,
  digestTypeLabel,
  formatEps,
  formatPerShare,
  formatPeriod,
  formatYi,
  periodKindLabel
} from './format'
import type { ProfileRow, SecurityProfileState } from './types'

const props = defineProps<{ state: SecurityProfileState; market: string }>()
const emit = defineEmits<{ backfill: [] }>()

// Naive 的公开 contentClass 定位内容层，父层为实际滚动区；保留原生左右键滚动。
const vTableScrollFocus: Directive<HTMLElement, string> = {
  mounted(element, { value }) {
    const container = element.querySelector<HTMLElement>('.sd-financial-content')?.parentElement
    if (!container) return
    container.classList.add('sd-financial-scroll')
    container.tabIndex = 0
    container.setAttribute('role', 'region')
    container.setAttribute('aria-label', value)
  }
}

const isA = computed(() => props.market === 'A股')
const isHk = computed(() => props.market === '港股')
const datasets = computed(() => props.state.datasets || {})

// ---- 财报摘要 ----
// 与后端 report_digest_prompts.DIGEST_FIELDS 同序（关键数字单列）
const DIGEST_FIELD_ORDER = [
  '经营回顾',
  '业务分部占比',
  '上下游与产业链',
  '主营收入结构',
  '成本与费用',
  '一次性项目',
  '会计信号',
  '风险要点',
  '展望'
]

function digestFields(digest: ProfileRow): string[] {
  return DIGEST_FIELD_ORDER.filter((field) => digest.digest?.[field])
}

function keyNumbers(digest: ProfileRow): string[] {
  const numbers = digest.digest?.['关键数字']
  return Array.isArray(numbers) ? numbers.map(String) : []
}

const backfillSummary = computed(() => {
  const result = props.state.backfillResult
  if (!result) return ''
  const parts = [
    `本次生成 ${result.generated ?? 0} 份，累计 ${result.completed ?? 0}/${result.total ?? 0} 份`
  ]
  if (result.remaining) parts.push(`剩余 ${result.remaining} 份可继续点击补齐`)
  const statements = result.statements as ProfileRow | undefined
  if (statements) {
    let line = `报表抽取：新抽 ${statements.generated ?? 0} 份，累计 ${statements.completed ?? 0}/${statements.total ?? 0} 份`
    if (statements.suspect) line += `，${statements.suspect} 期校验存疑`
    if (statements.remaining) line += `，剩余 ${statements.remaining} 份`
    parts.push(line)
  }
  if (result.gaps?.length) parts.push(`缺口：${result.gaps.join('；')}`)
  return parts.join('；')
})

// ---- 港股报表抽取进度 ----
const statementProgress = computed(() => props.state.statementProgress)
const showFailedReports = ref(false)
const failedReports = computed<ProfileRow[]>(
  () => (statementProgress.value?.failed_reports as ProfileRow[]) || []
)
const suspectPeriodsText = computed(() =>
  ((statementProgress.value?.suspect_periods as string[]) || [])
    .map(formatStatementPeriodKey)
    .join('、')
)
// 最近一次完整的披露易清单里没有中期报告（第二上市公司如 09618）：说「没有」而不是「缺」
const noInterimReports = computed(() => statementProgress.value?.planned_interim === 0)

// 构建层标注（已重列 / 小计已修正 / 雅虎口径不同）：info，不置空
function notesText(row: ProfileRow): string {
  return row.__notes ? buildNotesText((row as HkPivotRow).__notes) : ''
}

function notesTagLabel(row: ProfileRow): string {
  const notes = (row as HkPivotRow).__notes
  if (!notes) return ''
  if (notes.restated.length) return '已重列'
  if (notes.repaired.length) return '已修正'
  if (notes.yahooDefinitionDiff.length) return '口径'
  return ''
}

// 只有 PDF 取值的 EPS 才谈得上「仙→元」（雅虎补上的值本来就是元）
function epsNote(row: ProfileRow): string {
  if (!row.__notes || row.__source?.basic_eps !== 'pdf') return ''
  return epsNoteText((row as HkPivotRow).__notes)
}

// ---- 美股/港股核心科目透视 ----
const showInterim = ref(false)
const pivotRows = computed<ProfileRow[]>(() => {
  if (props.market === '美股') {
    // 美股行含季度，表格只展示 FY
    return (datasets.value.edgar_companyfacts || []).filter((row) => row.fp === 'FY')
  }
  if (isHk.value) {
    // 规则在 utils/hkStatements（与后端 merge_hk_statement_rows 同口径，有 spec）：PDF 行优先、
    // 存疑科目先置空、雅虎只补缺且币种须一致；行上带 __source/__suspect 供来源列与上标
    return mergeHkPivotRows(
      datasets.value.report_statements || [],
      datasets.value.yahoo_fundamentals || [],
      { includeInterim: showInterim.value }
    )
  }
  return []
})
const currencySummary = computed(() => summarizePivotCurrency(pivotRows.value))
const pivotUnitText = computed(() => {
  if (!pivotRows.value.length) return '单位 亿'
  const { uniform, majority } = currencySummary.value
  return uniform ? `单位 ${majority} 亿` : '单位 亿（多币种，见币种列）'
})
const pivotSourceHelp = computed(() =>
  props.market === '美股'
    ? '来源：SEC XBRL（EDGAR companyfacts），只列财年（FY）行。金额按发行人的报告币种（中概 20-F 发行人多为人民币，不取美元便利折算）；EPS 为每股普通股，不是每股 ADS。'
    : '来源：披露易年报/中报原文抽取（生成分析或补齐摘要时自动抽取，可达十年）+ 雅虎补缺；雅虎补上的科目带「雅」上标；校验存疑的科目已置空（✕）。'
)

const PIVOT_COLUMNS = [
  { field: 'total_revenue', label: '营业收入' },
  { field: 'n_income_attr_p', label: '归母净利润' },
  { field: 'n_cashflow_act', label: '经营现金流' },
  { field: 'total_assets', label: '总资产' },
  { field: 'total_hldr_eqy_exc_min_int', label: '股东权益' }
]

function scrubbed(row: ProfileRow, field: string): boolean {
  return Array.isArray(row.__suspectFields) && isScrubbedCell(row as HkPivotRow, field)
}

// 雅虎补上的科目打上标（PDF 值不打）；非港股行没有 __source
function cellSup(row: ProfileRow, field: string): string {
  return row.__source?.[field] === 'yahoo' ? '雅' : ''
}

// ---- A股利润与现金流摘要 ----
const statementAnnualOnly = ref(false)
// 三大报表按报告期合并为一行（利润表/现金流/资产负债各取核心科目）
const statementRows = computed(() => {
  const merged = new Map<string, ProfileRow>()
  for (const dataset of ['income', 'cashflow', 'balancesheet']) {
    for (const row of datasets.value[dataset] || []) {
      const key = String(row.end_date || '')
      if (!key) continue
      merged.set(key, { ...(merged.get(key) || {}), ...row })
    }
  }
  const rows = [...merged.entries()].sort((a, b) => (a[0] < b[0] ? 1 : -1)).map(([, row]) => row)
  return (statementAnnualOnly.value ? annualOnly(rows) : rows).slice(0, 8)
})

function note(label: string, text: string, caption?: string) {
  return h(ResearchNote, { label, text, caption })
}
const failedColumns: DataTableColumns<ProfileRow> = [
  {
    title: '报告',
    key: 'report',
    width: 160,
    render: (row) =>
      `${formatPeriod(row.end_date)} ${row.report_type === 'interim' ? '中报' : '年报'}`
  },
  {
    title: '状态',
    key: 'status',
    width: 120,
    render: (row) =>
      h(
        NTag,
        { type: row.capped ? 'error' : 'warning', size: 'small', bordered: false },
        { default: () => (row.capped ? '已封顶' : `可重试（${row.attempts} 次）`) }
      )
  },
  { title: '原因', key: 'error', minWidth: 200, render: (row) => row.error || EMPTY }
]
function pivotValue(row: ProfileRow, field: string) {
  if (scrubbed(row, field))
    return note(
      `${field === 'basic_eps' ? 'EPS' : PIVOT_COLUMNS.find((column) => column.field === field)?.label}校验说明`,
      '校验存疑，已置空',
      '✕'
    )
  if (field === 'basic_eps' && epsNote(row))
    return h('span', [formatPerShare(row.basic_eps), note('EPS折元依据', epsNote(row), '折')])
  return h('span', [
    field === 'basic_eps' ? formatEps(row.basic_eps) : formatYi(row[field]),
    h('sup', { class: 'sd-src-sup' }, cellSup(row, field))
  ])
}
const pivotColumns = computed<DataTableColumns<ProfileRow>>(() => [
  {
    title: showInterim.value ? '期末' : '财年止',
    key: 'period',
    width: 130,
    fixed: 'left',
    render: (row) =>
      h('span', [
        formatPeriod(row.end_date),
        ...(row.fp === 'H1'
          ? [note('中报期别说明', '中报：六个月数，不可与年度数直接比较', '6M')]
          : [])
      ])
  },
  {
    title: '币种',
    key: 'currency',
    width: 90,
    render: (row) =>
      h('span', [
        h(
          'span',
          { class: { 'sd-currency-outlier': isCurrencyOutlier(row, currencySummary.value) } },
          row.currency || EMPTY
        ),
        ...(currencySwitchText(pivotRows.value, row)
          ? [
              h(ResearchNote, {
                label: '报告币种变更说明',
                text: currencySwitchText(pivotRows.value, row),
                caption: '换币',
                'data-testid': 'pivot-currency-switch'
              })
            ]
          : [])
      ])
  },
  ...PIVOT_COLUMNS.map((column) => ({
    title: column.label,
    key: column.field,
    minWidth: 100,
    align: 'right' as const,
    className: 'sd-numeric',
    render: (row: ProfileRow) => pivotValue(row, column.field)
  })),
  {
    title: () =>
      h('span', [
        'EPS',
        ...(isHk.value
          ? [
              note(
                'EPS单位说明',
                '每股金额（原币元，不按亿换算）。原文以「仙」列示的期别已 ÷100 折元（带「折」标记，可展开查看依据）'
              )
            ]
          : [])
      ]),
    key: 'basic_eps',
    minWidth: 90,
    align: 'right',
    className: 'sd-numeric',
    render: (row) => pivotValue(row, 'basic_eps')
  },
  ...(isHk.value
    ? [
        {
          title: '来源',
          key: 'source',
          minWidth: 160,
          render: (row: ProfileRow) =>
            h('span', [
              row.__sourceKinds ? sourceLabel(row as HkPivotRow) : '',
              ...(row.__suspect
                ? [note('报表校验存疑依据', suspectTooltipText(row as HkPivotRow), '存疑')]
                : []),
              ...(notesTagLabel(row)
                ? [note('报表修正与口径依据', notesText(row), notesTagLabel(row))]
                : [])
            ])
        }
      ]
    : [])
])
const statementColumns: DataTableColumns<ProfileRow> = [
  {
    title: '报告期',
    key: 'period',
    width: 120,
    fixed: 'left',
    render: (row) => formatPeriod(row.end_date)
  },
  { title: '期别', key: 'kind', width: 70, render: (row) => periodKindLabel(row.end_date) },
  ...[
    { key: 'total_revenue', title: '营业总收入', width: 100 },
    { key: 'operate_profit', title: '营业利润', width: 92 },
    { key: 'n_income', title: '归母净利润', width: 100 },
    { key: 'n_cashflow_act', title: '经营现金流净额', width: 118 },
    { key: 'n_cashflow_inv_act', title: '投资现金流净额', width: 118 },
    { key: 'total_assets', title: '总资产', width: 92 },
    { key: 'total_hldr_eqy_exc_min_int', title: '净资产', width: 92 }
  ].map((column) => ({
    key: column.key,
    title: column.title,
    minWidth: column.width,
    align: 'right' as const,
    className: 'sd-numeric',
    render: (row: ProfileRow) =>
      formatYi(column.key === 'n_income' ? (row.n_income_attr_p ?? row.n_income) : row[column.key])
  }))
]
</script>

<template>
  <!-- 财报摘要 -->
  <section
    v-if="state.capabilities.report_digest"
    class="sd-block digest-section"
    data-testid="report-digest-section"
  >
    <div class="sd-block-header">
      <h3 class="sd-block-title">
        财报摘要
        <span class="sd-title-note"
          >已摘要 {{ state.digestProgress.digested }} 份<template
            v-if="state.digestProgress.failed_capped"
            >，{{ state.digestProgress.failed_capped }} 份获取失败</template
          ></span
        >
      </h3>
      <NButton
        size="small"
        secondary
        :loading="state.backfilling"
        data-testid="backfill-digests-button"
        @click="emit('backfill')"
      >
        补齐历史摘要
      </NButton>
    </div>
    <NAlert v-if="backfillSummary" type="default" :closable="false" class="block-alert">{{
      backfillSummary
    }}</NAlert>
    <el-collapse v-if="state.reportDigests.length" class="digest-collapse">
      <el-collapse-item
        v-for="digest in state.reportDigests"
        :key="digest.period_key"
        :name="digest.period_key"
      >
        <template #title>
          <div class="digest-title">
            <span class="sd-num">{{ formatPeriod(digest.end_date) }}</span>
            <NTag size="small" :bordered="false">{{ digestTypeLabel(digest.report_type) }}</NTag>
            <NTag v-if="digest.digest_tier === 'C'" size="small" :bordered="false">精简摘要</NTag>
            <span class="digest-title-spacer" />
            <a
              v-if="digestSourceHref(digest.source_url)"
              :href="digestSourceHref(digest.source_url)!"
              target="_blank"
              rel="noopener noreferrer"
              class="digest-source"
              @click.stop
              @keydown.stop
            >
              原文
            </a>
          </div>
        </template>
        <p v-if="digest.digest_tier === 'C'" class="sd-footnote">
          较早年份只摘要核心三项（主营收入结构 / 一次性项目 / 会计信号）与关键数字
        </p>
        <dl class="sd-field-list">
          <template v-for="field in digestFields(digest)" :key="field">
            <dt>{{ field }}</dt>
            <dd class="digest-narrative">{{ digest.digest[field] }}</dd>
          </template>
          <template v-if="keyNumbers(digest).length">
            <dt>关键数字</dt>
            <dd>
              <ul class="sd-number-grid">
                <li v-for="(num, index) in keyNumbers(digest)" :key="index">{{ num }}</li>
              </ul>
            </dd>
          </template>
        </dl>
      </el-collapse-item>
    </el-collapse>
    <NEmpty
      v-else
      description="暂无财报摘要；点击「补齐历史摘要」抓取年报并生成（每次最多 4 份，可重复点击续跑）"
    />
  </section>

  <!-- 报表抽取进度（港股：披露易年报/中报三张表） -->
  <section
    v-if="state.capabilities.statements && statementProgress"
    class="sd-block"
    data-testid="statement-progress-section"
  >
    <div class="sd-block-header">
      <h3 class="sd-block-title">
        报表抽取
        <span class="sd-title-note">
          年报 {{ statementProgress.annual_periods?.length || 0 }} 期 · 中报
          {{ statementProgress.interim_periods?.length || 0 }} 期<template
            v-if="statementProgress.reports_failed"
          >
            · {{ statementProgress.reports_failed }} 份失败<template
              v-if="statementProgress.reports_capped"
              >（{{ statementProgress.reports_capped }} 份已封顶）</template
            ></template
          ><template v-if="statementProgress.reports_stale">
            · {{ statementProgress.reports_stale }} 份待重抽</template
          ><template v-if="statementProgress.suspect_count">
            · {{ statementProgress.suspect_count }} 期校验存疑</template
          >
        </span>
      </h3>
    </div>
    <div class="progress-meta">
      <span v-if="statementProgress.last_extracted_at">
        最近抽取 {{ formatDateTime(statementProgress.last_extracted_at) }}
      </span>
      <span v-else>尚未抽取；「补齐历史摘要」或生成分析时自动抽取</span>
      <span v-if="noInterimReports" data-testid="statement-no-interim"
        >该公司未在披露易发布中期报告</span
      >
      <span v-if="suspectPeriodsText">存疑期：{{ suspectPeriodsText }}</span>
      <NButton
        v-if="failedReports.length"
        text
        :aria-expanded="showFailedReports"
        @click="showFailedReports = !showFailedReports"
        >{{ showFailedReports ? '收起失败清单' : '查看失败清单' }}</NButton
      >
    </div>
    <div
      v-if="showFailedReports && failedReports.length"
      class="sd-table-scroll"
      tabindex="0"
      role="region"
      aria-label="失败报告清单，可横向滚动"
    >
      <NDataTable
        class="sd-data-table"
        style="min-width: 480px"
        :columns="failedColumns"
        :data="failedReports"
        :bordered="false"
      />
    </div>
  </section>

  <!-- 核心科目透视（美股 EDGAR / 港股 PDF 抽取 + 雅虎补缺） -->
  <section v-if="!isA" class="sd-block" data-testid="pivot-statements-section">
    <div class="sd-block-header">
      <h3 class="sd-block-title">
        {{ showInterim ? '核心科目（年报 + 中报）' : '年度核心科目' }}
        <span class="sd-title-note" data-testid="pivot-unit">{{ pivotUnitText }}</span>
        <ResearchNote popover label="核心科目来源与单位说明" :text="pivotSourceHelp" />
      </h3>
      <label v-if="isHk" class="statement-switch"
        ><NSwitch
          v-model:value="showInterim"
          aria-label="显示中报"
          data-testid="pivot-show-interim"
        />显示中报</label
      >
    </div>
    <p class="sd-financial-scroll-hint">
      左右滑动查看更多列；首列固定便于核对，也可用左右方向键滚动。
    </p>
    <div v-table-scroll-focus="'核心科目透视表，可横向滚动'" class="sd-fixed-table">
      <NDataTable
        class="sd-data-table"
        :scroll-x="isHk ? 1070 : 910"
        :scrollbar-props="{ contentClass: 'sd-financial-content' }"
        :columns="pivotColumns"
        :data="pivotRows"
        :bordered="false"
        :row-class-name="(row) => (row.fp === 'H1' ? 'sd-interim-row' : '')"
        ><template #empty><NEmpty description="暂无数据；生成分析时会自动同步" /></template
      ></NDataTable>
    </div>
  </section>

  <!-- 利润与现金流摘要（A股三大报表） -->
  <section v-if="isA" class="sd-block">
    <div class="sd-block-header">
      <h3 class="sd-block-title">
        利润与现金流摘要
        <span class="sd-title-note"
          >合并报表，亿元<template v-if="state.latestPeriods.income"
            >，最新报告期 {{ formatPeriod(state.latestPeriods.income) }}</template
          ></span
        >
        <ResearchNote
          popover
          label="利润与现金流期别说明"
          text="一季报/中报/三季报为年初至今累计值，与年报混排时注意期别"
        />
      </h3>
      <label class="statement-switch"
        ><NSwitch
          v-model:value="statementAnnualOnly"
          aria-label="利润与现金流摘要只看年报"
        />只看年报</label
      >
    </div>
    <p class="sd-financial-scroll-hint">
      左右滑动查看更多列；首列固定便于核对，也可用左右方向键滚动。
    </p>
    <div v-table-scroll-focus="'利润与现金流摘要表，可横向滚动'" class="sd-fixed-table">
      <NDataTable
        class="sd-data-table"
        :scroll-x="902"
        :scrollbar-props="{ contentClass: 'sd-financial-content' }"
        :columns="statementColumns"
        :data="statementRows"
        :bordered="false"
        ><template #empty><NEmpty description="暂无数据；生成分析时会自动同步" /></template
      ></NDataTable>
    </div>
  </section>
</template>

<style scoped>
.block-alert {
  margin-bottom: 10px;
}

.digest-narrative {
  max-width: 38em;
  font-family: var(--app-font-serif);
  font-size: 18px;
  line-height: 1.8;
  overflow-wrap: anywhere;
}

@media (max-width: 640px) {
  .digest-narrative {
    font-size: 17px;
  }
}

.digest-collapse {
  border-top: none;
}

.digest-title {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding-right: 8px;
  font-size: 14px;
}

.digest-title-spacer {
  flex: 1;
}

.digest-source {
  font-size: 13px;
  gap: 2px;
}

.progress-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  font-size: 13px;
  color: var(--app-text-muted);
  margin-bottom: 8px;
}

.statement-switch {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.digest-source {
  color: var(--app-primary-strong);
}
:deep(.sd-interim-row td) {
  background: var(--app-surface-muted);
}
:deep(.sd-currency-outlier) {
  color: var(--app-warning-text);
}
</style>
