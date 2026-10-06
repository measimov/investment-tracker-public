<script setup lang="ts">
import type { CashEventCreate } from '@/types'
import { LEDGER_CURRENCIES } from '@/utils/currency'
import { makeConfirmedAction } from '@/composables/useConfirmAction'
import { computed, h, nextTick, reactive, ref } from 'vue'
import { NAlert, NButton, NDataTable, NEmpty, NSpin, NTag, type DataTableColumns } from 'naive-ui'
import { type FormInstance } from 'element-plus'
import api from '@/api'
import { isLongNote, renderNote } from './shared'
import DividendTaxDialog from './DividendTaxDialog.vue'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { formatDate, formatCurrency, pnlClass } from '@/utils/helpers'
import {
  type AccountListStatus,
  accountLabel,
  accountOptionLabel,
  CASH_EVENT_TYPE_LABELS,
  cashEventTypeLabel as cashTypeLabel,
  optionsOf
} from '@/utils/labels'
import {
  type AccountRow,
  type CashEventRow,
  type DialogState,
  isAtListLimit,
  LIST_LIMIT,
  makeSaver,
  today
} from './shared'

const props = defineProps<{
  cashEvents: CashEventRow[]
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

const cashTypeOptions = optionsOf(CASH_EVENT_TYPE_LABELS)

const cashFilters = reactive<{ accountId: number | null; eventType: string }>({
  accountId: null,
  eventType: ''
})

const filteredCashEvents = computed(() =>
  props.cashEvents.filter((item) => {
    const accountId = item.broker_account_id
    return (
      (!cashFilters.accountId || String(accountId) === String(cashFilters.accountId)) &&
      (!cashFilters.eventType || item.event_type === cashFilters.eventType)
    )
  })
)

const cashDialog = reactive<DialogState>({ visible: false, id: null, saving: false })
const taxEvent = ref<CashEventRow | null>(null)
const cashFormRef = ref<FormInstance>()
const cashForm = reactive<{
  broker_account_id?: number | null
  event_date?: string
  event_type?: string
  tax_kind?: 'DIVIDEND' | null
  amount?: number | null
  currency?: string
  notes?: string
}>({})

const cashRules = {
  broker_account_id: [{ required: true, message: '请选择账户', trigger: 'change' }],
  event_date: [{ required: true, message: '请选择日期', trigger: 'change' }],
  event_type: [{ required: true, message: '请选择类型', trigger: 'change' }],
  amount: [{ required: true, message: '请输入金额', trigger: 'blur' }],
  currency: [{ required: true, message: '请选择币种', trigger: 'change' }]
}

function resetCashForm(row: Partial<CashEventRow> = {}) {
  Object.assign(cashForm, {
    broker_account_id: row.broker_account_id || props.accounts[0]?.id || null,
    event_date: (row.event_date || today()).slice(0, 10),
    event_type: row.event_type || 'DEPOSIT',
    tax_kind: row.tax_kind ?? null,
    amount: row.amount == null ? null : Math.abs(Number(row.amount)),
    currency:
      row.currency ||
      props.accounts.find((item) => item.id === row.broker_account_id)?.base_currency ||
      'CNY',
    notes: row.notes || ''
  })
}

function openCashDialog(row?: CashEventRow) {
  cashDialog.id = row?.id || null
  resetCashForm(row)
  cashDialog.visible = true
  void nextTick(() => cashFormRef.value?.clearValidate())
}

const saveCashEvent = makeSaver({
  formRef: cashFormRef,
  dialog: cashDialog,
  // 表单校验（cashRules）保证必填项：这里是「已校验表单 → 请求体」的唯一断言点
  buildPayload: () =>
    ({
      ...cashForm,
      tax_kind: cashForm.event_type === 'TAX' ? cashForm.tax_kind : null
    }) as CashEventCreate,
  update: (id, payload) => api.updateCashEvent(id, payload),
  create: (payload) => api.createCashEvent(payload),
  messages: { updated: '现金事件已更新', created: '现金事件已新增', failure: '现金事件保存失败' },
  reload: () => props.reload()
})

const removeCashEvent = makeConfirmedAction<CashEventRow>({
  title: '删除现金事件',
  confirmText: '删除',
  message: '该操作会影响后续账户收益校准，确认删除？',
  request: (row) => api.deleteCashEvent(row.id),
  successMessage: '现金事件已删除',
  failureMessage: '现金事件删除失败',
  reload: () => props.reload()
})

const cashTypeTag = (type: string | undefined) => {
  if (['DEPOSIT', 'INTEREST', 'TRANSFER_IN', 'FX_IN'].includes(type || '')) return 'success'
  if (['WITHDRAWAL', 'FEE', 'TAX', 'TRANSFER_OUT', 'FX_OUT'].includes(type || '')) return 'warning'
  return 'default'
}
const cashDirection = (row: CashEventRow) =>
  ['WITHDRAWAL', 'FEE', 'TAX', 'TRANSFER_OUT', 'FX_OUT'].includes(row.event_type || '') ? -1 : 1
const signedAmount = (row: CashEventRow) => {
  const value = Math.abs(Number(row.amount || 0)) * cashDirection(row)
  const prefix = value > 0 ? '+' : ''
  return `${prefix}${formatCurrency(value, row.currency)}`
}
const amountClass = (row: CashEventRow) => pnlClass(Number(row.amount) * cashDirection(row))

const taxBalanceText = (row: CashEventRow) =>
  row.unallocated_tax_amount == null
    ? '归属余额未知'
    : Number(row.unallocated_tax_amount) > 0
      ? `待归属 ${formatCurrency(row.unallocated_tax_amount, row.currency)}`
      : '已归属'
const emptyDescription = computed(() =>
  props.loadError
    ? '尚未确认现金事件，请重试'
    : !props.hasLoaded
      ? '现金事件正在加载'
      : cashFilters.accountId || cashFilters.eventType
        ? '没有匹配当前筛选的现金事件'
        : '暂无现金事件'
)
const rowName = (row: CashEventRow) =>
  `${formatDate(row.event_date)} ${accountLabelOf(row.broker_account_id)} ${cashTypeLabel(row.event_type)}`
const columns: DataTableColumns<CashEventRow> = [
  { title: '日期', key: 'event_date', width: 120, render: (row) => formatDate(row.event_date) },
  {
    title: '账户',
    key: 'broker_account_id',
    width: 185,
    render: (row) => accountLabelOf(row.broker_account_id)
  },
  {
    title: '类型',
    key: 'event_type',
    width: 120,
    render: (row) =>
      h(NTag, { size: 'small', bordered: false, type: cashTypeTag(row.event_type) }, () =>
        cashTypeLabel(row.event_type)
      )
  },
  {
    title: '金额',
    key: 'amount',
    width: 160,
    align: 'right',
    render: (row) => h('strong', { class: ['num', amountClass(row)] }, signedAmount(row))
  },
  {
    title: '备注',
    key: 'notes',
    width: 240,
    cellProps: () => ({ style: { verticalAlign: 'top' } }),
    render: (row) => renderNote(row.notes)
  },
  {
    title: '股息税归属',
    key: 'tax_kind',
    width: 150,
    render: (row) => (row.tax_kind === 'DIVIDEND' ? taxBalanceText(row) : '—')
  },
  {
    title: '操作',
    key: 'actions',
    width: 200,
    fixed: 'right',
    render: (row) =>
      h('div', { class: 'row-actions' }, [
        row.tax_kind === 'DIVIDEND'
          ? h(
              NButton,
              {
                text: true,
                type: 'primary',
                'aria-label': `${rowName(row)} 股息税归属`,
                onClick: () => (taxEvent.value = row)
              },
              () => '归属'
            )
          : null,
        ...(!row.read_only
          ? [
              h(
                NButton,
                {
                  text: true,
                  type: 'primary',
                  'aria-label': `编辑 ${rowName(row)} 现金事件`,
                  onClick: () => openCashDialog(row)
                },
                () => '编辑'
              ),
              h(
                NButton,
                {
                  text: true,
                  type: 'error',
                  'aria-label': `删除 ${rowName(row)} 现金事件`,
                  onClick: () => removeCashEvent(row)
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
        <h2>现金事件</h2>
        <p>
          保存真实资金变化，用于现金对账与出入金追踪；收益统计为权益仓口径（仅证券投入，不含账户现金）。
        </p>
      </div>
      <div class="toolbar-actions">
        <NButton :loading="loading" aria-label="重新加载现金事件" @click="reload">重新加载</NButton
        ><NButton type="primary" :disabled="!accounts.length" @click="openCashDialog()"
          >新增事件</NButton
        >
      </div>
    </div>

    <div class="compact-filter">
      <label class="filter-field"
        >账户<select v-model="cashFilters.accountId" aria-label="现金事件账户">
          <option :value="null">全部账户</option>
          <option v-for="account in accounts" :key="account.id" :value="account.id">
            {{ accountOptionLabel(account) }}
          </option>
        </select></label
      ><label class="filter-field"
        >类型<select v-model="cashFilters.eventType" aria-label="现金事件类型">
          <option value="">全部类型</option>
          <option v-for="item in cashTypeOptions" :key="item.value" :value="item.value">
            {{ item.label }}
          </option>
        </select></label
      >
    </div>
    <NAlert
      v-if="loadError"
      type="warning"
      :show-icon="false"
      class="read-alert"
      title="现金事件加载失败"
      >{{
        hasLoaded ? '显示上次成功加载的现金事件，尚未确认最新结果。' : '尚未确认现金事件，请重试。'
      }}
      <NButton text type="primary" @click="reload">重试现金事件</NButton></NAlert
    >
    <p v-else-if="loading && hasLoaded" class="read-note" role="status">
      正在重新加载，以下为上次成功现金事件。
    </p>
    <p v-if="isAtListLimit(cashEvents)" class="read-note">
      仅显示最近 {{ LIST_LIMIT }} 条现金事件，筛选只在这些记录内进行
    </p>

    <NSpin v-if="!isMobileView" :show="loading"
      ><NDataTable
        class="cash-table"
        :data="filteredCashEvents"
        :columns="columns"
        :row-key="(row: CashEventRow) => row.id"
        :scroll-x="1175"
        :bordered="false"
        ><template #empty
          ><NEmpty
            :description="emptyDescription"
            :theme-overrides="{ textColor: 'var(--app-text-muted)' }" /></template></NDataTable
    ></NSpin>

    <NSpin v-else :show="loading"
      ><div class="mobile-card-list">
        <NEmpty
          v-if="!filteredCashEvents.length"
          :description="emptyDescription"
          :theme-overrides="{ textColor: 'var(--app-text-muted)' }"
        />
        <article
          v-for="row in filteredCashEvents"
          :key="row.id"
          class="mobile-card"
          data-testid="cash-event-card"
        >
          <div class="mobile-card-head">
            <div class="mobile-card-title">
              <span class="mobile-card-symbol" :class="amountClass(row)">
                {{ signedAmount(row) }}
              </span>
              <span class="mobile-card-name">
                {{ accountLabelOf(row.broker_account_id) }}
              </span>
            </div>
            <div class="mobile-card-tags">
              <NTag :bordered="false" :type="cashTypeTag(row.event_type)" size="small">
                {{ cashTypeLabel(row.event_type) }}
              </NTag>
            </div>
          </div>

          <div class="mobile-card-meta">
            <span>{{ formatDate(row.event_date) }}</span>
            <details v-if="isLongNote(row.notes)" class="read-details">
              <summary>查看完整备注</summary>
              <p>{{ row.notes }}</p>
            </details>
            <span v-else-if="row.notes">{{ row.notes }}</span
            ><span v-if="row.tax_kind === 'DIVIDEND'">{{ taxBalanceText(row) }}</span>
          </div>

          <div class="mobile-card-actions">
            <NButton
              v-if="row.tax_kind === 'DIVIDEND'"
              type="primary"
              text
              :aria-label="`${rowName(row)} 股息税归属`"
              @click="taxEvent = row"
            >
              {{ Number(row.unallocated_tax_amount) > 0 ? '待归属股息税' : '股息税归属' }}
            </NButton>
            <template v-if="!row.read_only">
              <NButton
                type="primary"
                size="small"
                text
                :aria-label="`编辑 ${rowName(row)} 现金事件`"
                @click="openCashDialog(row)"
              >
                编辑
              </NButton>
              <NButton
                type="error"
                size="small"
                text
                :aria-label="`删除 ${rowName(row)} 现金事件`"
                @click="removeCashEvent(row)"
              >
                删除
              </NButton>
            </template>
            <NTag :bordered="false" v-else type="default" size="small">导入生成 · 只读</NTag>
          </div>
        </article>
      </div></NSpin
    >

    <el-dialog
      v-model="cashDialog.visible"
      :title="cashDialog.id ? '编辑现金事件' : '新增现金事件'"
      width="560px"
      :close-on-click-modal="false"
      class="account-form-dialog"
      :close-on-press-escape="!cashDialog.saving"
      :show-close="!cashDialog.saving"
    >
      <el-form ref="cashFormRef" :model="cashForm" :rules="cashRules" label-width="100px">
        <el-form-item label="账户" prop="broker_account_id">
          <el-select
            v-model="cashForm.broker_account_id"
            aria-label="现金事件账户"
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
        <el-form-item label="日期" prop="event_date">
          <el-date-picker
            v-model="cashForm.event_date"
            aria-label="现金事件日期"
            type="date"
            format="YYYY/MM/DD"
            value-format="YYYY-MM-DD"
          />
        </el-form-item>
        <el-form-item label="类型" prop="event_type">
          <el-select v-model="cashForm.event_type" aria-label="现金事件类型">
            <el-option
              v-for="item in cashTypeOptions"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item v-if="cashForm.event_type === 'TAX'" label="税款用途">
          <el-checkbox
            :model-value="cashForm.tax_kind === 'DIVIDEND'"
            @change="cashForm.tax_kind = $event ? 'DIVIDEND' : null"
          >
            股息税（计入股息收益）
          </el-checkbox>
        </el-form-item>
        <el-form-item label="金额" prop="amount">
          <el-input-number
            v-model="cashForm.amount"
            aria-label="现金事件金额"
            :min="0.01"
            :precision="2"
            :step="100"
            controls-position="right"
          />
          <span class="field-hint">金额始终填正数，资金方向由事件类型表达</span>
        </el-form-item>
        <el-form-item label="币种" prop="currency">
          <el-select v-model="cashForm.currency" aria-label="现金事件币种">
            <el-option
              v-for="currency in LEDGER_CURRENCIES"
              :key="currency"
              :label="currency"
              :value="currency"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="cashForm.notes"
            aria-label="现金事件备注"
            type="textarea"
            :rows="3"
            placeholder="例如：由银行账户转入"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="mobile-dialog-footer">
          <el-button :disabled="cashDialog.saving" @click="cashDialog.visible = false"
            >取消</el-button
          >
          <NButton
            type="primary"
            class="form-save-button"
            aria-label="保存"
            :loading="cashDialog.saving"
            :aria-disabled="cashDialog.saving"
            :aria-busy="cashDialog.saving"
            @click="saveCashEvent"
            >保存</NButton
          >
        </div>
      </template>
    </el-dialog>
    <DividendTaxDialog
      v-if="taxEvent"
      :key="taxEvent.id"
      :event="taxEvent"
      :reload="reload"
      @close="taxEvent = null"
    />
  </div>
</template>

<style scoped>
.list-limit-alert {
  margin-bottom: 12px;
}

@media (max-width: 640px) {
  .form-save-button {
    min-height: 44px;
    width: 100%;
  }
}
</style>
