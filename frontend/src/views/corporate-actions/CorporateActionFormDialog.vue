<script setup lang="ts">
/**
 * 公司行动新增/编辑对话框（#284：由 RecordsTab 拆出）。按类型动态必填与字段映射；
 * 行 ↔ 表单 ↔ 请求体的换算是 shared.ts 的纯函数（formFromAction / payloadFromForm，有 spec）。
 */
import { computed, reactive, ref } from 'vue'
import { ElMessage, type FormInstance, type FormItemRule } from 'element-plus'
import api from '@/api'
import SecuritySelect from '@/components/SecuritySelect.vue'
import { useSecurityFormBinding } from '@/composables/useSecurityFormBinding'
import type { BrokerAccount, CorporateAction } from '@/types'
import { LEDGER_CURRENCY_OPTIONS } from '@/utils/currency'
import { formatCurrency } from '@/utils/helpers'
import { accountOptionLabel, ACTION_TYPE_LABELS, optionsOf } from '@/utils/labels'
import { MARKETS } from '@/utils/securities'
import { showApiError } from '@/utils/showApiError'
import {
  cashDividendAmounts,
  emptyActionForm,
  formFromAction,
  payloadFromForm,
  taxFromRate,
  TYPE_SPECIFIC_FIELDS_EMPTY,
  type CorporateActionForm
} from './shared'

defineProps<{ brokerAccounts: BrokerAccount[] }>()
const emit = defineEmits<{ saved: [] }>()

const actionTypeOptions = optionsOf(ACTION_TYPE_LABELS)
const dialogVisible = ref(false)
const isEdit = ref(false)
const formRef = ref<FormInstance | null>(null)
const submitting = ref(false)
const form = reactive<CorporateActionForm>(emptyActionForm())

// 按类型动态必填：现金股息必须有总额（统计/对账只读总额，只填每股 = 按 0 计）；
// 期初建仓必须有账户与数量（数量按账户桶加入，NULL 账户会落到「未指定账户」）
function requiredWhen(
  types: string[],
  isMissing: (value: unknown) => boolean,
  message: string
): FormItemRule {
  return {
    trigger: ['blur', 'change'],
    validator: (_rule, value, callback) => {
      if (types.includes(form.action_type) && isMissing(value)) callback(new Error(message))
      else callback()
    }
  }
}
const notPositive = (value: unknown) =>
  value === null || value === undefined || value === '' || !(Number(value) > 0)

const rules: Record<string, FormItemRule[]> = {
  symbol: [{ required: true, message: '请输入股票代码', trigger: 'blur' }],
  market: [{ required: true, message: '请选择市场', trigger: 'change' }],
  action_type: [{ required: true, message: '请选择行动类型', trigger: 'change' }],
  ex_date: [{ required: true, message: '请选择除权除息日', trigger: 'change' }],
  total_dividend: [requiredWhen(['CASH_DIVIDEND'], notPositive, '请输入股息总额（大于 0）')],
  broker_account_id: [
    requiredWhen(
      ['OPENING_POSITION'],
      (value) => value === null || value === undefined || value === '',
      '期初建仓必须选择账户'
    )
  ],
  opening_quantity: [requiredWhen(['OPENING_POSITION'], notPositive, '请输入数量（大于 0）')]
}

const netDividendPreview = computed(() =>
  form.total_dividend === null
    ? null
    : cashDividendAmounts({ total_dividend: form.total_dividend, tax_withheld: form.tax_withheld })
        .net
)

// 税率辅助：输入税率或改总额时按「总额 × 税率」回填税额
function applyTaxRate() {
  const tax = taxFromRate(form.total_dividend, form.tax_rate_percent)
  if (tax !== null) form.tax_withheld = tax
}

// 手工改税额后，与之不再对应的税率清空（避免存下自相矛盾的税率）
function onTaxWithheldInput() {
  const expected = taxFromRate(form.total_dividend, form.tax_rate_percent)
  if (expected !== null && expected !== form.tax_withheld) form.tax_rate_percent = null
}

const {
  onSymbolSelected,
  onMarketChange,
  onCurrencyChange,
  onSymbolFreeText,
  onSymbolResolved,
  setCurrencyAuto
} = useSecurityFormBinding(form)

function openAdd() {
  isEdit.value = false
  setCurrencyAuto(true)
  Object.assign(form, emptyActionForm())
  delete form.id
  formRef.value?.clearValidate()
  dialogVisible.value = true
}

function openEdit(row: CorporateAction) {
  isEdit.value = true
  setCurrencyAuto(false) // 已有记录的币种是事实
  Object.assign(form, formFromAction(row))
  dialogVisible.value = true
}

function handleActionTypeChange() {
  // 清空特定类型的字段
  Object.assign(form, TYPE_SPECIFIC_FIELDS_EMPTY)
  formRef.value?.clearValidate(['total_dividend', 'broker_account_id', 'opening_quantity'])
}

async function handleSubmit() {
  const valid = await formRef.value?.validate()
  if (!valid) return

  submitting.value = true
  try {
    const payload = payloadFromForm(form, { isEdit: isEdit.value })
    if (isEdit.value) {
      await api.updateCorporateAction(form.id as number, payload)
      ElMessage.success('更新成功')
    } else {
      await api.createCorporateAction(payload)
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    emit('saved')
  } catch (error) {
    showApiError(error, isEdit.value ? '更新失败' : '创建失败')
  } finally {
    submitting.value = false
  }
}

defineExpose({ openAdd, openEdit })
</script>

<template>
  <el-dialog
    v-model="dialogVisible"
    :title="isEdit ? '编辑公司行动' : '新增公司行动'"
    width="720px"
  >
    <el-form :model="form" :rules="rules" ref="formRef" label-width="100px">
      <!-- 基本信息 -->
      <el-divider content-position="left">基本信息</el-divider>

      <el-form-item
        label="券商账户"
        prop="broker_account_id"
        :required="form.action_type === 'OPENING_POSITION'"
      >
        <el-select
          v-model="form.broker_account_id"
          :placeholder="
            form.action_type === 'OPENING_POSITION'
              ? '必选：建仓数量加入该账户'
              : '可选；历史记录请按实际来源归属'
          "
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

      <el-form-item label="股票代码" prop="symbol">
        <SecuritySelect
          v-model="form.symbol"
          :market="form.market"
          placeholder="如: 600000, AAPL；支持名称/拼音检索"
          @select="onSymbolSelected"
          @free-text="onSymbolFreeText"
          @resolved="onSymbolResolved"
        />
      </el-form-item>

      <el-form-item label="名称" prop="name">
        <el-input v-model="form.name" placeholder="资产名称" />
      </el-form-item>

      <el-form-item label="市场" prop="market">
        <el-select v-model="form.market" placeholder="选择市场" @change="onMarketChange">
          <el-option v-for="m in MARKETS" :key="m" :label="m" :value="m" />
        </el-select>
      </el-form-item>

      <el-form-item label="行动类型" prop="action_type">
        <el-select
          v-model="form.action_type"
          placeholder="选择类型"
          @change="handleActionTypeChange"
        >
          <el-option
            v-for="item in actionTypeOptions"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
      </el-form-item>

      <el-form-item label="除权除息日" prop="ex_date">
        <el-date-picker
          v-model="form.ex_date"
          type="date"
          placeholder="选择日期"
          value-format="YYYY-MM-DD"
        />
      </el-form-item>

      <!-- 现金股息专用字段 -->
      <template v-if="form.action_type === 'CASH_DIVIDEND'">
        <el-divider content-position="left">现金股息</el-divider>

        <el-form-item label="每股股息" prop="dividend_per_share">
          <el-input-number v-model="form.dividend_per_share" :min="0" />
        </el-form-item>

        <el-form-item label="股息总额" prop="total_dividend" required>
          <el-input-number
            v-model="form.total_dividend"
            :min="0"
            :precision="2"
            @change="applyTaxRate"
          />
          <div class="form-tip">税前总额；统计与对账按总额计入，只填每股不会计入任何金额</div>
        </el-form-item>

        <el-form-item label="预扣税额" prop="tax_withheld">
          <div class="tax-inputs">
            <el-input-number
              v-model="form.tax_withheld"
              :min="0"
              :precision="2"
              placeholder="0"
              @change="onTaxWithheldInput"
            />
            <span class="tax-rate-label">或按税率</span>
            <el-input-number
              v-model="form.tax_rate_percent"
              :min="0"
              :max="100"
              :precision="2"
              :controls="false"
              placeholder="%"
              class="tax-rate-input"
              @change="applyTaxRate"
            />
            <span>%</span>
          </div>
          <div class="form-tip">
            填实际被预扣的税额；输入税率会按「股息总额 × 税率」算出税额。A
            股券商到账通常为税前全额，税额留空即为 0。
          </div>
        </el-form-item>

        <el-form-item label="税后净额">
          <span class="net-preview">
            {{
              netDividendPreview === null
                ? '—'
                : formatCurrency(netDividendPreview, form.currency || 'CNY')
            }}
          </span>
        </el-form-item>
      </template>

      <!-- 股票股息/送股专用字段 -->
      <template v-if="form.action_type === 'STOCK_DIVIDEND' || form.action_type === 'BONUS_ISSUE'">
        <el-divider content-position="left">股票股息</el-divider>

        <el-form-item label="获得股数" prop="shares_received">
          <el-input-number v-model="form.shares_received" :min="0" />
        </el-form-item>

        <el-form-item label="分配比例" prop="distribution_ratio">
          <el-input v-model="form.distribution_ratio" placeholder="如: 10:1 表示每10股送1股" />
        </el-form-item>
      </template>

      <!-- 配股专用字段 -->
      <template v-if="form.action_type === 'RIGHTS_ISSUE'">
        <el-divider content-position="left">配股信息</el-divider>

        <el-form-item label="认购价格" prop="subscription_price">
          <el-input-number v-model="form.subscription_price" :min="0" />
        </el-form-item>

        <el-form-item label="认购数量" prop="subscription_quantity">
          <el-input-number v-model="form.subscription_quantity" :min="0" />
        </el-form-item>

        <el-form-item label="配股比例" prop="distribution_ratio">
          <el-input v-model="form.distribution_ratio" placeholder="如: 10:2 表示每10股配2股" />
        </el-form-item>
      </template>

      <!-- 拆股/合股专用字段 -->
      <template v-if="form.action_type === 'STOCK_SPLIT' || form.action_type === 'REVERSE_SPLIT'">
        <el-divider content-position="left">拆股/合股</el-divider>

        <el-form-item label="拆分比例" prop="split_ratio">
          <el-input
            v-model="form.split_ratio"
            placeholder="如: 1:2 表示1股拆成2股, 10:1 表示10股合成1股"
          />
        </el-form-item>
      </template>

      <!-- 期初建仓专用字段（#174） -->
      <template v-if="form.action_type === 'OPENING_POSITION'">
        <el-divider content-position="left">期初建仓</el-divider>

        <el-form-item label="数量" prop="opening_quantity" required>
          <el-input-number v-model="form.opening_quantity" :min="0" />
        </el-form-item>

        <el-form-item label="单位成本" prop="opening_cost_per_share">
          <el-input-number v-model="form.opening_cost_per_share" :min="0" />
        </el-form-item>

        <el-form-item label="总成本" prop="opening_total_cost">
          <el-input-number v-model="form.opening_total_cost" :min="0" :precision="2" />
          <div class="form-tip">两个成本都留空 = 成本未知，持仓成本与已实现盈亏将标记为估计值</div>
        </el-form-item>
      </template>

      <!-- 通用字段 -->
      <el-divider content-position="left">其他信息</el-divider>

      <el-form-item label="币种" prop="currency">
        <el-select v-model="form.currency" @change="onCurrencyChange">
          <el-option
            v-for="option in LEDGER_CURRENCY_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
      </el-form-item>

      <el-form-item label="备注" prop="notes">
        <el-input v-model="form.notes" type="textarea" :rows="3" placeholder="备注信息" />
      </el-form-item>
    </el-form>

    <template #footer>
      <div class="mobile-dialog-footer">
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleSubmit" :loading="submitting">确定</el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.form-tip {
  width: 100%;
  font-size: 12px;
  color: var(--app-text-soft);
  margin-top: 5px;
}

.tax-inputs {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.tax-rate-label {
  color: var(--app-text-soft);
  font-size: 13px;
}

.tax-rate-input {
  width: 96px;
}

.net-preview {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}
</style>
