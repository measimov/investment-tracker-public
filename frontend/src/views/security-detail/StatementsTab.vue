<script setup lang="ts">
/**
 * 「报表」tab：财报摘要（LLM 分档摘要）、港股报表抽取进度、美/港股核心科目透视表、
 * A股利润与现金流摘要。
 */
import { computed, ref } from 'vue'
import { InfoFilled, Link } from '@element-plus/icons-vue'
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

function pivotRowClass({ row }: { row: ProfileRow }): string {
  return row.fp === 'H1' ? 'sd-interim-row' : ''
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
</script>

<template>
  <!-- 财报摘要 -->
  <section
    v-if="state.capabilities.report_digest"
    class="sd-block"
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
      <el-button
        size="small"
        plain
        :loading="state.backfilling"
        data-testid="backfill-digests-button"
        @click="emit('backfill')"
      >
        补齐历史摘要
      </el-button>
    </div>
    <el-alert
      v-if="backfillSummary"
      :title="backfillSummary"
      type="info"
      :closable="false"
      class="block-alert"
    />
    <el-collapse v-if="state.reportDigests.length" class="digest-collapse">
      <el-collapse-item
        v-for="digest in state.reportDigests"
        :key="digest.period_key"
        :name="digest.period_key"
      >
        <template #title>
          <div class="digest-title">
            <span class="sd-num">{{ formatPeriod(digest.end_date) }}</span>
            <el-tag size="small" effect="plain">{{ digestTypeLabel(digest.report_type) }}</el-tag>
            <el-tooltip
              v-if="digest.digest_tier === 'C'"
              content="较早年份只摘要核心三项（主营收入结构 / 一次性项目 / 会计信号）与关键数字"
              placement="top"
            >
              <el-tag size="small" type="info" effect="plain">精简摘要</el-tag>
            </el-tooltip>
            <span class="digest-title-spacer" />
            <el-link
              v-if="digestSourceHref(digest.source_url)"
              :href="digestSourceHref(digest.source_url)!"
              target="_blank"
              rel="noopener noreferrer"
              type="primary"
              :underline="false"
              class="digest-source"
              @click.stop
            >
              <el-icon><Link /></el-icon>原文
            </el-link>
          </div>
        </template>
        <dl class="sd-field-list">
          <template v-for="field in digestFields(digest)" :key="field">
            <dt>{{ field }}</dt>
            <dd>{{ digest.digest[field] }}</dd>
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
    <el-empty
      v-else
      description="暂无财报摘要；点击「补齐历史摘要」抓取年报并生成（每次最多 4 份，可重复点击续跑）"
      :image-size="56"
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
      <el-link
        v-if="failedReports.length"
        type="primary"
        :underline="false"
        @click="showFailedReports = !showFailedReports"
      >
        {{ showFailedReports ? '收起失败清单' : '查看失败清单' }}
      </el-link>
    </div>
    <el-table v-if="showFailedReports && failedReports.length" :data="failedReports" size="small">
      <el-table-column label="报告" width="160">
        <template #default="{ row }">
          {{ formatPeriod(row.end_date) }}
          {{ row.report_type === 'interim' ? '中报' : '年报' }}
        </template>
      </el-table-column>
      <el-table-column label="状态" width="120">
        <template #default="{ row }">
          <el-tag :type="row.capped ? 'danger' : 'warning'" size="small" effect="plain">
            {{ row.capped ? '已封顶' : `可重试（${row.attempts} 次）` }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="原因" min-width="200">
        <template #default="{ row }">{{ row.error || EMPTY }}</template>
      </el-table-column>
    </el-table>
  </section>

  <!-- 核心科目透视（美股 EDGAR / 港股 PDF 抽取 + 雅虎补缺） -->
  <section v-if="!isA" class="sd-block" data-testid="pivot-statements-section">
    <div class="sd-block-header">
      <h3 class="sd-block-title">
        {{ showInterim ? '核心科目（年报 + 中报）' : '年度核心科目' }}
        <span class="sd-title-note" data-testid="pivot-unit">{{ pivotUnitText }}</span>
        <el-tooltip :content="pivotSourceHelp" placement="top">
          <el-icon class="sd-help"><InfoFilled /></el-icon>
        </el-tooltip>
      </h3>
      <el-switch
        v-if="isHk"
        v-model="showInterim"
        size="small"
        active-text="显示中报"
        data-testid="pivot-show-interim"
      />
    </div>
    <el-table :data="pivotRows" size="small" :stripe="!showInterim" :row-class-name="pivotRowClass">
      <template #empty>
        <el-empty description="暂无数据；生成分析时会自动同步" :image-size="56" />
      </template>
      <el-table-column :label="showInterim ? '期末' : '财年止'" width="122">
        <template #default="{ row }">
          <span class="sd-num">{{ formatPeriod(row.end_date) }}</span>
          <el-tooltip v-if="row.fp === 'H1'" content="中报：六个月数，不可与年度数直接比较">
            <span class="sd-period-badge">6M</span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="币种" width="84">
        <template #default="{ row }">
          <span :class="{ 'sd-currency-outlier': isCurrencyOutlier(row, currencySummary) }">
            {{ row.currency || EMPTY }}
          </span>
          <el-tooltip
            v-if="currencySwitchText(pivotRows, row)"
            :content="currencySwitchText(pivotRows, row)"
          >
            <span class="sd-period-badge" data-testid="pivot-currency-switch">换币</span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column
        v-for="column in PIVOT_COLUMNS"
        :key="column.field"
        :label="column.label"
        align="right"
        min-width="92"
      >
        <template #default="{ row }">
          <el-tooltip v-if="scrubbed(row, column.field)" content="校验存疑，已置空" placement="top">
            <span class="sd-scrubbed">✕</span>
          </el-tooltip>
          <template v-else
            >{{ formatYi(row[column.field])
            }}<sup class="sd-src-sup">{{ cellSup(row, column.field) }}</sup></template
          >
        </template>
      </el-table-column>
      <el-table-column align="right" min-width="76">
        <template #header>
          EPS
          <el-tooltip
            v-if="isHk"
            content="每股金额（原币元，不按亿换算）。原文以「仙」列示的期别已 ÷100 折元（带「折」上标，悬停看依据）"
            placement="top"
          >
            <el-icon class="sd-help"><InfoFilled /></el-icon>
          </el-tooltip>
        </template>
        <template #default="{ row }">
          <el-tooltip v-if="scrubbed(row, 'basic_eps')" content="校验存疑，已置空" placement="top">
            <span class="sd-scrubbed">✕</span>
          </el-tooltip>
          <el-tooltip v-else-if="epsNote(row)" :content="epsNote(row)" placement="top">
            <span>{{ formatPerShare(row.basic_eps) }}<sup class="sd-src-sup">折</sup></span>
          </el-tooltip>
          <template v-else
            >{{ formatEps(row.basic_eps)
            }}<sup class="sd-src-sup">{{ cellSup(row, 'basic_eps') }}</sup></template
          >
        </template>
      </el-table-column>
      <el-table-column v-if="isHk" label="来源" min-width="140">
        <template #default="{ row }">
          <span>{{ row.__sourceKinds ? sourceLabel(row as HkPivotRow) : '' }}</span>
          <el-tooltip
            v-if="row.__suspect"
            :content="suspectTooltipText(row as HkPivotRow)"
            placement="top"
          >
            <el-tag type="warning" size="small" effect="plain" class="suspect-tag">存疑</el-tag>
          </el-tooltip>
          <el-tooltip v-if="notesTagLabel(row)" :content="notesText(row)" placement="top">
            <el-tag type="info" size="small" effect="plain" class="suspect-tag">{{
              notesTagLabel(row)
            }}</el-tag>
          </el-tooltip>
        </template>
      </el-table-column>
    </el-table>
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
        <el-tooltip
          content="一季报/中报/三季报为年初至今累计值，与年报混排时注意期别"
          placement="top"
        >
          <el-icon class="sd-help"><InfoFilled /></el-icon>
        </el-tooltip>
      </h3>
      <el-switch v-model="statementAnnualOnly" size="small" active-text="只看年报" />
    </div>
    <el-table :data="statementRows" size="small" stripe>
      <template #empty>
        <el-empty description="暂无数据；生成分析时会自动同步" :image-size="56" />
      </template>
      <el-table-column label="报告期" width="104">
        <template #default="{ row }">{{ formatPeriod(row.end_date) }}</template>
      </el-table-column>
      <el-table-column label="期别" width="70">
        <template #default="{ row }">{{ periodKindLabel(row.end_date) }}</template>
      </el-table-column>
      <el-table-column label="营业总收入" align="right" min-width="92">
        <template #default="{ row }">{{ formatYi(row.total_revenue) }}</template>
      </el-table-column>
      <el-table-column label="营业利润" align="right" min-width="84">
        <template #default="{ row }">{{ formatYi(row.operate_profit) }}</template>
      </el-table-column>
      <el-table-column label="归母净利润" align="right" min-width="92">
        <template #default="{ row }">{{ formatYi(row.n_income_attr_p ?? row.n_income) }}</template>
      </el-table-column>
      <el-table-column label="经营现金流净额" align="right" min-width="110">
        <template #default="{ row }">{{ formatYi(row.n_cashflow_act) }}</template>
      </el-table-column>
      <el-table-column label="投资现金流净额" align="right" min-width="110">
        <template #default="{ row }">{{ formatYi(row.n_cashflow_inv_act) }}</template>
      </el-table-column>
      <el-table-column label="总资产" align="right" min-width="84">
        <template #default="{ row }">{{ formatYi(row.total_assets) }}</template>
      </el-table-column>
      <el-table-column label="净资产" align="right" min-width="84">
        <template #default="{ row }">{{ formatYi(row.total_hldr_eqy_exc_min_int) }}</template>
      </el-table-column>
    </el-table>
  </section>
</template>

<style scoped>
.block-alert {
  margin-bottom: 10px;
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

.suspect-tag {
  margin-left: 6px;
}
</style>
