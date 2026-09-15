<script setup lang="ts">
import { reactive, ref } from 'vue'
import { type FormInstance } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import api from '@/api'
import { useMediaQuery } from '@/composables/useMediaQuery'
import {
  type AccountRow,
  type DialogState,
  accountName,
  brokerOptions,
  currencyOptions,
  makeRemover,
  makeSaver
} from './shared'

const props = defineProps<{
  accounts: AccountRow[]
  loading: boolean
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
    account_name: row.account_name || row.name || '',
    broker: row.broker || row.broker_name || '',
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
}

const saveAccount = makeSaver({
  formRef: accountFormRef,
  dialog: accountDialog,
  buildPayload: () => ({ ...accountForm }),
  update: (id, payload) => api.updateBrokerAccount(id, payload),
  create: (payload) => api.createBrokerAccount(payload),
  messages: { updated: '账户已更新', created: '账户已新增', failure: '账户保存失败' },
  reload: () => props.reload()
})

const removeAccount = makeRemover<AccountRow>({
  title: '删除账户',
  message: (row) =>
    `仅空账户可以删除。若“${accountName(row)}”已有交易或审计记录，请编辑账户并将其停用。`,
  request: (row) => api.deleteBrokerAccount(row.id),
  successMessage: '账户已删除',
  failureMessage: '账户删除失败',
  reload: () => props.reload()
})
</script>

<template>
  <div>
    <div class="section-toolbar">
      <div>
        <h2>券商账户</h2>
        <p>为交易以及后续的账户级持仓、收益统计建立明确归属。</p>
      </div>
      <el-button type="primary" :icon="Plus" @click="openAccountDialog()">新增账户</el-button>
    </div>

    <div v-if="!isMobileView" class="responsive-table desktop-data-table">
      <el-table :data="accounts" v-loading="loading" stripe row-key="id">
        <template #empty>
          <el-empty description="尚未登记券商账户">
            <el-button type="primary" @click="openAccountDialog()">新增第一个账户</el-button>
          </el-empty>
        </template>
        <el-table-column label="账户" min-width="200">
          <template #default="{ row }">
            <div class="primary-cell">
              <strong>{{ accountName(row) }}</strong>
              <span>{{ row.account_number_masked || '未填写尾号' }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="broker" label="券商" min-width="140">
          <template #default="{ row }">{{ row.broker || row.broker_name || '-' }}</template>
        </el-table-column>
        <el-table-column prop="base_currency" label="基础币种" width="105" />
        <el-table-column prop="is_active" label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.is_active === false ? 'info' : 'success'" size="small">
              {{ row.is_active === false ? '停用' : '启用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="notes" label="备注" min-width="180" show-overflow-tooltip />
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" text @click="openAccountDialog(row)">编辑</el-button>
            <el-button type="danger" text @click="removeAccount(row)">删除空账户</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div v-else v-loading="loading" class="mobile-card-list">
      <el-empty v-if="!accounts.length" description="尚未登记券商账户" :image-size="88">
        <el-button type="primary" @click="openAccountDialog()">新增第一个账户</el-button>
      </el-empty>
      <article v-for="row in accounts" :key="row.id" class="mobile-card" data-testid="account-card">
        <div class="mobile-card-head">
          <div class="mobile-card-title">
            <span class="mobile-card-symbol">{{ accountName(row) }}</span>
            <span class="mobile-card-name">
              {{ row.account_number_masked || '未填写尾号' }}
            </span>
          </div>
          <div class="mobile-card-tags">
            <el-tag :type="row.is_active === false ? 'info' : 'success'" size="small">
              {{ row.is_active === false ? '停用' : '启用' }}
            </el-tag>
          </div>
        </div>

        <div class="mobile-card-meta">
          <span>{{ row.broker || row.broker_name || '未填写券商' }}</span>
          <span>{{ row.base_currency }}</span>
          <span v-if="row.notes">{{ row.notes }}</span>
        </div>

        <div class="mobile-card-actions">
          <el-button type="primary" size="small" text @click="openAccountDialog(row)">
            编辑
          </el-button>
          <el-button type="danger" size="small" text @click="removeAccount(row)">
            删除空账户
          </el-button>
        </div>
      </article>
    </div>

    <el-dialog
      v-model="accountDialog.visible"
      :title="accountDialog.id ? '编辑券商账户' : '新增券商账户'"
      width="560px"
    >
      <el-form ref="accountFormRef" :model="accountForm" :rules="accountRules" label-width="100px">
        <el-form-item label="账户名称" prop="account_name">
          <el-input v-model="accountForm.account_name" placeholder="例如：IBKR 主账户" />
        </el-form-item>
        <el-form-item label="券商" prop="broker">
          <!-- 浮层向上展开：默认向下会盖住尚未填写的「账户尾号」，快速录入/
               自动化对下一个字段的点击会落进浮层（选错项或输入丢失）。录入
               顺序自上而下，向上只盖住已填过的「账户名称」，无害。(#87) -->
          <el-select
            v-model="accountForm.broker"
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
            placeholder="只保存脱敏标识，例如 ****1234 / ****5678"
          />
          <span class="field-hint">一份对账单有多个股东代码时，请把各尾号都填在这里。</span>
        </el-form-item>
        <el-form-item label="基础币种" prop="base_currency">
          <el-select v-model="accountForm.base_currency">
            <el-option
              v-for="currency in currencyOptions"
              :key="currency"
              :label="currency"
              :value="currency"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="状态">
          <el-switch v-model="accountForm.is_active" active-text="启用" inactive-text="停用" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="accountForm.notes"
            type="textarea"
            :rows="3"
            placeholder="不保存密码、完整账号或报表访问令牌"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="mobile-dialog-footer">
          <el-button @click="accountDialog.visible = false">取消</el-button>
          <el-button type="primary" :loading="accountDialog.saving" @click="saveAccount"
            >保存</el-button
          >
        </div>
      </template>
    </el-dialog>
  </div>
</template>
