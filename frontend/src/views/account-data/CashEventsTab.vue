<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { type FormInstance } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import api from '@/api'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { formatDate, formatNumber } from '@/utils/helpers'
import {
  type AccountRow,
  type CashEventRow,
  type DialogState,
  accountLabelIn,
  accountName,
  currencyOptions,
  makeRemover,
  makeSaver,
  today
} from './shared'

const props = defineProps<{
  cashEvents: CashEventRow[]
  accounts: AccountRow[]
  loading: boolean
  reload: () => Promise<unknown>
}>()

const isMobileView = useMediaQuery('(max-width: 640px)')
const accountLabel = (id: unknown) => accountLabelIn(props.accounts, id)

const cashTypeOptions = [
  { label: '入金', value: 'DEPOSIT' },
  { label: '出金', value: 'WITHDRAWAL' },
  { label: '利息', value: 'INTEREST' },
  { label: '费用', value: 'FEE' },
  { label: '税费', value: 'TAX' },
  { label: '转入', value: 'TRANSFER_IN' },
  { label: '转出', value: 'TRANSFER_OUT' },
  { label: '换汇转入', value: 'FX_IN' },
  { label: '换汇转出', value: 'FX_OUT' },
  { label: '其他', value: 'OTHER' }
]

const cashFilters = reactive<{ accountId: number | null; eventType: string }>({
  accountId: null,
  eventType: ''
})

const filteredCashEvents = computed(() =>
  props.cashEvents.filter((item) => {
    const accountId = item.broker_account_id ?? item.account_id
    return (
      (!cashFilters.accountId || String(accountId) === String(cashFilters.accountId)) &&
      (!cashFilters.eventType || item.event_type === cashFilters.eventType)
    )
  })
)

const cashDialog = reactive<DialogState>({ visible: false, id: null, saving: false })
const cashFormRef = ref<FormInstance>()
const cashForm = reactive<{
  broker_account_id?: number | null
  event_date?: string
  event_type?: string
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
    broker_account_id: row.broker_account_id || row.account_id || props.accounts[0]?.id || null,
    event_date: (row.event_date || row.occurred_at || today()).slice(0, 10),
    event_type: row.event_type || 'DEPOSIT',
    amount: row.amount == null ? null : Math.abs(Number(row.amount)),
    currency:
      row.currency ||
      props.accounts.find((item) => item.id === (row.broker_account_id || row.account_id))
        ?.base_currency ||
      'CNY',
    notes: row.notes || ''
  })
}

function openCashDialog(row?: CashEventRow) {
  cashDialog.id = row?.id || null
  resetCashForm(row)
  cashDialog.visible = true
}

const saveCashEvent = makeSaver({
  formRef: cashFormRef,
  dialog: cashDialog,
  buildPayload: () => ({ ...cashForm }),
  update: (id, payload) => api.updateCashEvent(id, payload),
  create: (payload) => api.createCashEvent(payload),
  messages: { updated: '现金事件已更新', created: '现金事件已新增', failure: '现金事件保存失败' },
  reload: () => props.reload()
})

const removeCashEvent = makeRemover<CashEventRow>({
  title: '删除现金事件',
  message: '该操作会影响后续账户收益校准，确认删除？',
  request: (row) => api.deleteCashEvent(row.id),
  successMessage: '现金事件已删除',
  failureMessage: '现金事件删除失败',
  reload: () => props.reload()
})

const cashTypeLabel = (type: string | undefined) =>
  cashTypeOptions.find((item) => item.value === type)?.label || type || '其他'
const cashTypeTag = (type: string | undefined) => {
  if (['DEPOSIT', 'INTEREST', 'TRANSFER_IN', 'FX_IN'].includes(type || '')) return 'success'
  if (['WITHDRAWAL', 'FEE', 'TAX', 'TRANSFER_OUT', 'FX_OUT'].includes(type || '')) return 'warning'
  return 'info'
}
const cashDirection = (row: CashEventRow) =>
  ['WITHDRAWAL', 'FEE', 'TAX', 'TRANSFER_OUT', 'FX_OUT'].includes(row.event_type || '') ? -1 : 1
const signedAmount = (row: CashEventRow) => {
  const value = Math.abs(Number(row.amount || 0)) * cashDirection(row)
  const prefix = value > 0 ? '+' : ''
  return `${prefix}${formatNumber(value)} ${row.currency || ''}`
}
const amountClass = (row: CashEventRow) => ({
  'amount-positive': cashDirection(row) > 0,
  'amount-negative': cashDirection(row) < 0
})
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
      <el-button type="primary" :icon="Plus" :disabled="!accounts.length" @click="openCashDialog()">
        新增事件
      </el-button>
    </div>

    <el-form :inline="true" class="compact-filter">
      <el-form-item label="账户">
        <el-select v-model="cashFilters.accountId" clearable placeholder="全部账户">
          <el-option
            v-for="account in accounts"
            :key="account.id"
            :label="accountName(account)"
            :value="account.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="类型">
        <el-select v-model="cashFilters.eventType" clearable placeholder="全部类型">
          <el-option
            v-for="item in cashTypeOptions"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
      </el-form-item>
    </el-form>

    <div v-if="!isMobileView" class="responsive-table desktop-data-table">
      <el-table :data="filteredCashEvents" v-loading="loading" stripe row-key="id">
        <template #empty>
          <el-empty description="暂无现金事件" :image-size="88" />
        </template>
        <el-table-column prop="event_date" label="日期" width="120">
          <template #default="{ row }">{{
            formatDate(row.event_date || row.occurred_at)
          }}</template>
        </el-table-column>
        <el-table-column label="账户" min-width="170">
          <template #default="{ row }">{{
            accountLabel(row.broker_account_id || row.account_id)
          }}</template>
        </el-table-column>
        <el-table-column prop="event_type" label="类型" width="110">
          <template #default="{ row }">
            <el-tag :type="cashTypeTag(row.event_type)" size="small">
              {{ cashTypeLabel(row.event_type) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="金额" min-width="130" align="right">
          <template #default="{ row }">
            <strong :class="amountClass(row)">{{ signedAmount(row) }}</strong>
          </template>
        </el-table-column>
        <el-table-column prop="notes" label="备注" min-width="200" show-overflow-tooltip />
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <template v-if="!row.imported">
              <el-button type="primary" text @click="openCashDialog(row)">编辑</el-button>
              <el-button type="danger" text @click="removeCashEvent(row)">删除</el-button>
            </template>
            <el-tag v-else type="info" size="small">导入生成 · 只读</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div v-else v-loading="loading" class="mobile-card-list">
      <el-empty v-if="!filteredCashEvents.length" description="暂无现金事件" :image-size="88" />
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
              {{ accountLabel(row.broker_account_id || row.account_id) }}
            </span>
          </div>
          <div class="mobile-card-tags">
            <el-tag :type="cashTypeTag(row.event_type)" size="small">
              {{ cashTypeLabel(row.event_type) }}
            </el-tag>
          </div>
        </div>

        <div class="mobile-card-meta">
          <span>{{ formatDate(row.event_date || row.occurred_at) }}</span>
          <span v-if="row.notes">{{ row.notes }}</span>
        </div>

        <div class="mobile-card-actions">
          <template v-if="!row.imported">
            <el-button type="primary" size="small" text @click="openCashDialog(row)">
              编辑
            </el-button>
            <el-button type="danger" size="small" text @click="removeCashEvent(row)">
              删除
            </el-button>
          </template>
          <el-tag v-else type="info" size="small">导入生成 · 只读</el-tag>
        </div>
      </article>
    </div>

    <el-dialog
      v-model="cashDialog.visible"
      :title="cashDialog.id ? '编辑现金事件' : '新增现金事件'"
      width="560px"
    >
      <el-form ref="cashFormRef" :model="cashForm" :rules="cashRules" label-width="100px">
        <el-form-item label="账户" prop="broker_account_id">
          <el-select v-model="cashForm.broker_account_id" placeholder="选择账户">
            <el-option
              v-for="account in accounts"
              :key="account.id"
              :label="accountName(account)"
              :value="account.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="日期" prop="event_date">
          <el-date-picker v-model="cashForm.event_date" type="date" value-format="YYYY-MM-DD" />
        </el-form-item>
        <el-form-item label="类型" prop="event_type">
          <el-select v-model="cashForm.event_type">
            <el-option
              v-for="item in cashTypeOptions"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="金额" prop="amount">
          <el-input-number
            v-model="cashForm.amount"
            :min="0.01"
            :precision="2"
            :step="100"
            controls-position="right"
          />
          <span class="field-hint">金额始终填正数，资金方向由事件类型表达</span>
        </el-form-item>
        <el-form-item label="币种" prop="currency">
          <el-select v-model="cashForm.currency">
            <el-option
              v-for="currency in currencyOptions"
              :key="currency"
              :label="currency"
              :value="currency"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="cashForm.notes"
            type="textarea"
            :rows="3"
            placeholder="例如：由银行账户转入"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="mobile-dialog-footer">
          <el-button @click="cashDialog.visible = false">取消</el-button>
          <el-button type="primary" :loading="cashDialog.saving" @click="saveCashEvent"
            >保存</el-button
          >
        </div>
      </template>
    </el-dialog>
  </div>
</template>
