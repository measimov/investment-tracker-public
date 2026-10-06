<script setup lang="ts">
import { showApiError } from '@/utils/showApiError'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { computed, h, ref, reactive, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { NAlert, NButton, NDataTable, NEmpty, NSpin, NTag, type DataTableColumns } from 'naive-ui'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { holdingsLink } from '@/utils/securities'
import { ElMessage } from 'element-plus'
import api from '@/api'
import DividendReceiptDialog from './DividendReceiptDialog.vue'
import type { BrokerAccount, DividendSuggestion } from '@/types'
import { formatCurrency, formatDate, formatQuantity, toNumber } from '@/utils/helpers'
import { pollJobUntilDone } from '@/utils/polling'
import {
  type AccountListStatus,
  accountLabel,
  accountOptionLabel,
  actionTypeLabel,
  actionTypeTag,
  UNASSIGNED_ACCOUNT_LABEL
} from '@/utils/labels'
import { useAliveGuard } from '@/composables/useAliveGuard'
import { useLatestRequest } from '@/composables/useLatestRequest'
import {
  dividendReviewReason,
  hkDividendNotes,
  suggestionSourceLabel,
  type HkAnnouncementDetail
} from './shared'

// 后端 schema 为准（此前手写副本把 status 枚举放宽为 string）
type SuggestionRow = DividendSuggestion

const props = defineProps<{
  brokerAccounts: BrokerAccount[]
  brokerAccountsStatus: AccountListStatus
  active: boolean
}>()

// counts-changed：待处理徽标挂在壳层 tab 标签上，由壳层重取；
// accepted：接受入账产生了正式公司行动记录，壳层要刷新记录 tab
const emit = defineEmits<{ 'counts-changed': []; accepted: [] }>()

const suggestions = ref<SuggestionRow[]>([])
const suggestionsLoading = ref(false)
const suggestionsHasLoaded = ref(false)
const suggestionsError = ref('')
const suggestionsRequest = useLatestRequest()
// 默认展示全部待处理预计（包括部分到账），状态由服务器在分页前筛选。
const suggestionStatusFilter = ref('PENDING')
const receiptReview = ref<SuggestionRow | null>(null)
async function receiptsSaved() {
  await loadSuggestions()
  emit('counts-changed')
}
const SUGGESTION_LIMIT = 200
const { isUnmounted } = useAliveGuard()
const syncing = ref(false)
const accepting = ref(false)

const acceptDialog = reactive<{
  visible: boolean
  row: SuggestionRow | null
  brokerAccountId: number | null
  totalDividend: number | null
  taxWithheld: number | null
}>({ visible: false, row: null, brokerAccountId: null, totalDividend: null, taxWithheld: null })

function brokerAccountLabelById(accountId: number | null | undefined) {
  return accountLabel(props.brokerAccounts, accountId, { status: props.brokerAccountsStatus })
}

function receiptStateLabel(state: string) {
  return (
    (
      {
        ANNOUNCED: '预计分红·资格待定',
        PENDING: '等待到账',
        PARTIAL: '已有实收·待核对收齐',
        RECEIVED: '已收齐',
        NEEDS_REVIEW: '待核对',
        INACTIVE: '已忽略或被修订'
      } as Record<string, string>
    )[state] || '待核对'
  )
}

function suggestionStatusLabel(status: string) {
  return (
    (
      { NEW: '新建议', MATCHED: '已匹配', ACCEPTED: '已入账', IGNORED: '已忽略' } as Record<
        string,
        string
      >
    )[status] || status
  )
}

function hkNotes(row: SuggestionRow): string[] {
  return hkDividendNotes(row.announcement_detail as HkAnnouncementDetail | null | undefined)
}

function isHkRow(row: SuggestionRow | null | undefined): boolean {
  return row?.source === 'hkexnews-dividend'
}

function suggestionStatusTag(status: string) {
  if (status === 'NEW') return 'primary'
  if (status === 'ACCEPTED') return 'success'
  if (status === 'IGNORED') return 'info'
  return 'info'
}

async function loadSuggestions() {
  const token = suggestionsRequest.begin()
  suggestionsLoading.value = true
  suggestionsError.value = ''
  try {
    const params: Record<string, unknown> = {
      limit: SUGGESTION_LIMIT,
      pending_only: suggestionStatusFilter.value === 'PENDING'
    }
    if (!['ALL', 'PENDING'].includes(suggestionStatusFilter.value))
      params.status = suggestionStatusFilter.value
    const response = await api.listDividendSuggestions(params)
    if (suggestionsRequest.isCurrent(token)) {
      suggestions.value = response.data
      suggestionsHasLoaded.value = true
    }
  } catch (error) {
    if (suggestionsRequest.isCurrent(token)) {
      suggestionsError.value = getApiErrorMessage(error, '加载分红建议失败')
      showApiError(error, '加载分红建议失败')
    }
  } finally {
    if (suggestionsRequest.isCurrent(token)) suggestionsLoading.value = false
  }
}

async function syncDividends() {
  syncing.value = true
  try {
    const startResponse = await api.startDividendSyncJob()
    const job = await pollJobUntilDone(() => api.getDividendSyncJob(startResponse.data.id), {
      intervalMs: 2000,
      maxAttempts: 900,
      isCancelled: isUnmounted,
      failureMessage: '分红公告同步失败',
      timeoutMessage: '分红公告同步仍在后台运行，请稍后刷新公司行动页查看'
    })
    // 取消/卸载即收手：pollJobUntilDone 被 isCancelled 中止时返回 null，
    // 若继续落到下面的列表刷新，会产生卸载后的请求与状态写入，失败时还会
    // 在别的页面弹迟到错误（PR #171 复审）。
    if (!job || isUnmounted()) return
    const result = (job.result || {}) as Record<string, unknown>
    const count = (value: unknown) => (Array.isArray(value) ? value.length : 0)
    const failed = count(result.failed)
    const unparsed = count(result.hk_unparsed_forms)
    const pending = count(result.hk_pending)
    const blocked = count(result.hk_blocked)
    const skippedNoTushare = Number(result.skipped_no_tushare || 0)
    const warnings = [
      failed ? `${failed} 只标的失败` : '',
      unparsed ? `${unparsed} 份港股公告未能识别` : '',
      blocked ? `${blocked} 处港股股息因最新公告无法识别而暂停更新（沿用现有建议，未改写）` : '',
      skippedNoTushare ? `行情来源需管理员配置，已跳过 ${skippedNoTushare} 只 A/B 股` : ''
    ].filter(Boolean)
    const notes = [...warnings, pending ? `${pending} 笔港股股息金额/除净日有待公布` : ''].filter(
      Boolean
    )
    const message =
      `同步完成：扫描 ${result.symbols_scanned ?? 0} 只标的，新建议 ${result.new ?? 0} 条、` +
      `已匹配 ${result.matched ?? 0} 条、事件 ${result.events_upserted ?? 0} 条` +
      (notes.length ? `；${notes.join('；')}` : '')
    if (warnings.length) ElMessage.warning(message)
    else ElMessage.success(message)
    await loadSuggestions()
    emit('counts-changed')
  } catch (error) {
    if (!isUnmounted()) showApiError(error, '分红公告同步失败')
  } finally {
    if (!isUnmounted()) syncing.value = false
  }
}

function openAcceptDialog(row: SuggestionRow) {
  if (row.action_type === 'CASH_DIVIDEND') return
  acceptDialog.row = row
  // 默认取建议行自身的账户归属（每账户一条建议）；NULL=未指定/合并口径
  acceptDialog.brokerAccountId = row.broker_account_id ?? null
  acceptDialog.totalDividend =
    row.estimated_total_dividend != null ? toNumber(row.estimated_total_dividend) : null
  acceptDialog.taxWithheld = 0
  acceptDialog.visible = true
}

async function submitAccept() {
  if (!acceptDialog.row) return
  accepting.value = true
  try {
    // 始终显式发送 broker_account_id（含 null）：省略该键时后端会沿用建议
    // 原账户，用户"清空账户"的意图会静默丢失。el-select 清空把 model 置为
    // undefined，而 undefined 在 JSON 序列化时被丢键，必须归一化为 null。
    const payload: Record<string, unknown> = {
      broker_account_id:
        typeof acceptDialog.brokerAccountId === 'number' ? acceptDialog.brokerAccountId : null
    }
    if (acceptDialog.row.action_type === 'CASH_DIVIDEND') {
      if (acceptDialog.totalDividend != null) payload.total_dividend = acceptDialog.totalDividend
      if (acceptDialog.taxWithheld != null) payload.tax_withheld = acceptDialog.taxWithheld
    }
    await api.acceptDividendSuggestion(acceptDialog.row.id, payload)
    ElMessage.success('已入账为正式公司行动记录')
    acceptDialog.visible = false
    await loadSuggestions()
    emit('counts-changed')
    emit('accepted')
  } catch (error) {
    showApiError(error, '接受建议失败')
    // 后端可能已在拒绝时把建议转为 MATCHED（迟到入账重判重）：刷新列表
    // 反映真实状态；若该行已不再是 NEW，关闭弹窗防止对旧状态重试。
    const failedId = acceptDialog.row?.id
    await loadSuggestions()
    emit('counts-changed')
    const fresh = suggestions.value.find((row) => row.id === failedId)
    if (!fresh || fresh.status !== 'NEW') acceptDialog.visible = false
  } finally {
    accepting.value = false
  }
}

async function ignoreSuggestion(row: SuggestionRow) {
  try {
    await api.ignoreDividendSuggestion(row.id)
    await loadSuggestions()
    emit('counts-changed')
  } catch (error) {
    showApiError(error, '忽略建议失败')
  }
}

async function restoreSuggestion(row: SuggestionRow) {
  try {
    await api.restoreDividendSuggestion(row.id)
    await loadSuggestions()
    emit('counts-changed')
  } catch (error) {
    showApiError(error, '恢复建议失败')
  }
}

// 首次切到本 tab 才加载（v-show 下组件常驻，卸载即整页离开）
watch(
  () => props.active,
  (active) => {
    if (active && !suggestions.value.length) loadSuggestions()
  }
)

const isMobileView = useMediaQuery('(max-width: 640px)')
const emptyDescription = computed(() =>
  suggestionsLoading.value
    ? '正在加载预计与待收股息'
    : suggestionsError.value
      ? '预计与待收股息暂不可用，请重试'
      : !suggestionsHasLoaded.value
        ? '预计与待收股息尚未加载'
        : '暂无符合当前筛选的分红建议；可同步公告'
)
function naiveTag(
  value: string | undefined
): 'default' | 'warning' | 'error' | 'success' | 'primary' {
  if (value === 'danger') return 'error'
  if (value === 'warning' || value === 'success' || value === 'primary') return value
  return 'default'
}
function suggestionAccount(row: SuggestionRow) {
  return row.broker_account_id
    ? brokerAccountLabelById(row.broker_account_id)
    : row.action_type === 'STOCK_DIVIDEND'
      ? '全部账户'
      : UNASSIGNED_ACCOUNT_LABEL
}
function remainingEstimate(row: SuggestionRow) {
  return row.remaining_estimated_gross != null
    ? formatCurrency(row.remaining_estimated_gross, row.currency)
    : row.receipt_state === 'RECEIVED'
      ? '—'
      : '待核对'
}
function completionExplanation(row: SuggestionRow) {
  const basis =
    row.completion_source === 'statement'
      ? '对账单自动核对完成'
      : row.completion_source === 'manual'
        ? '人工确认收齐'
        : row.review_reason
          ? dividendReviewReason(row.review_reason)
          : ''
  return (
    basis +
    (['statement', 'manual'].includes(row.completion_source || '') && row.completion_date
      ? ` · 末笔到账 ${formatDate(row.completion_date)}`
      : '')
  )
}
function renderCash(row: SuggestionRow) {
  if (row.action_type !== 'CASH_DIVIDEND')
    return h('span', `每股送转 ${formatQuantity(row.stk_div_per_share)}`)
  const notes = hkNotes(row)
  return h('div', { class: 'suggestion-amount' }, [
    h('span', formatCurrency(row.cash_div_pre_tax, row.currency, 4)),
    row.cash_div_pre_tax == null && row.cash_div_after_tax == null
      ? h('small', { class: 'secondary-text' }, row.currency)
      : null,
    row.cash_div_after_tax != null
      ? h(
          'small',
          { class: 'secondary-text' },
          `税后 ${formatCurrency(row.cash_div_after_tax, row.currency, 4)}`
        )
      : null,
    notes.length
      ? h('details', { class: 'announcement-detail' }, [
          h('summary', '公告说明'),
          ...notes.map((note) => h('p', note))
        ])
      : null
  ])
}
function renderReceipt(row: SuggestionRow) {
  if (row.action_type !== 'CASH_DIVIDEND')
    return h(
      NTag,
      { type: naiveTag(suggestionStatusTag(row.status)), size: 'small', bordered: false },
      () => suggestionStatusLabel(row.status)
    )
  return h('div', { class: 'receipt-state' }, [
    h(
      NTag,
      {
        type: row.receipt_state === 'RECEIVED' ? 'success' : 'default',
        size: 'small',
        bordered: false
      },
      () => receiptStateLabel(row.receipt_state || '')
    ),
    completionExplanation(row)
      ? h('p', { class: 'receipt-explanation' }, completionExplanation(row))
      : null,
    row.overdue && Number(row.remaining_estimated_gross) > 0
      ? h(
          NTag,
          { type: 'warning', size: 'small', bordered: false },
          () => '已过预计派息日·仍有待收余额'
        )
      : null
  ])
}
function renderActions(row: SuggestionRow) {
  const nodes = []
  const button = (label: string, click: () => void) =>
    h(
      NButton,
      {
        text: true,
        type: 'primary',
        onClick: click,
        'aria-label': `${label} ${row.symbol} ${formatDate(row.ex_date)}`
      },
      () => label
    )
  if (row.action_type === 'CASH_DIVIDEND' && row.receipt_state !== 'INACTIVE')
    nodes.push(button('核对到账', () => (receiptReview.value = row)))
  if (row.status === 'NEW') {
    if (row.action_type !== 'CASH_DIVIDEND') nodes.push(button('接受', () => openAcceptDialog(row)))
    nodes.push(button('忽略', () => ignoreSuggestion(row)))
  } else if (row.status === 'MATCHED') {
    nodes.push(
      h(
        'small',
        { class: 'accepted-hint' },
        `账本已有匹配记录，无需入账；如有出入请先核对既有记录。${row.action_type === 'CASH_DIVIDEND' ? receiptStateLabel(row.receipt_state || '') : '已在账'}`
      )
    )
    nodes.push(button('忽略', () => ignoreSuggestion(row)))
  } else if (row.status === 'IGNORED') nodes.push(button('恢复', () => restoreSuggestion(row)))
  else
    nodes.push(
      h(
        'small',
        { class: 'accepted-hint' },
        row.action_type === 'CASH_DIVIDEND' ? receiptStateLabel(row.receipt_state || '') : '已入账'
      )
    )
  return h('div', { class: 'suggestion-actions' }, nodes)
}
const CashAmount = (props: { row: SuggestionRow }) => renderCash(props.row)
const ReceiptState = (props: { row: SuggestionRow }) => renderReceipt(props.row)
const SuggestionActions = (props: { row: SuggestionRow }) => renderActions(props.row)
const columns: DataTableColumns<SuggestionRow> = [
  {
    title: '标的',
    key: 'symbol',
    width: 160,
    render: (row) =>
      h('div', { class: 'security-cell' }, [
        h(RouterLink, { to: holdingsLink(row), class: 'symbol-link' }, () => row.symbol),
        h('span', { class: 'security-name' }, row.name || row.market),
        isHkRow(row)
          ? h(
              NTag,
              { size: 'small', bordered: false, 'data-testid': 'suggestion-source-hkex' },
              () => suggestionSourceLabel(row.source)
            )
          : null
      ])
  },
  {
    title: '类型',
    key: 'action_type',
    width: 110,
    render: (row) =>
      h(
        NTag,
        { type: naiveTag(actionTypeTag(row.action_type)), size: 'small', bordered: false },
        () => actionTypeLabel(row.action_type)
      )
  },
  {
    title: '账户',
    key: 'account',
    width: 150,
    render: (row) =>
      h(
        'span',
        { class: !row.broker_account_id ? 'account-unassigned' : '' },
        suggestionAccount(row)
      )
  },
  { title: '除权除息日', key: 'ex_date', width: 110, render: (row) => formatDate(row.ex_date) },
  {
    title: '预计派息日',
    key: 'pay_date',
    width: 110,
    render: (row) => (row.pay_date ? formatDate(row.pay_date) : '—')
  },
  {
    title: '每股金额 / 送转比例',
    key: 'amount',
    width: 160,
    align: 'right',
    cellProps: () => ({ style: { verticalAlign: 'top' } }),
    render: renderCash
  },
  {
    title: '登记日持仓',
    key: 'quantity',
    width: 150,
    align: 'right',
    render: (row) =>
      h('div', [
        h('span', formatQuantity(row.record_date_quantity)),
        row.quantity_basis === 'merged'
          ? h('p', { class: 'receipt-explanation' }, '合并口径：账户归属存在矛盾，数量总和可信')
          : null
      ])
  },
  {
    title: '推算总额（税前）',
    key: 'estimated',
    width: 150,
    align: 'right',
    render: (row) =>
      row.estimated_total_dividend != null
        ? formatCurrency(row.estimated_total_dividend, row.currency)
        : '—'
  },
  {
    title: '剩余预计（税前）',
    key: 'remaining',
    width: 160,
    align: 'right',
    render: remainingEstimate
  },
  { title: '到账核对', key: 'receipt', width: 230, render: renderReceipt },
  {
    title: '已确认到账',
    key: 'received',
    width: 150,
    align: 'right',
    render: (row) =>
      h(
        'div',
        row.receipt_ids?.length
          ? Object.entries(row.received_by_currency || {}).map(([currency, amount]) =>
              h('div', formatCurrency(amount, String(currency)))
            )
          : '—'
      )
  },
  { title: '操作', key: 'actions', width: 230, fixed: 'right', render: renderActions }
]
</script>

<template>
  <section
    class="suggestions-tab"
    aria-labelledby="suggestions-heading"
    :aria-busy="suggestionsLoading"
  >
    <header class="suggestions-heading">
      <h2 id="suggestions-heading">预计与待收股息</h2>
      <div class="header-actions">
        <select
          v-model="suggestionStatusFilter"
          aria-label="筛选股息建议状态"
          @change="loadSuggestions"
        >
          <option value="NEW">待处理（新建议）</option>
          <option value="PENDING">预计与待收</option>
          <option value="ALL">全部公告</option>
          <option value="MATCHED">仅已匹配</option>
          <option value="ACCEPTED">历史已接受</option>
          <option value="IGNORED">已忽略</option>
        </select>
        <NButton
          type="primary"
          :loading="syncing"
          data-testid="dividend-sync-button"
          @click="syncDividends"
          >同步分红公告</NButton
        >
      </div>
    </header>
    <p class="suggestions-note">
      公告用于预计统计，实收按实际到账计入。有对账单明细、能唯一对应本次派息且金额吻合时，系统自动核对收齐；否则说明待核对原因。普通股息不改变买入成本。美股公告暂未自动覆盖。
    </p>
    <NAlert
      v-if="suggestionsError"
      type="error"
      :show-icon="false"
      class="state-alert"
      data-testid="suggestions-load-error"
      >{{
        suggestionsHasLoaded
          ? '预计与待收股息加载失败，保留上次成功结果。当前筛选尚未确认。'
          : '预计与待收股息加载失败，公告与待收状态未知。'
      }}<NButton @click="loadSuggestions">重试建议</NButton></NAlert
    >
    <p v-if="suggestionsLoading" role="status" class="suggestions-note">
      正在加载{{ suggestionsHasLoaded ? '，暂保留上次结果' : '' }}
    </p>
    <NAlert
      v-if="suggestions.length >= SUGGESTION_LIMIT"
      type="info"
      class="state-alert"
      :title="`仅显示最近 ${SUGGESTION_LIMIT} 条建议（按除权除息日倒序）`"
    />
    <NDataTable
      v-if="!isMobileView"
      class="suggestions-table"
      :columns="columns"
      :data="suggestions"
      :loading="suggestionsLoading"
      :row-key="(row) => row.id"
      :scroll-x="1970"
      :bordered="false"
      ><template #empty><NEmpty :description="emptyDescription" /></template
    ></NDataTable>
    <NSpin v-else :show="suggestionsLoading"
      ><div class="mobile-card-list">
        <NEmpty v-if="!suggestions.length" :description="emptyDescription" />
        <article
          v-for="row in suggestions"
          :key="row.id"
          class="mobile-card"
          data-testid="dividend-suggestion-card"
        >
          <div class="mobile-card-head">
            <div class="mobile-card-title">
              <RouterLink :to="holdingsLink(row)" class="mobile-card-symbol">{{
                row.symbol
              }}</RouterLink
              ><span class="mobile-card-name">{{ row.name || row.market }}</span>
            </div>
            <NTag :type="naiveTag(actionTypeTag(row.action_type))" size="small" :bordered="false">{{
              actionTypeLabel(row.action_type)
            }}</NTag>
          </div>
          <NTag
            v-if="isHkRow(row)"
            size="small"
            :bordered="false"
            data-testid="suggestion-source-hkex"
            >{{ suggestionSourceLabel(row.source) }}</NTag
          >
          <div class="mobile-card-meta">
            <span :class="{ 'account-unassigned': !row.broker_account_id }">{{
              suggestionAccount(row)
            }}</span
            ><span>除权除息日 {{ formatDate(row.ex_date) }}</span
            ><span>预计派息日 {{ formatDate(row.pay_date) }}</span
            ><span>{{ row.currency }}</span>
          </div>
          <dl class="suggestion-facts">
            <div>
              <dt>
                {{ row.action_type === 'CASH_DIVIDEND' ? '每股税前 / 税后' : '每股送转比例' }}
              </dt>
              <dd><CashAmount :row="row" /></dd>
            </div>
            <div>
              <dt>登记日持仓</dt>
              <dd>
                {{ formatQuantity(row.record_date_quantity)
                }}<small v-if="row.quantity_basis === 'merged'"
                  >合并口径：账户归属存在矛盾，数量总和可信</small
                >
              </dd>
            </div>
            <div>
              <dt>推算总额（税前）</dt>
              <dd>
                {{
                  row.estimated_total_dividend != null
                    ? formatCurrency(row.estimated_total_dividend, row.currency)
                    : '—'
                }}
              </dd>
            </div>
            <div>
              <dt>剩余预计（税前）</dt>
              <dd>{{ remainingEstimate(row) }}</dd>
            </div>
            <div>
              <dt>已确认到账</dt>
              <dd>
                <span v-for="(amount, currency) in row.received_by_currency" :key="currency">{{
                  formatCurrency(amount, String(currency))
                }}</span
                ><span v-if="!row.receipt_ids?.length">—</span>
              </dd>
            </div>
          </dl>
          <ReceiptState :row="row" />
          <div class="mobile-card-actions"><SuggestionActions :row="row" /></div>
        </article></div
    ></NSpin>
    <DividendReceiptDialog
      :row="receiptReview"
      @close="receiptReview = null"
      @saved="receiptsSaved"
    />
    <!-- 接受建议：账户归属与税额可改 -->
    <el-dialog v-model="acceptDialog.visible" title="接受分红建议" width="min(480px, 94vw)">
      <el-form label-width="110px">
        <el-form-item label="标的">
          <span>
            {{ acceptDialog.row?.symbol }} {{ acceptDialog.row?.name || '' }} （{{
              actionTypeLabel(acceptDialog.row?.action_type || '')
            }}）
          </span>
        </el-form-item>
        <el-form-item label="账户">
          <el-select
            v-model="acceptDialog.brokerAccountId"
            placeholder="可选；按实际到账账户归属"
            clearable
          >
            <el-option
              v-for="account in brokerAccounts"
              :key="account.id"
              :label="accountOptionLabel(account)"
              :value="account.id"
            />
          </el-select>
        </el-form-item>
        <template v-if="acceptDialog.row?.action_type === 'CASH_DIVIDEND'">
          <el-form-item v-if="acceptDialog.row?.currency !== 'CNY'" label="币种">
            <span>{{ acceptDialog.row?.currency }}</span>
          </el-form-item>
          <el-form-item label="股息总额">
            <el-input-number
              v-model="acceptDialog.totalDividend"
              :min="0"
              :precision="2"
              :controls="false"
              class="amount-input"
            />
          </el-form-item>
          <el-form-item label="预扣税额">
            <el-input-number
              v-model="acceptDialog.taxWithheld"
              :min="0"
              :precision="2"
              :controls="false"
              class="amount-input"
            />
            <div v-if="isHkRow(acceptDialog.row)" class="field-hint">
              港股按公告派发币种与除净日前一天持仓推算税前总额；预扣税视持有渠道而定（H 股/红筹经
              HKSCC 代理人常按 10%、港股通个人 20%），请按券商实际到账填写
            </div>
            <div v-else class="field-hint">A股券商到账通常为税前全额，税额保持 0 即可</div>
          </el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="acceptDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="accepting" @click="submitAccept">确认入账</el-button>
      </template>
    </el-dialog>
  </section>
</template>
<style scoped>
.suggestions-tab {
  width: 100%;
  min-width: 0;
}
.suggestions-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin: 24px 0;
}
.suggestions-heading h2 {
  font-size: 16px;
  font-weight: 500;
  margin: 0;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.header-actions select {
  height: 36px;
  width: 200px;
  border: 1px solid var(--app-border);
  border-radius: 3px;
  background: var(--app-surface);
  color: var(--app-text);
  padding: 0 10px;
  font: inherit;
  font-size: 14px;
}
.header-actions select:focus-visible {
  outline: 2px solid var(--app-primary);
  outline-offset: 2px;
}
.suggestions-note {
  font-size: 13px;
  color: var(--app-text-muted);
  line-height: 1.8;
  margin: 16px 0;
}
.state-alert {
  margin: 12px 0;
}
.state-alert .n-button {
  margin-left: 12px;
}
.suggestions-table :deep(.security-cell) {
  display: grid;
  gap: 6px;
}
.suggestions-table :deep(.security-name) {
  font-size: 12px;
  color: var(--app-text-muted);
  overflow-wrap: anywhere;
}
.suggestions-table :deep(.symbol-link),
.mobile-card-symbol {
  color: var(--app-primary-strong);
  font-weight: 600;
  text-decoration: none;
}
.suggestions-table :deep(.symbol-link:hover),
.mobile-card-symbol:hover {
  text-decoration: underline;
}
.suggestions-table :deep(.account-unassigned),
.account-unassigned {
  color: var(--app-text-muted);
}
.suggestions-tab :deep(.suggestion-amount) {
  display: grid;
  gap: 4px;
  overflow-wrap: anywhere;
}
.suggestions-tab :deep(.secondary-text),
.suggestions-tab :deep(.receipt-explanation),
.suggestions-tab :deep(.accepted-hint) {
  font-size: 12px;
  color: var(--app-text-muted);
  line-height: 1.8;
  overflow-wrap: anywhere;
}
.suggestions-tab :deep(.receipt-explanation) {
  margin: 6px 0;
}
.suggestions-tab :deep(.receipt-state) {
  display: grid;
  justify-items: start;
  gap: 6px;
}
.suggestions-tab :deep(.suggestion-actions) {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}
.suggestions-tab :deep(.suggestion-actions .n-button) {
  min-height: 24px;
}
.suggestions-tab :deep(.accepted-hint) {
  flex-basis: 100%;
}
.suggestions-tab :deep(.announcement-detail) {
  text-align: left;
  font-size: 12px;
  line-height: 1.8;
  color: var(--app-text-muted);
}
.suggestions-tab :deep(.announcement-detail summary) {
  align-content: center;
  cursor: pointer;
  min-height: 24px;
  color: var(--app-primary-strong);
}
.suggestions-tab :deep(.announcement-detail p) {
  white-space: normal;
  overflow-wrap: anywhere;
}
.amount-input {
  width: 100%;
}
.field-hint {
  font-size: 12px;
  color: var(--app-text-muted);
  line-height: 1.8;
}
@media (max-width: 640px) {
  .suggestions-heading {
    align-items: flex-start;
    flex-wrap: wrap;
  }
  .header-actions {
    width: 100%;
  }
  .header-actions select {
    min-height: 44px;
    flex: 1;
    min-width: 160px;
  }
  .header-actions .n-button {
    min-height: 44px;
  }
  .mobile-card {
    background: var(--app-surface);
    border: 1px solid var(--app-border-soft);
    border-radius: 3px;
    box-shadow: none;
  }
  .mobile-card-title {
    min-width: 0;
    flex: 1;
  }
  .mobile-card-name {
    white-space: normal;
    overflow-wrap: anywhere;
  }
  .mobile-card-meta {
    line-height: 1.8;
    margin: 12px 0;
  }
  .suggestion-facts {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px 12px;
    margin: 18px 0;
  }
  .suggestion-facts div {
    min-width: 0;
  }
  .suggestion-facts dt {
    font-size: 12px;
    color: var(--app-text-muted);
    line-height: 1.8;
  }
  .suggestion-facts dd {
    margin: 4px 0 0;
    line-height: 1.8;
    overflow-wrap: anywhere;
  }
  .suggestion-facts small,
  .suggestion-facts dd > span {
    display: block;
  }
  .suggestions-tab :deep(.suggestion-actions .n-button),
  .suggestions-tab :deep(.announcement-detail summary) {
    min-height: 44px;
  }
  .mobile-card-actions {
    margin-top: 14px;
    justify-content: flex-start;
  }
}
</style>
