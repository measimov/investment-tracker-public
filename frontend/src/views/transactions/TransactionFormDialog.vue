<script setup lang="ts">
import { ref, reactive } from 'vue'
import { ElMessage, type FormInstance } from 'element-plus'
import SecuritySelect from '@/components/SecuritySelect.vue'
import { useTransactionsStore, type Transaction } from '@/stores/transactions'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { toNumber } from '@/utils/helpers'
import {
  MARKETS,
  followMarketCurrency,
  freeTextFormPatch,
  resolvedFormPatch,
  securityFormPatch
} from '@/utils/securities'
import type { BrokerAccount, SecurityResolveResponse, SecuritySearchItem } from '@/types'
import { brokerAccountLabel } from './shared'

defineProps<{ brokerAccounts: BrokerAccount[]; brokerAccountsLoading: boolean }>()

// 保存成功后由壳层刷新列表（回到第一页并强制重取）
const emit = defineEmits<{ saved: [] }>()

const transactionsStore = useTransactionsStore()
const dialogVisible = ref(false)
const isEdit = ref(false)
const formRef = ref<FormInstance | null>(null)
const submitting = ref(false)

const form = reactive<{
  id?: number
  broker_account_id: number | null
  symbol: string
  name: string
  market: string
  transaction_type: string
  quantity: number
  price: number
  fee: number
  transaction_date: string
  currency: string
  notes: string
}>({
  broker_account_id: null,
  symbol: '',
  name: '',
  market: '',
  transaction_type: 'BUY',
  quantity: 0,
  price: 0,
  fee: 0,
  transaction_date: '',
  currency: 'CNY',
  notes: ''
})

const rules = {
  symbol: [{ required: true, message: '请输入股票代码', trigger: 'blur' }],
  market: [{ required: true, message: '请选择市场', trigger: 'change' }],
  transaction_type: [{ required: true, message: '请选择交易类型', trigger: 'change' }],
  quantity: [{ required: true, message: '请输入数量', trigger: 'blur' }],
  price: [{ required: true, message: '请输入价格', trigger: 'blur' }],
  // currency 可能被 securityFormPatch 清空（B股/加密货币推不出默认币种）
  currency: [{ required: true, message: '请选择币种', trigger: 'change' }],
  transaction_date: [{ required: true, message: '请选择交易日期', trigger: 'change' }]
}

/**
 * 标的选择：候选 = 账本（持仓/自选/历史，排前）∪ 标的全集（/securities/search）。
 * 选中即回填名称/市场/币种——市场与代码在同一动作里确定，配合后端的手工入口
 * 归一化（港股补零/大写），从源头堵住同券双键（3900 vs 03900）。
 * 手输未收录代码（新加坡股/加密货币/漏网 B 股）照样可提交；市场已知时后端按需
 * 解析名称/币种并只填空。三种补丁语义见 utils/securities.ts。
 */
function onSymbolSelected(item: SecuritySearchItem) {
  Object.assign(form, securityFormPatch(item))
  currencyAuto = true
}

// 币种是否仍是自动推导值（选候选 / 换市场 / 解析回填）：用户手选过一次就不再跟随市场。
// 只挂在两个 el-select 的 @change（用户动作）上，编辑回填/重置这类程序赋值不触发。
let currencyAuto = true
function onMarketChange(market: string) {
  // 自动态下推不出也要清空（评审 P1：默认 CNY 配加密货币会把 BTC 当 CNY 入账）
  form.currency = followMarketCurrency(form.currency, market, form.symbol, currencyAuto)
}

function onCurrencyChange() {
  currencyAuto = false
}

function onSymbolFreeText(payload: { symbol: string; lastPicked: SecuritySearchItem | null }) {
  Object.assign(form, freeTextFormPatch(form, payload))
}

function onSymbolResolved(result: SecurityResolveResponse) {
  Object.assign(form, resolvedFormPatch(form, result))
}

function resetForm() {
  delete form.id
  Object.assign(form, {
    broker_account_id: null,
    symbol: '',
    name: '',
    market: '',
    transaction_type: 'BUY',
    quantity: 0,
    price: 0,
    fee: 0,
    transaction_date: '',
    currency: 'CNY',
    notes: ''
  })
  formRef.value?.clearValidate()
}

function openAdd() {
  isEdit.value = false
  resetForm()
  currencyAuto = true
  dialogVisible.value = true
}

function openEdit(row: Transaction) {
  isEdit.value = true
  currencyAuto = false // 已有记录的币种是事实，不随市场重推
  Object.assign(form, {
    id: row.id,
    broker_account_id: row.broker_account_id || null,
    symbol: row.symbol,
    name: row.name || '',
    market: row.market,
    transaction_type: row.transaction_type,
    quantity: toNumber(row.quantity),
    price: toNumber(row.price),
    fee: toNumber(row.fee),
    transaction_date: row.transaction_date,
    currency: row.currency,
    notes: row.notes || ''
  })
  dialogVisible.value = true
}

async function handleSubmit() {
  const valid = await formRef.value?.validate()
  if (!valid) return

  submitting.value = true
  try {
    // 后端 TransactionCreate/Update 均为 extra="forbid"：payload 只能含
    // schema 字段——把编辑态残留的 form.id 一并提交会被 422 拒绝
    const payload = {
      broker_account_id: form.broker_account_id || null,
      symbol: form.symbol,
      name: form.name,
      market: form.market,
      transaction_type: form.transaction_type,
      quantity: form.quantity,
      price: form.price,
      fee: form.fee,
      transaction_date: form.transaction_date,
      currency: form.currency,
      notes: form.notes
    }
    if (isEdit.value) {
      await transactionsStore.updateTransaction(form.id as number, payload)
      ElMessage.success('更新成功')
    } else {
      await transactionsStore.createTransaction(payload)
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    emit('saved')
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, isEdit.value ? '更新失败' : '创建失败'))
  } finally {
    submitting.value = false
  }
}

defineExpose({ openAdd, openEdit })
</script>

<template>
  <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑交易' : '新增交易'" width="560px">
    <el-form :model="form" :rules="rules" ref="formRef" label-width="100px">
      <el-form-item label="券商账户">
        <el-select
          v-model="form.broker_account_id"
          clearable
          placeholder="可选；用于账户归属"
          :loading="brokerAccountsLoading"
        >
          <el-option
            v-for="account in brokerAccounts"
            :key="account.id"
            :label="brokerAccountLabel(account)"
            :value="account.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="股票代码" prop="symbol">
        <SecuritySelect
          v-model="form.symbol"
          :market="form.market"
          data-testid="symbol-autocomplete"
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
      <el-form-item label="交易类型" prop="transaction_type">
        <el-radio-group v-model="form.transaction_type">
          <el-radio value="BUY">买入</el-radio>
          <el-radio value="SELL">卖出</el-radio>
        </el-radio-group>
      </el-form-item>
      <!-- 不设 :precision：固定精度会把 1500 渲染成 1500.00000000；
           el-input-number 无 precision 时按用户实际输入的小数位显示 -->
      <el-form-item label="数量" prop="quantity">
        <el-input-number v-model="form.quantity" :min="0" :controls="false" class="amount-input" />
      </el-form-item>
      <el-form-item label="价格" prop="price">
        <el-input-number v-model="form.price" :min="0" :controls="false" class="amount-input" />
      </el-form-item>
      <el-form-item label="手续费" prop="fee">
        <el-input-number v-model="form.fee" :min="0" :precision="2" />
      </el-form-item>
      <el-form-item label="交易日期" prop="transaction_date">
        <el-date-picker
          v-model="form.transaction_date"
          type="date"
          placeholder="选择日期"
          value-format="YYYY-MM-DD"
        />
      </el-form-item>
      <el-form-item label="币种" prop="currency">
        <el-select v-model="form.currency" @change="onCurrencyChange">
          <el-option label="CNY (人民币)" value="CNY" />
          <el-option label="USD (美元)" value="USD" />
          <el-option label="HKD (港币)" value="HKD" />
          <el-option label="SGD (新加坡元)" value="SGD" />
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
.amount-input {
  width: 200px;
}
</style>
