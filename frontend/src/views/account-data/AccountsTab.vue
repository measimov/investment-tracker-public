<script setup lang="ts">
import type { BrokerAccountCreate } from '@/types'
import { EMPTY } from '@/utils/helpers'
import { LEDGER_CURRENCIES } from '@/utils/currency'
import { accountShortName, maskAccountNumber } from '@/utils/labels'
import { makeConfirmedAction } from '@/composables/useConfirmAction'
import { computed, h, nextTick, reactive, ref } from 'vue'
import {
  NAlert,
  NButton,
  NCheckbox,
  NDataTable,
  NEmpty,
  NSpin,
  NTag,
  type DataTableColumns
} from 'naive-ui'
import { type FormInstance } from 'element-plus'
import api from '@/api'
import { isLongNote, renderNote } from './shared'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { type AccountRow, type DialogState, brokerOptions, makeSaver } from './shared'

const props = defineProps<{
  accounts: AccountRow[]
  loading: boolean
  hasLoaded: boolean
  loadError: boolean
  reload: () => Promise<unknown>
}>()

const isMobileView = useMediaQuery('(max-width: 640px)')

const accountDialog = reactive<DialogState>({ visible: false, id: null, saving: false })
const accountFormRef = ref<FormInstance>()
const accountForm = reactive<{
  account_name?: string
  broker?: string
  account_number_masked?: string
  base_currency?: string
  is_active?: boolean
  notes?: string
}>({})

const accountRules = {
  account_name: [{ required: true, message: '请输入账户名称', trigger: 'blur' }],
  broker: [{ required: true, message: '请选择券商', trigger: 'change' }],
  base_currency: [{ required: true, message: '请选择基础币种', trigger: 'change' }]
}

function resetAccountForm(row: Partial<AccountRow> = {}) {
  Object.assign(accountForm, {
    account_name: row.account_name || '',
    broker: row.broker || '',
    account_number_masked: row.account_number_masked || '',
    base_currency: row.base_currency || 'CNY',
    is_active: row.is_active !== false,
    notes: row.notes || ''
  })
}

function openAccountDialog(row?: AccountRow) {
  accountDialog.id = row?.id || null
  resetAccountForm(row)
  accountDialog.visible = true
  void nextTick(() => accountFormRef.value?.clearValidate())
}

const saveAccount = makeSaver({
  formRef: accountFormRef,
  dialog: accountDialog,
  // 表单校验（accountRules）保证必填项：这里是「已校验表单 → 请求体」的唯一断言点
  buildPayload: () => ({ ...accountForm }) as BrokerAccountCreate,
  update: (id, payload) => api.updateBrokerAccount(id, payload),
  create: (payload) => api.createBrokerAccount(payload),
  messages: { updated: '账户已更新', created: '账户已新增', failure: '账户保存失败' },
  reload: () => props.reload()
})

const removeAccount = makeConfirmedAction<AccountRow>({
  title: '删除账户',
  confirmText: '删除',
  message: (row) =>
    `仅空账户可以删除。若「${accountShortName(row)}」已有交易或审计记录，请编辑账户并将其停用。`,
  request: (row) => api.deleteBrokerAccount(row.id),
  successMessage: '账户已删除',
  failureMessage: '账户删除失败',
  reload: () => props.reload()
})

const emptyDescription = computed(() =>
  props.loadError ? '尚未确认账户，请重试' : !props.hasLoaded ? '账户正在加载' : '尚未登记券商账户'
)
const columns: DataTableColumns<AccountRow> = [
  {
    title: '账户',
    key: 'account_name',
    width: 220,
    render: (row) =>
      h('div', { class: 'primary-cell' }, [
        h('strong', accountShortName(row)),
        h('span', maskAccountNumber(row.account_number_masked) || '未填写尾号')
      ])
  },
  { title: '券商', key: 'broker', width: 150, render: (row) => row.broker || EMPTY },
  { title: '基础币种', key: 'base_currency', width: 105 },
  {
    title: '状态',
    key: 'is_active',
    width: 90,
    render: (row) =>
      h(
        NTag,
        { size: 'small', bordered: false, type: row.is_active === false ? 'default' : 'success' },
        () => (row.is_active === false ? '停用' : '启用')
      )
  },
  {
    title: '备注',
    key: 'notes',
    width: 220,
    cellProps: () => ({ style: { verticalAlign: 'top' } }),
    render: (row) => renderNote(row.notes)
  },
  {
    title: '操作',
    key: 'actions',
    width: 190,
    fixed: 'right',
    render: (row) =>
      h('div', [
        h('div', { class: 'row-actions' }, [
          h(
            NButton,
            {
              text: true,
              type: 'primary',
              'aria-label': `编辑 ${accountShortName(row)} 账户`,
              onClick: () => openAccountDialog(row)
            },
            () => '编辑'
          ),
          h(
            NButton,
            {
              text: true,
              type: 'error',
              disabled: row.has_records,
              'aria-label': `删除 ${accountShortName(row)} 空账户`,
              onClick: () => removeAccount(row)
            },
            () => '删除空账户'
          )
        ]),
        row.has_records ? h('p', { class: 'cell-sub' }, '已有记录，可编辑后停用') : null
      ])
  }
]
</script>

<template>
  <div>
    <div class="section-toolbar">
      <div>
        <h2>券商账户</h2>
        <p>为交易以及后续的账户级持仓、收益统计建立明确归属。</p>
      </div>
      <div class="toolbar-actions">
        <NButton :loading="loading" aria-label="重新加载账户" @click="reload">重新加载</NButton
        ><NButton type="primary" @click="openAccountDialog()">新增账户</NButton>
      </div>
    </div>

    <NAlert
      v-if="loadError"
      type="warning"
      :show-icon="false"
      class="read-alert"
      title="账户加载失败"
      >{{ hasLoaded ? '显示上次成功加载的账户，尚未确认最新结果。' : '尚未确认账户，请重试。' }}
      <NButton text type="primary" @click="reload">重试账户</NButton></NAlert
    >
    <p v-else-if="loading && hasLoaded" class="read-note" role="status">
      正在重新加载，以下为上次成功账户。
    </p>
    <NSpin v-if="!isMobileView" :show="loading"
      ><NDataTable
        :data="accounts"
        :columns="columns"
        :row-key="(row: AccountRow) => row.id"
        :scroll-x="975"
        :bordered="false"
        class="account-table"
        ><template #empty
          ><NEmpty
            :description="emptyDescription"
            :theme-overrides="{ textColor: 'var(--app-text-muted)' }"
            ><template v-if="hasLoaded && !loadError && !loading" #extra
              ><NButton type="primary" @click="openAccountDialog()"
                >新增第一个账户</NButton
              ></template
            ></NEmpty
          ></template
        ></NDataTable
      ></NSpin
    >

    <NSpin v-else :show="loading"
      ><div class="mobile-card-list">
        <NEmpty
          v-if="!accounts.length"
          :description="emptyDescription"
          :theme-overrides="{ textColor: 'var(--app-text-muted)' }"
          ><template v-if="hasLoaded && !loadError && !loading" #extra
            ><NButton type="primary" @click="openAccountDialog()">新增第一个账户</NButton></template
          ></NEmpty
        >
        <article
          v-for="row in accounts"
          :key="row.id"
          class="mobile-card"
          data-testid="account-card"
        >
          <div class="mobile-card-head">
            <div class="mobile-card-title">
              <span class="mobile-card-symbol">{{ accountShortName(row) }}</span>
              <span class="mobile-card-name">
                {{ maskAccountNumber(row.account_number_masked) || '未填写尾号' }}
              </span>
            </div>
            <div class="mobile-card-tags">
              <NTag
                :type="row.is_active === false ? 'default' : 'success'"
                size="small"
                :bordered="false"
              >
                {{ row.is_active === false ? '停用' : '启用' }}
              </NTag>
            </div>
          </div>

          <div class="mobile-card-meta">
            <span>{{ row.broker || '未填写券商' }}</span>
            <span>{{ row.base_currency }}</span>
            <details v-if="isLongNote(row.notes)" class="read-details">
              <summary>查看完整备注</summary>
              <p>{{ row.notes }}</p>
            </details>
            <span v-else-if="row.notes">{{ row.notes }}</span>
          </div>

          <div class="mobile-card-actions">
            <NButton
              type="primary"
              text
              :aria-label="`编辑 ${accountShortName(row)} 账户`"
              @click="openAccountDialog(row)"
            >
              编辑
            </NButton>
            <NButton
              v-if="!row.has_records"
              type="error"
              text
              :aria-label="`删除 ${accountShortName(row)} 空账户`"
              @click="removeAccount(row)"
            >
              删除空账户
            </NButton>
          </div>
        </article>
      </div></NSpin
    >

    <el-dialog
      v-model="accountDialog.visible"
      :title="accountDialog.id ? '编辑券商账户' : '新增券商账户'"
      width="560px"
      :close-on-click-modal="false"
      class="account-form-dialog"
      :close-on-press-escape="!accountDialog.saving"
      :show-close="!accountDialog.saving"
    >
      <el-form ref="accountFormRef" :model="accountForm" :rules="accountRules" label-width="100px">
        <el-form-item label="账户名称" prop="account_name">
          <el-input
            v-model="accountForm.account_name"
            aria-label="账户名称"
            placeholder="例如：IBKR 主账户"
          />
        </el-form-item>
        <el-form-item label="券商" prop="broker">
          <!-- 浮层向上展开：默认向下会盖住尚未填写的「账户尾号」，快速录入/
               自动化对下一个字段的点击会落进浮层（选错项或输入丢失）。录入
               顺序自上而下，向上只盖住已填过的「账户名称」，无害。(#87) -->
          <el-select
            v-model="accountForm.broker"
            aria-label="券商"
            filterable
            allow-create
            default-first-option
            placement="top-start"
            :fallback-placements="['top-start', 'bottom-start']"
            placeholder="选择或输入券商"
          >
            <el-option
              v-for="broker in brokerOptions"
              :key="broker"
              :label="broker"
              :value="broker"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="账户尾号">
          <el-input
            v-model="accountForm.account_number_masked"
            aria-label="账户尾号"
            placeholder="只保存脱敏标识，例如 ****1234 / ****5678"
          />
          <span class="field-hint">一份对账单有多个股东代码时，请把各尾号都填在这里。</span>
        </el-form-item>
        <el-form-item label="基础币种" prop="base_currency">
          <el-select v-model="accountForm.base_currency" aria-label="基础币种">
            <el-option
              v-for="currency in LEDGER_CURRENCIES"
              :key="currency"
              :label="currency"
              :value="currency"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="状态">
          <NCheckbox
            v-model:checked="accountForm.is_active"
            class="account-state-checkbox"
            :disabled="accountDialog.saving"
            :aria-disabled="accountDialog.saving"
            >启用账户</NCheckbox
          >
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="accountForm.notes"
            aria-label="账户备注"
            type="textarea"
            :rows="3"
            placeholder="不保存密码、完整账号或报表访问令牌"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="mobile-dialog-footer">
          <el-button :disabled="accountDialog.saving" @click="accountDialog.visible = false"
            >取消</el-button
          >
          <NButton
            type="primary"
            class="form-save-button"
            aria-label="保存"
            :loading="accountDialog.saving"
            :aria-disabled="accountDialog.saving"
            :aria-busy="accountDialog.saving"
            @click="saveAccount"
            >保存</NButton
          >
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.account-state-checkbox {
  min-height: 24px;
}

@media (max-width: 640px) {
  .account-state-checkbox {
    min-height: 44px;
  }
  .form-save-button {
    min-height: 44px;
    width: 100%;
  }
}
</style>
