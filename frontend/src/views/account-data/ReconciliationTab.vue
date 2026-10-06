<script setup lang="ts">
import type { ReconciliationSnapshotCreate } from '@/types'
import { LEDGER_CURRENCIES } from '@/utils/currency'
import { type AccountListStatus, accountLabel, accountOptionLabel } from '@/utils/labels'
import { makeConfirmedAction } from '@/composables/useConfirmAction'
import { showApiError } from '@/utils/showApiError'
import { computed, h, nextTick, reactive, ref } from 'vue'
import { NAlert, NButton, NDataTable, NEmpty, NSpin, NTag, type DataTableColumns } from 'naive-ui'
import { ElMessage, type FormInstance } from 'element-plus'
import { Plus } from '@lucide/vue'
import api from '@/api'
import { isLongNote, renderNote } from './shared'
import SecuritySelect from '@/components/SecuritySelect.vue'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { EMPTY, formatCurrency, formatDate, formatDateTime, formatQuantity } from '@/utils/helpers'
import {
  type AccountRow,
  type DialogState,
  type SnapshotRow,
  LIST_LIMIT,
  isAtListLimit,
  marketOptions,
  monthEnd,
  signedDelta,
  statementScopeLabel
} from './shared'

const props = defineProps<{
  snapshots: SnapshotRow[]
  accounts: AccountRow[]
  accountsStatus: AccountListStatus
  loading: boolean
  hasLoaded: boolean
  loadError: boolean
  reload: () => Promise<unknown>
}>()

const isMobileView = useMediaQuery('(max-width: 640px)')
const accountLabelOf = (id: unknown) =>
  accountLabel(props.accounts, id, { status: props.accountsStatus })

interface SnapshotCashRowInput {
  currency: string
  amount: number
}

interface SnapshotPositionRowInput {
  symbol: string
  market: string
  quantity: number
  currency?: string | null
  [key: string]: unknown
}

const snapshotDialog = reactive<DialogState>({ visible: false, id: null, saving: false })
const snapshotFormRef = ref<FormInstance>()
const snapshotForm = reactive<{
  broker_account_id?: number | null
  snapshot_date?: string
  source_filename?: string
  cashRows: SnapshotCashRowInput[]
  positionRows: SnapshotPositionRowInput[]
  notes?: string
}>({ cashRows: [], positionRows: [] })

const snapshotRules = {
  broker_account_id: [{ required: true, message: '请选择账户', trigger: 'change' }],
  snapshot_date: [{ required: true, message: '请选择日期', trigger: 'change' }]
}

const diffDialog = reactive<{ visible: boolean; row: SnapshotRow | null }>({
  visible: false,
  row: null
})
const comparingSnapshotId = ref<number | null>(null)

const DIFF_ITEM_LABELS: Record<string, string> = {
  MATCH: '一致',
  QUANTITY_MISMATCH: '数量差',
  MISSING_IN_SYSTEM: '系统缺记录',
  MISSING_IN_SNAPSHOT: '快照缺记录'
}

const diffItemLabel = (status: string) => DIFF_ITEM_LABELS[status] || status
const diffItemTag = (status: string) => (status === 'MATCH' ? 'success' : 'danger')

// 排除清单内的现金管理标的双侧忽略（summary.excluded_symbols），需在弹窗里说清楚
const excludedSymbols = (row: SnapshotRow | null) => {
  const items = row?.diff_detail?.summary?.excluded_symbols
  return Array.isArray(items) ? (items as Array<{ symbol?: string; market?: string }>) : []
}

function openDiffDialog(row: SnapshotRow) {
  diffDialog.row = row
  diffDialog.visible = true
}

async function compareSnapshot(row: SnapshotRow) {
  comparingSnapshotId.value = row.id
  try {
    const response = await api.compareReconciliationSnapshot(row.id)
    if (response.data.status === 'MISMATCHED') ElMessage.warning('比对发现差异，点击状态查看明细')
    else if (response.data.status === 'MATCHED')
      ElMessage.success(snapshotStatusLabel(response.data))
    else ElMessage.info(snapshotStatusLabel(response.data))
    await props.reload()
  } catch (error) {
    showApiError(error, '比对失败')
  } finally {
    comparingSnapshotId.value = null
  }
}

function resetSnapshotForm(row: Partial<SnapshotRow> = {}) {
  const cashBalances = row.cash_balances ?? {}
  const positions = row.positions ?? []
  Object.assign(snapshotForm, {
    broker_account_id: row.broker_account_id || props.accounts[0]?.id || null,
    snapshot_date: (row.snapshot_date || monthEnd()).slice(0, 10),
    source_filename: row.source_filename || '',
    cashRows: Object.entries(cashBalances).length
      ? Object.entries(cashBalances).map(([currency, amount]) => ({
          currency,
          amount: Number(amount)
        }))
      : [{ currency: 'CNY', amount: 0 }],
    positionRows: positions.map((item) => ({
      ...item,
      quantity: Number(item.quantity)
    })),
    notes: row.notes || ''
  })
}

function addCashRow() {
  const used = new Set(snapshotForm.cashRows.map((item) => item.currency))
  const currency = LEDGER_CURRENCIES.find((item) => !used.has(item)) || 'CNY'
  snapshotForm.cashRows.push({ currency, amount: 0 })
}

function addPositionRow() {
  snapshotForm.positionRows.push({
    symbol: '',
    market: '',
    quantity: 0,
    currency: null
  })
}

function openSnapshotDialog(row?: SnapshotRow) {
  snapshotDialog.id = row?.id || null
  resetSnapshotForm(row)
  snapshotDialog.visible = true
  nextTick(() => snapshotFormRef.value?.clearValidate())
}

async function saveSnapshot() {
  if (!(await snapshotFormRef.value?.validate().catch(() => false))) return
  const cashRows = snapshotForm.cashRows.filter((item) => item.currency)
  if (new Set(cashRows.map((item) => item.currency)).size !== cashRows.length) {
    ElMessage.warning('同一币种只能填写一次现金余额')
    return
  }
  const incompletePosition = snapshotForm.positionRows.some(
    (item) => (item.symbol || item.market) && !(item.symbol && item.market)
  )
  if (incompletePosition) {
    ElMessage.warning('请补全持仓的标的代码和市场，或移除该行')
    return
  }
  snapshotDialog.saving = true
  try {
    const payload = {
      broker_account_id: snapshotForm.broker_account_id,
      snapshot_date: snapshotForm.snapshot_date,
      source_filename: snapshotForm.source_filename || null,
      cash_balances: Object.fromEntries(
        cashRows.map((item) => [item.currency, Number(item.amount || 0)])
      ),
      positions: snapshotForm.positionRows
        .filter((item) => item.symbol && item.market)
        .map((item) => ({
          symbol: item.symbol.trim(),
          market: item.market,
          quantity: Number(item.quantity || 0),
          currency: item.currency || null
        })),
      notes: snapshotForm.notes
    }
    // 表单校验保证账户与日期必填：已校验表单 → 请求体
    const body = payload as ReconciliationSnapshotCreate
    if (snapshotDialog.id) await api.updateReconciliationSnapshot(snapshotDialog.id, body)
    else await api.createReconciliationSnapshot(body)
    ElMessage.success(snapshotDialog.id ? '核对记录已更新' : '核对记录已新增')
    snapshotDialog.visible = false
    await props.reload()
  } catch (error) {
    showApiError(error, '核对记录保存失败')
  } finally {
    snapshotDialog.saving = false
  }
}

const removeSnapshot = makeConfirmedAction<SnapshotRow>({
  title: '删除核对记录',
  confirmText: '删除',
  message: '确认删除这条月末核对记录？',
  request: (row) => api.deleteReconciliationSnapshot(row.id),
  successMessage: '核对记录已删除',
  failureMessage: '核对记录删除失败',
  reload: () => props.reload()
})

const snapshotStatusLabel = (row: SnapshotRow | null | undefined) => {
  const status = String(row?.status || '').toUpperCase()
  // 分范围对账单不比对现金，绿色语义限定为"持仓一致"，避免误读为整体对账完成
  if (status === 'MATCHED' && row?.statement_scope) return '持仓一致'
  return (
    (
      {
        PENDING: '待比对',
        MATCHED: '比对一致',
        MISMATCHED: '有差异'
      } as Record<string, string>
    )[status] ||
    row?.status ||
    '待比对'
  )
}
const snapshotStatusTag = (status: string | undefined) => {
  const value = String(status).toUpperCase()
  if (value === 'MATCHED') return 'success'
  if (value === 'MISMATCHED') return 'danger'
  return 'warning'
}

const jsonSummary = (value: Record<string, string> | null | undefined) => {
  const entries = Object.entries(value ?? {})
  if (!entries.length) return EMPTY
  return entries.map(([currency, amount]) => formatCurrency(amount, currency)).join(' · ')
}
const positionSummary = (value: readonly unknown[] | null | undefined) =>
  value?.length ? `${value.length} 只标的` : EMPTY

const emptyDescription = computed(() =>
  props.loadError
    ? '尚未确认月末核对记录，请重试'
    : !props.hasLoaded
      ? '月末核对记录正在加载'
      : '暂无月末核对记录'
)
const statusType = (status: string | undefined) => {
  const type = snapshotStatusTag(status)
  return type === 'danger' ? 'error' : type
}
const statusContent = (row: SnapshotRow) =>
  h(NTag, { size: 'small', bordered: false, type: statusType(row.status) }, () =>
    snapshotStatusLabel(row)
  )
const statusCell = (row: SnapshotRow) =>
  row.diff_detail
    ? h(
        'button',
        {
          type: 'button',
          class: 'diff-status-button',
          'aria-label': `${snapshotStatusLabel(row)}，查看差异明细`,
          onClick: () => openDiffDialog(row)
        },
        [statusContent(row)]
      )
    : statusContent(row)
const columns: DataTableColumns<SnapshotRow> = [
  {
    title: '核对日期',
    key: 'snapshot_date',
    width: 130,
    render: (row) => formatDate(row.snapshot_date)
  },
  {
    title: '账户',
    key: 'broker_account_id',
    width: 190,
    render: (row) => accountLabelOf(row.broker_account_id)
  },
  {
    title: '范围',
    key: 'statement_scope',
    width: 105,
    render: (row) => statementScopeLabel(row.statement_scope)
  },
  { title: '状态', key: 'status', width: 145, render: statusCell },
  {
    title: '现金摘要',
    key: 'cash_balances',
    width: 190,
    render: (row) => jsonSummary(row.cash_balances)
  },
  {
    title: '持仓摘要',
    key: 'positions',
    width: 120,
    render: (row) => positionSummary(row.positions)
  },
  {
    title: '来源文件',
    key: 'source_filename',
    width: 210,
    render: (row) => row.source_filename || EMPTY
  },
  {
    title: '备注',
    key: 'notes',
    width: 210,
    cellProps: () => ({ style: { verticalAlign: 'top' } }),
    render: (row) => renderNote(row.notes)
  },
  {
    title: '操作',
    key: 'actions',
    width: 240,
    fixed: 'right',
    render: (row) =>
      h('div', { class: 'row-actions' }, [
        h(
          NButton,
          {
            text: true,
            loading: comparingSnapshotId.value === row.id,
            'aria-label': `重新比对 ${formatDate(row.snapshot_date)} ${accountLabelOf(row.broker_account_id)} 核对记录`,
            onClick: () => compareSnapshot(row)
          },
          () => '重新比对'
        ),
        ...(!row.import_batch_id
          ? [
              h(
                NButton,
                {
                  text: true,
                  type: 'primary',
                  'aria-label': `编辑 ${formatDate(row.snapshot_date)} 核对记录`,
                  onClick: () => openSnapshotDialog(row)
                },
                () => '编辑'
              ),
              h(
                NButton,
                {
                  text: true,
                  type: 'error',
                  'aria-label': `删除 ${formatDate(row.snapshot_date)} 核对记录`,
                  onClick: () => removeSnapshot(row)
                },
                () => '删除'
              )
            ]
          : [h(NTag, { size: 'small', bordered: false }, () => '导入生成 · 只读')])
      ])
  }
]
</script>

<template>
  <div>
    <div class="section-toolbar">
      <div>
        <h2>月末核对</h2>
        <p>录入券商月结单上的期末现金与持仓，与系统按流水推导的结果自动比对。</p>
      </div>
      <div class="toolbar-actions">
        <NButton :loading="loading" aria-label="重新加载月末核对" @click="reload">重新加载</NButton
        ><NButton type="primary" :disabled="!accounts.length" @click="openSnapshotDialog()"
          >新增核对</NButton
        >
      </div>
    </div>

    <NAlert
      v-if="loadError"
      type="warning"
      :show-icon="false"
      title="月末核对加载失败"
      class="read-alert"
      >{{
        hasLoaded ? '显示上次成功加载的记录，尚未确认最新结果。' : '尚未确认月末核对记录，请重试。'
      }}
      <NButton text type="primary" @click="reload">重试月末核对</NButton></NAlert
    >
    <p v-else-if="loading && hasLoaded" class="read-note" role="status">
      正在重新加载，以下为上次成功记录。
    </p>
    <p v-if="isAtListLimit(snapshots)" class="read-note">仅显示最近 {{ LIST_LIMIT }} 条核对记录</p>
    <NSpin :show="loading">
      <NDataTable
        v-if="!isMobileView"
        :data="snapshots"
        :columns="columns"
        :row-key="(row: SnapshotRow) => row.id"
        :scroll-x="1540"
        :bordered="false"
        ><template #empty
          ><NEmpty
            :description="emptyDescription"
            :theme-overrides="{ textColor: 'var(--app-text-muted)' }" /></template
      ></NDataTable>
      <div v-else class="mobile-card-list">
        <NEmpty
          v-if="!snapshots.length"
          :description="emptyDescription"
          :theme-overrides="{ textColor: 'var(--app-text-muted)' }"
        />
        <article
          v-for="row in snapshots"
          :key="row.id"
          class="mobile-card"
          data-testid="snapshot-card"
        >
          <div class="mobile-card-head">
            <div class="mobile-card-title">
              <strong class="mobile-card-symbol">{{ formatDate(row.snapshot_date) }}</strong
              ><span class="mobile-card-name">{{ accountLabelOf(row.broker_account_id) }}</span>
            </div>
            <button
              v-if="row.diff_detail"
              type="button"
              class="diff-status-button"
              :aria-label="`${snapshotStatusLabel(row)}，查看差异明细`"
              @click="openDiffDialog(row)"
            >
              <NTag size="small" :bordered="false" :type="statusType(row.status)">{{
                snapshotStatusLabel(row)
              }}</NTag></button
            ><NTag v-else size="small" :bordered="false" :type="statusType(row.status)">{{
              snapshotStatusLabel(row)
            }}</NTag>
          </div>
          <div class="mobile-card-meta">
            <span>范围 {{ statementScopeLabel(row.statement_scope) }}</span
            ><span>现金 {{ jsonSummary(row.cash_balances) }}</span
            ><span>持仓 {{ positionSummary(row.positions) }}</span
            ><span v-if="row.source_filename">{{ row.source_filename }}</span>
            <details v-if="isLongNote(row.notes)" class="read-details">
              <summary>查看完整备注</summary>
              <p>{{ row.notes }}</p>
            </details>
            <span v-else-if="row.notes">{{ row.notes }}</span>
          </div>
          <div class="mobile-card-actions">
            <NButton
              text
              :loading="comparingSnapshotId === row.id"
              :aria-label="`重新比对 ${formatDate(row.snapshot_date)} 核对记录`"
              @click="compareSnapshot(row)"
              >重新比对</NButton
            ><template v-if="!row.import_batch_id"
              ><NButton
                text
                type="primary"
                :aria-label="`编辑 ${formatDate(row.snapshot_date)} 核对记录`"
                @click="openSnapshotDialog(row)"
                >编辑</NButton
              ><NButton
                text
                type="error"
                :aria-label="`删除 ${formatDate(row.snapshot_date)} 核对记录`"
                @click="removeSnapshot(row)"
                >删除</NButton
              ></template
            ><NTag v-else size="small" :bordered="false">导入生成 · 只读</NTag>
          </div>
        </article>
      </div>
    </NSpin>

    <el-dialog
      v-model="snapshotDialog.visible"
      :title="snapshotDialog.id ? '编辑月末核对' : '新增月末核对'"
      width="min(720px, 96vw)"
      :close-on-click-modal="false"
      class="account-form-dialog"
    >
      <el-alert
        title="根据券商月结单核对后，记录报表余额、核对状态和差异说明。"
        type="info"
        :closable="false"
        show-icon
        class="dialog-alert"
      />
      <el-form
        ref="snapshotFormRef"
        :model="snapshotForm"
        :rules="snapshotRules"
        label-width="100px"
      >
        <el-form-item label="账户" prop="broker_account_id">
          <el-select
            v-model="snapshotForm.broker_account_id"
            aria-label="核对账户"
            placeholder="选择账户"
          >
            <el-option
              v-for="account in accounts"
              :key="account.id"
              :label="accountOptionLabel(account)"
              :value="account.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="核对日期" prop="snapshot_date">
          <el-date-picker
            v-model="snapshotForm.snapshot_date"
            aria-label="核对日期"
            type="date"
            format="YYYY/MM/DD"
            value-format="YYYY-MM-DD"
          />
        </el-form-item>
        <el-form-item label="来源文件">
          <el-input
            v-model="snapshotForm.source_filename"
            aria-label="核对来源文件"
            placeholder="例如：2026-06 月结单.pdf"
          />
        </el-form-item>
        <el-form-item label="现金余额">
          <div class="repeatable-fields">
            <div
              v-for="(item, index) in snapshotForm.cashRows"
              :key="`cash-${index}`"
              class="repeatable-row cash-row"
            >
              <el-select
                :aria-label="`第 ${index + 1} 行币种`"
                v-model="item.currency"
                placeholder="币种"
              >
                <el-option
                  v-for="currency in LEDGER_CURRENCIES"
                  :key="currency"
                  :label="currency"
                  :value="currency"
                />
              </el-select>
              <!-- 不设下限：融资账户期末现金可以是负数（后端允许） -->
              <el-input-number
                :aria-label="`第 ${index + 1} 行现金余额`"
                v-model="item.amount"
                :precision="2"
                controls-position="right"
                placeholder="报表余额（可为负）"
              />
              <el-button
                type="danger"
                text
                :disabled="snapshotForm.cashRows.length === 1"
                @click="snapshotForm.cashRows.splice(index, 1)"
              >
                移除
              </el-button>
            </div>
            <el-button plain :icon="Plus" @click="addCashRow">添加币种</el-button>
          </div>
        </el-form-item>
        <el-form-item label="持仓数量">
          <div class="repeatable-fields">
            <div
              v-for="(item, index) in snapshotForm.positionRows"
              :key="`position-${index}`"
              class="repeatable-row position-row"
            >
              <SecuritySelect
                v-model="item.symbol"
                :resolve="false"
                placeholder="标的代码"
                @select="item.market = $event.market"
              />
              <el-select
                :aria-label="`第 ${index + 1} 行市场`"
                v-model="item.market"
                placeholder="市场"
              >
                <el-option
                  v-for="market in marketOptions"
                  :key="market"
                  :label="market"
                  :value="market"
                />
              </el-select>
              <el-input-number
                :aria-label="`第 ${index + 1} 行持仓数量`"
                v-model="item.quantity"
                :min="0"
                controls-position="right"
                placeholder="数量"
              />
              <el-select
                :aria-label="`第 ${index + 1} 行币种`"
                v-model="item.currency"
                clearable
                placeholder="币种"
              >
                <el-option
                  v-for="currency in LEDGER_CURRENCIES"
                  :key="currency"
                  :label="currency"
                  :value="currency"
                />
              </el-select>
              <el-button type="danger" text @click="snapshotForm.positionRows.splice(index, 1)">
                移除
              </el-button>
            </div>
            <el-button plain :icon="Plus" @click="addPositionRow">添加持仓</el-button>
          </div>
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="snapshotForm.notes"
            aria-label="核对备注"
            type="textarea"
            :rows="3"
            placeholder="记录差异原因或凭证位置"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="mobile-dialog-footer">
          <el-button @click="snapshotDialog.visible = false">取消</el-button>
          <el-button type="primary" :loading="snapshotDialog.saving" @click="saveSnapshot"
            >保存</el-button
          >
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="diffDialog.visible" title="对账比对详情" width="min(820px, 96vw)">
      <template v-if="diffDialog.row">
        <div class="diff-meta">
          <el-tag :type="snapshotStatusTag(diffDialog.row.status)" size="small">
            {{ snapshotStatusLabel(diffDialog.row) }}
          </el-tag>
          <span>快照日 {{ formatDate(diffDialog.row.snapshot_date) }}</span>
          <span>范围 {{ statementScopeLabel(diffDialog.row.statement_scope) }}</span>
          <span v-if="diffDialog.row.compared_at"
            >比对于 {{ formatDateTime(diffDialog.row.compared_at) }}</span
          >
        </div>

        <h4>持仓比对</h4>
        <el-table :data="diffDialog.row.diff_detail?.positions || []" size="small" stripe>
          <template #empty><el-empty description="无持仓数据" :image-size="88" /></template>
          <el-table-column prop="symbol" label="代码" width="110" />
          <el-table-column prop="market" label="市场" width="90" />
          <el-table-column label="快照数量" width="110" align="right">
            <template #default="{ row }">{{ formatQuantity(row.snapshot_quantity) }}</template>
          </el-table-column>
          <el-table-column label="系统数量" width="110" align="right">
            <template #default="{ row }">{{ formatQuantity(row.system_quantity) }}</template>
          </el-table-column>
          <el-table-column label="差额（系统−快照）" width="140" align="right">
            <template #default="{ row }">{{ signedDelta(row.delta, 4) }}</template>
          </el-table-column>
          <el-table-column label="结果" min-width="120">
            <template #default="{ row }">
              <el-tag :type="diffItemTag(row.status)" size="small">
                {{ diffItemLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
        <p v-if="excludedSymbols(diffDialog.row).length" class="diff-excluded">
          已按排除规则双侧忽略：
          {{
            excludedSymbols(diffDialog.row)
              .map((item) => `${item.symbol}（${item.market}）`)
              .join('、')
          }}
        </p>

        <h4>现金比对</h4>
        <el-alert
          v-if="diffDialog.row.diff_detail?.summary?.cash_compared === false"
          type="info"
          :closable="false"
          title="分范围对账单的现金余额只属于该报表范围，不与账户级推导现金比对。"
        />
        <el-table v-else :data="diffDialog.row.diff_detail?.cash || []" size="small" stripe>
          <template #empty><el-empty description="无现金数据" :image-size="88" /></template>
          <el-table-column prop="currency" label="币种" width="90" />
          <el-table-column label="快照余额" width="130" align="right">
            <template #default="{ row }">{{
              formatCurrency(row.snapshot_balance, row.currency)
            }}</template>
          </el-table-column>
          <el-table-column label="推导余额" width="130" align="right">
            <template #default="{ row }">{{
              formatCurrency(row.derived_balance, row.currency)
            }}</template>
          </el-table-column>
          <el-table-column label="差额（推导−快照）" width="140" align="right">
            <template #default="{ row }">{{ signedDelta(row.delta, 2) }}</template>
          </el-table-column>
          <el-table-column label="结果" min-width="110">
            <template #default="{ row }">
              <el-tag :type="row.status === 'MATCH' ? 'success' : 'warning'" size="small">
                {{ row.status === 'MATCH' ? '一致' : '差异' }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>

        <template v-if="(diffDialog.row.diff_detail?.replay_inconsistent || []).length">
          <h4>账户归属矛盾</h4>
          <ul class="diff-notes">
            <li
              v-for="item in diffDialog.row.diff_detail?.replay_inconsistent || []"
              :key="`${item.symbol}-${item.market}`"
            >
              {{ item.symbol }}（{{ item.market }}）：{{ item.reason }}
            </li>
          </ul>
        </template>

        <el-collapse>
          <el-collapse-item title="比对口径说明">
            <ul class="diff-notes">
              <li
                v-for="(note, index) in diffDialog.row.diff_detail?.methodology_notes || []"
                :key="index"
              >
                {{ note }}
              </li>
            </ul>
          </el-collapse-item>
        </el-collapse>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.repeatable-fields {
  display: grid;
  gap: 10px;
  width: 100%;
}

.repeatable-fields > .el-button {
  justify-self: start;
}

.repeatable-row {
  display: grid;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 10px;
  border: 1px solid var(--app-border-soft);
  border-radius: var(--app-radius-inner);
  background: var(--app-surface-muted);
}

.repeatable-row :deep(.el-select),
.repeatable-row :deep(.el-input-number) {
  width: 100%;
}

.cash-row {
  grid-template-columns: 110px minmax(160px, 1fr) auto;
}

.position-row {
  grid-template-columns: minmax(120px, 1fr) 115px minmax(150px, 1fr) 100px auto;
}

.dialog-alert {
  margin-bottom: 20px;
}

.diff-tag-clickable {
  cursor: pointer;
}

.diff-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
  color: var(--app-text-muted);
  font-size: 13px;
}

.list-limit-alert {
  margin-bottom: 12px;
}

.diff-excluded {
  margin: 6px 0 0;
  color: var(--app-text-muted);
  font-size: 12px;
}

.diff-notes {
  margin: 4px 0;
  padding-left: 18px;
  color: var(--app-text-muted);
  font-size: 13px;
  line-height: 1.7;
}

@media (max-width: 640px) {
  .cash-row,
  .position-row {
    grid-template-columns: 1fr;
  }

  .repeatable-row > .el-button {
    width: 100%;
  }
}
</style>
