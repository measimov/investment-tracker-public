<script setup lang="ts">
import { Plus } from '@element-plus/icons-vue'
import { ref, reactive } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance } from 'element-plus'
import api from '@/api'
import SecuritySelect from '@/components/SecuritySelect.vue'
import { useHoldingsStore } from '@/stores/holdings'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { getApiErrorMessage } from '@/utils/apiErrors'
import type {
  BrokerAccount,
  CorporateAction,
  SecurityResolveResponse,
  SecuritySearchItem
} from '@/types'
import { formatNumber, formatDate, toNumber } from '@/utils/helpers'
import {
  MARKETS,
  followMarketCurrency,
  freeTextFormPatch,
  resolvedFormPatch,
  securityFormPatch
} from '@/utils/securities'
import {
  brokerAccountLabel,
  brokerAccountLabelById as labelById,
  getActionTypeName,
  getActionTypeTag,
  openingPositionCostKnown
} from './shared'

// 后端 schema 为准（生成类型；Decimal 序列化为 string，展示经 toNumber）
type CorporateActionRow = CorporateAction

interface ActionsSummary {
  total_count?: number
  cash_dividends?: {
    total_dividend?: number
    total_tax?: number
    net_dividend?: number
    missing_rate_currencies?: string[]
  } | null
  [key: string]: unknown
}

const props = defineProps<{ brokerAccounts: BrokerAccount[] }>()

const isMobileView = useMediaQuery('(max-width: 640px)')
const loading = ref(false)
const holdingsStore = useHoldingsStore()
const actions = ref<CorporateActionRow[]>([])
const summary = ref<ActionsSummary | null>(null)
const dialogVisible = ref(false)
const isEdit = ref(false)
const formRef = ref<FormInstance | null>(null)
const submitting = ref(false)

const filters = reactive<{
  account: '' | 'unassigned' | number
  symbol: string
  market: string
  action_type: string
  date_range: string[]
}>({
  account: '',
  symbol: '',
  market: '',
  action_type: '',
  date_range: []
})

const pagination = reactive({
  page: 1,
  pageSize: 50,
  total: 0
})

const form = reactive<{
  id?: number
  broker_account_id: number | null
  symbol: string
  name: string
  market: string
  action_type: string
  ex_date: string
  dividend_per_share: number | null
  total_dividend: number | null
  tax_rate_percent: number
  shares_received: number | null
  distribution_ratio: string
  subscription_price: number | null
  subscription_quantity: number | null
  split_ratio: string
  opening_quantity: number | null
  opening_cost_per_share: number | null
  opening_total_cost: number | null
  currency: string
  notes: string
}>({
  broker_account_id: null,
  symbol: '',
  name: '',
  market: '',
  action_type: '',
  ex_date: '',
  // 现金股息
  dividend_per_share: null,
  total_dividend: null,
  tax_rate_percent: 10, // 显示用，实际提交时转换为小数
  // 股票股息
  shares_received: null,
  distribution_ratio: '',
  // 配股
  subscription_price: null,
  subscription_quantity: null,
  // 拆股
  split_ratio: '',
  // 期初建仓（#174）：数量必填，两个成本可选，都空 = 成本未知
  opening_quantity: null,
  opening_cost_per_share: null,
  opening_total_cost: null,
  // 通用
  currency: 'CNY',
  notes: ''
})

const rules = {
  symbol: [{ required: true, message: '请输入股票代码', trigger: 'blur' }],
  market: [{ required: true, message: '请选择市场', trigger: 'change' }],
  action_type: [{ required: true, message: '请选择行动类型', trigger: 'change' }],
  ex_date: [{ required: true, message: '请选择除权除息日', trigger: 'change' }]
}

function brokerAccountLabelById(accountId: number | null | undefined) {
  return labelById(props.brokerAccounts, accountId)
}

// 详情文案：桌面表格与移动卡片共用一份。
// 后端 CorporateActionCreate 允许若干字段二选一（送股=比例或绝对股数、
// 拆股=比例或拆后股数），所以这里只拼存在的字段——直接模板插值会把
// 合法的"只填绝对股数"记录显示成 `比例: null`，还会丢掉唯一有效的值。
// 字段顺序与 portfolio/semantics.py 的优先级一致：决定复算的比例在前。
function actionDetail(row: Record<string, unknown>): string {
  const num = (value: unknown, precision: number) => formatNumber(value as number, precision)
  const has = (value: unknown) => value !== null && value !== undefined && value !== ''
  const parts: string[] = []

  switch (row.action_type) {
    case 'CASH_DIVIDEND':
      if (has(row.dividend_per_share)) parts.push(`每股: ${num(row.dividend_per_share, 4)}`)
      if (has(row.total_dividend)) parts.push(`总额: ${num(row.total_dividend, 2)}`)
      if (has(row.net_dividend)) parts.push(`税后: ${num(row.net_dividend, 2)}`)
      break
    case 'STOCK_DIVIDEND':
    case 'BONUS_ISSUE':
      if (has(row.distribution_ratio)) parts.push(`比例: ${row.distribution_ratio}`)
      if (has(row.shares_received)) parts.push(`获得股数: ${num(row.shares_received, 2)}`)
      break
    case 'RIGHTS_ISSUE':
      if (has(row.subscription_price)) parts.push(`认购价: ${num(row.subscription_price, 2)}`)
      if (has(row.subscription_quantity)) parts.push(`数量: ${num(row.subscription_quantity, 2)}`)
      break
    case 'STOCK_SPLIT':
    case 'REVERSE_SPLIT':
      if (has(row.split_ratio)) parts.push(`拆分比例: ${row.split_ratio}`)
      if (has(row.new_shares)) parts.push(`拆后股数: ${num(row.new_shares, 2)}`)
      break
    case 'OPENING_POSITION':
      if (has(row.adjusted_quantity)) parts.push(`数量: ${num(row.adjusted_quantity, 2)}`)
      if (has(row.cost_basis_adjustment)) parts.push(`总成本: ${num(row.cost_basis_adjustment, 2)}`)
      else if (has(row.adjusted_cost_per_share))
        parts.push(`单位成本: ${num(row.adjusted_cost_per_share, 4)}`)
      else parts.push('成本未知')
      break
  }

  return parts.length ? parts.join(' | ') : '-'
}

async function loadActions() {
  loading.value = true
  const baseParams = buildQueryParams()
  const listParams = {
    ...baseParams,
    skip: (pagination.page - 1) * pagination.pageSize,
    limit: pagination.pageSize
  }

  try {
    const [listResponse, countResponse] = await Promise.all([
      api.getCorporateActions(listParams),
      api.getCorporateActionsCount(baseParams)
    ])
    actions.value = listResponse.data
    pagination.total = countResponse.data.total || 0
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '加载公司行动记录失败'))
  } finally {
    loading.value = false
  }

  loadSummary(baseParams)
}

function buildQueryParams() {
  const params: Record<string, unknown> = {}
  const symbol = filters.symbol.trim()

  if (symbol) params.symbol = symbol
  if (filters.account === 'unassigned') {
    params.unassigned_account = true
  } else if (filters.account) {
    params.broker_account_id = filters.account
  }
  if (filters.market) params.market = filters.market
  if (filters.action_type) params.action_type = filters.action_type
  if (filters.date_range?.length === 2) {
    params.start_date = filters.date_range[0]
    params.end_date = filters.date_range[1]
  }

  return params
}

// 筛选框选中候选：代码与市场一起定，否则 00700 配 A股 筛选查空
function onFilterSymbolSelected(item: SecuritySearchItem) {
  filters.market = item.market
  handleSearch()
}

function onSymbolSelected(item: SecuritySearchItem) {
  Object.assign(form, securityFormPatch(item))
  currencyAuto = true
}

// 币种仍是自动推导值时跟随市场变化；用户手选过一次就不再跟随（同交易表单）
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

function handleSearch() {
  pagination.page = 1
  loadActions()
}

function handlePageSizeChange() {
  pagination.page = 1
  loadActions()
}

async function loadSummary(params: Record<string, unknown> = {}) {
  try {
    const response = await api.getCorporateActionsSummary(params)
    summary.value = response.data
  } catch (error) {
    summary.value = null
    console.error('加载统计失败', error)
  }
}

function resetFilters() {
  filters.account = ''
  filters.symbol = ''
  filters.market = ''
  filters.action_type = ''
  filters.date_range = []
  handleSearch()
}

function handleAdd() {
  isEdit.value = false
  currencyAuto = true
  resetForm()
  dialogVisible.value = true
}

function handleEdit(row: CorporateActionRow) {
  isEdit.value = true
  currencyAuto = false // 已有记录的币种是事实
  Object.assign(form, {
    id: row.id,
    broker_account_id: row.broker_account_id || null,
    symbol: row.symbol,
    name: row.name || '',
    market: row.market,
    action_type: row.action_type,
    ex_date: row.ex_date,
    dividend_per_share: row.dividend_per_share ? toNumber(row.dividend_per_share) : null,
    total_dividend: row.total_dividend ? toNumber(row.total_dividend) : null,
    tax_rate_percent: row.tax_rate ? toNumber(row.tax_rate) * 100 : 10,
    shares_received: row.shares_received ? toNumber(row.shares_received) : null,
    distribution_ratio: row.distribution_ratio || '',
    subscription_price: row.subscription_price ? toNumber(row.subscription_price) : null,
    subscription_quantity: row.subscription_quantity ? toNumber(row.subscription_quantity) : null,
    split_ratio: row.split_ratio || '',
    opening_quantity: row.adjusted_quantity ? toNumber(row.adjusted_quantity) : null,
    opening_cost_per_share: row.adjusted_cost_per_share
      ? toNumber(row.adjusted_cost_per_share)
      : null,
    opening_total_cost: row.cost_basis_adjustment ? toNumber(row.cost_basis_adjustment) : null,
    currency: row.currency || 'CNY',
    notes: row.notes || ''
  })
  dialogVisible.value = true
}

// 期初建仓补录成本：导入建的行动整体只读，但成本必须有通道（#174）
const costDialogVisible = ref(false)
const costSubmitting = ref(false)
const costForm = reactive<{
  id: number | null
  symbol: string
  quantity: number | null
  cost_per_share: number | null
  total_cost: number | null
  notes: string
}>({ id: null, symbol: '', quantity: null, cost_per_share: null, total_cost: null, notes: '' })

function handleBackfillCost(row: CorporateAction) {
  costForm.id = row.id
  costForm.symbol = row.symbol
  costForm.quantity = row.adjusted_quantity ? toNumber(row.adjusted_quantity) : null
  costForm.cost_per_share = row.adjusted_cost_per_share
    ? toNumber(row.adjusted_cost_per_share)
    : null
  costForm.total_cost = row.cost_basis_adjustment ? toNumber(row.cost_basis_adjustment) : null
  costForm.notes = row.notes || ''
  costDialogVisible.value = true
}

async function handleSubmitCost() {
  if (costForm.id === null) return
  if (costForm.cost_per_share === null && costForm.total_cost === null) {
    ElMessage.warning('请填写单位成本或总成本')
    return
  }
  costSubmitting.value = true
  try {
    await api.updateOpeningPositionCost(costForm.id, {
      adjusted_cost_per_share: costForm.cost_per_share,
      cost_basis_adjustment: costForm.total_cost,
      notes: costForm.notes
    })
    ElMessage.success('成本已补录，持仓已重算')
    costDialogVisible.value = false
    loadActions()
  } catch (error) {
    ElMessage.error('补录失败：' + getApiErrorMessage(error))
  } finally {
    costSubmitting.value = false
  }
}

function handleActionTypeChange() {
  // 清空特定类型的字段
  form.dividend_per_share = null
  form.total_dividend = null
  form.shares_received = null
  form.distribution_ratio = ''
  form.subscription_price = null
  form.subscription_quantity = null
  form.split_ratio = ''
  form.opening_quantity = null
  form.opening_cost_per_share = null
  form.opening_total_cost = null
}

async function handleSubmit() {
  const valid = await formRef.value?.validate()
  if (!valid) return

  submitting.value = true
  try {
    // 准备提交数据
    const submitData: Record<string, unknown> = {
      broker_account_id: form.broker_account_id || null,
      symbol: form.symbol,
      name: form.name,
      market: form.market,
      action_type: form.action_type,
      ex_date: form.ex_date,
      currency: form.currency,
      notes: form.notes
    }

    // 根据类型添加特定字段
    if (form.action_type === 'CASH_DIVIDEND') {
      submitData.dividend_per_share = form.dividend_per_share
      submitData.total_dividend = form.total_dividend
      submitData.tax_rate = form.tax_rate_percent / 100 // 转换为小数
    } else if (form.action_type === 'STOCK_DIVIDEND' || form.action_type === 'BONUS_ISSUE') {
      submitData.shares_received = form.shares_received
      submitData.distribution_ratio = form.distribution_ratio
    } else if (form.action_type === 'RIGHTS_ISSUE') {
      submitData.subscription_price = form.subscription_price
      submitData.subscription_quantity = form.subscription_quantity
      submitData.distribution_ratio = form.distribution_ratio
    } else if (form.action_type === 'STOCK_SPLIT' || form.action_type === 'REVERSE_SPLIT') {
      submitData.split_ratio = form.split_ratio
    } else if (form.action_type === 'OPENING_POSITION') {
      submitData.adjusted_quantity = form.opening_quantity
      submitData.adjusted_cost_per_share = form.opening_cost_per_share
      submitData.cost_basis_adjustment = form.opening_total_cost
    }

    if (isEdit.value) {
      await api.updateCorporateAction(form.id as number, submitData)
      ElMessage.success('更新成功')
    } else {
      await api.createCorporateAction(submitData)
      ElMessage.success('创建成功')
    }

    holdingsStore.invalidate()
    dialogVisible.value = false
    loadActions()
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, isEdit.value ? '更新失败' : '创建失败'))
    console.error(error)
  } finally {
    submitting.value = false
  }
}

function handleDelete(row: CorporateActionRow) {
  ElMessageBox.confirm('确定要删除这条公司行动记录吗？', '提示', {
    confirmButtonText: '确定',
    cancelButtonText: '取消',
    type: 'warning'
  }).then(async () => {
    try {
      await api.deleteCorporateAction(row.id)
      holdingsStore.invalidate()
      ElMessage.success('删除成功')
      loadActions()
    } catch (error) {
      ElMessage.error(getApiErrorMessage(error, '删除失败'))
    }
  })
}

function resetForm() {
  Object.assign(form, {
    broker_account_id: null,
    symbol: '',
    name: '',
    market: '',
    action_type: '',
    ex_date: '',
    dividend_per_share: null,
    total_dividend: null,
    tax_rate_percent: 10,
    shares_received: null,
    distribution_ratio: '',
    subscription_price: null,
    subscription_quantity: null,
    split_ratio: '',
    currency: 'CNY',
    notes: ''
  })
  formRef.value?.clearValidate()
}

// 跨 tab 刷新入口：分红建议"接受"入账后由壳层调用（新记录要出现在列表里）
defineExpose({ reload: loadActions })
</script>

<template>
  <el-card>
    <template #header>
      <div class="page-header">
        <span>公司行动管理</span>
        <div class="header-actions">
          <el-button type="primary" :icon="Plus" @click="handleAdd">新增记录</el-button>
        </div>
      </div>
    </template>

    <!-- Filters -->
    <el-form :inline="true" class="filter-form">
      <el-form-item label="账户">
        <el-select
          v-model="filters.account"
          placeholder="全部账户"
          clearable
          @change="handleSearch"
          @clear="handleSearch"
        >
          <el-option label="未分配账户" value="unassigned" />
          <el-option
            v-for="account in brokerAccounts"
            :key="account.id"
            :label="brokerAccountLabel(account)"
            :value="account.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="代码">
        <SecuritySelect
          v-model="filters.symbol"
          :resolve="false"
          placeholder="股票代码"
          class="filter-symbol"
          @select="onFilterSymbolSelected"
          @clear="handleSearch"
          @keyup.enter="handleSearch"
        />
      </el-form-item>
      <el-form-item label="市场">
        <el-select
          v-model="filters.market"
          placeholder="选择市场"
          clearable
          @change="handleSearch"
          @clear="handleSearch"
        >
          <el-option v-for="m in MARKETS" :key="m" :label="m" :value="m" />
        </el-select>
      </el-form-item>
      <el-form-item label="类型">
        <el-select
          v-model="filters.action_type"
          placeholder="行动类型"
          clearable
          @change="handleSearch"
          @clear="handleSearch"
        >
          <el-option label="现金股息" value="CASH_DIVIDEND" />
          <el-option label="股票股息" value="STOCK_DIVIDEND" />
          <el-option label="配股" value="RIGHTS_ISSUE" />
          <el-option label="拆股" value="STOCK_SPLIT" />
          <el-option label="合股" value="REVERSE_SPLIT" />
          <el-option label="送股" value="BONUS_ISSUE" />
          <el-option label="期初建仓/转托管转入" value="OPENING_POSITION" />
        </el-select>
      </el-form-item>
      <el-form-item label="日期">
        <el-date-picker
          v-model="filters.date_range"
          type="daterange"
          value-format="YYYY-MM-DD"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          range-separator="至"
          clearable
          @change="handleSearch"
          @clear="handleSearch"
        />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="handleSearch">查询</el-button>
        <el-button @click="resetFilters">重置</el-button>
      </el-form-item>
    </el-form>

    <!-- Statistics Summary -->
    <el-alert
      v-if="summary?.cash_dividends?.missing_rate_currencies?.length"
      type="warning"
      :closable="false"
      show-icon
      class="stats-alert"
      :title="`缺少 ${summary.cash_dividends.missing_rate_currencies.join('/')} 汇率，对应股息未计入 CNY 折算总额，请先在汇率页补录`"
    />
    <el-row :gutter="20" class="stats-row" v-if="summary">
      <el-col :xs="12" :md="6">
        <el-statistic title="总记录数" :value="summary.total_count" />
      </el-col>
      <el-col :xs="12" :md="6">
        <el-statistic
          title="股息总额（CNY折算）"
          :value="summary.cash_dividends?.total_dividend || 0"
          :precision="2"
          prefix="¥"
        />
      </el-col>
      <el-col :xs="12" :md="6">
        <el-statistic
          title="预扣税（CNY折算）"
          :value="summary.cash_dividends?.total_tax || 0"
          :precision="2"
          prefix="¥"
        />
      </el-col>
      <el-col :xs="12" :md="6">
        <el-statistic
          title="税后净额（CNY折算）"
          :value="summary.cash_dividends?.net_dividend || 0"
          :precision="2"
          prefix="¥"
        />
      </el-col>
    </el-row>

    <!-- Table -->
    <div v-if="!isMobileView" class="responsive-table desktop-data-table">
      <el-table :data="actions" v-loading="loading" stripe row-key="id" max-height="560">
        <template #empty>
          <el-empty description="暂无公司行动记录" :image-size="88" />
        </template>
        <el-table-column prop="ex_date" label="除权除息日" width="120" sortable>
          <template #default="{ row }">
            {{ formatDate(row.ex_date) }}
          </template>
        </el-table-column>
        <el-table-column prop="symbol" label="代码" width="100" />
        <el-table-column prop="name" label="名称" width="120" />
        <el-table-column prop="market" label="市场" width="100" />
        <el-table-column label="账户" min-width="150">
          <template #default="{ row }">
            <span :class="{ 'account-unassigned': !row.broker_account_id }">
              {{ brokerAccountLabelById(row.broker_account_id) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="action_type" label="类型" width="120">
          <template #default="{ row }">
            <el-tag :type="getActionTypeTag(row.action_type)" size="small">
              {{ getActionTypeName(row.action_type) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="详情" min-width="220">
          <template #default="{ row }">{{ actionDetail(row) }}</template>
        </el-table-column>
        <el-table-column prop="notes" label="备注" min-width="150" show-overflow-tooltip />
        <el-table-column label="操作" width="170">
          <template #default="{ row }">
            <template v-if="row.import_batch_id">
              <el-tag type="info" effect="plain" size="small">导入只读</el-tag>
              <el-button
                v-if="row.action_type === 'OPENING_POSITION' && !openingPositionCostKnown(row)"
                type="warning"
                size="small"
                text
                @click="handleBackfillCost(row)"
              >
                补录成本
              </el-button>
            </template>
            <template v-else>
              <el-button type="primary" size="small" text @click="handleEdit(row)">
                编辑
              </el-button>
              <el-button type="danger" size="small" text @click="handleDelete(row)">
                删除
              </el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div v-else v-loading="loading" class="mobile-card-list">
      <el-empty v-if="!actions.length" description="暂无公司行动记录" :image-size="88" />
      <article
        v-for="row in actions"
        :key="row.id"
        class="mobile-card"
        data-testid="corporate-action-card"
      >
        <div class="mobile-card-head">
          <div class="mobile-card-title">
            <span class="mobile-card-symbol">{{ row.symbol }}</span>
            <span class="mobile-card-name">{{ row.name || row.market }}</span>
          </div>
          <div class="mobile-card-tags">
            <el-tag :type="getActionTypeTag(row.action_type)" size="small">
              {{ getActionTypeName(row.action_type) }}
            </el-tag>
          </div>
        </div>

        <div class="action-detail">{{ actionDetail(row) }}</div>

        <div class="mobile-card-meta">
          <span>除权除息日 {{ formatDate(row.ex_date) }}</span>
          <span>{{ row.market }}</span>
          <span :class="{ 'account-unassigned': !row.broker_account_id }">
            {{ brokerAccountLabelById(row.broker_account_id) }}
          </span>
          <span v-if="row.notes">{{ row.notes }}</span>
        </div>

        <div class="mobile-card-actions">
          <template v-if="row.import_batch_id">
            <el-tag type="info" effect="plain" size="small">导入只读</el-tag>
            <el-button
              v-if="row.action_type === 'OPENING_POSITION' && !openingPositionCostKnown(row)"
              type="warning"
              size="small"
              text
              @click="handleBackfillCost(row)"
            >
              补录成本
            </el-button>
          </template>
          <template v-else>
            <el-button type="primary" size="small" text @click="handleEdit(row)">编辑</el-button>
            <el-button type="danger" size="small" text @click="handleDelete(row)">删除</el-button>
          </template>
        </div>
      </article>
    </div>

    <div class="table-pagination">
      <el-pagination
        v-model:current-page="pagination.page"
        v-model:page-size="pagination.pageSize"
        :total="pagination.total"
        :page-sizes="[25, 50, 100, 200]"
        layout="total, sizes, prev, pager, next, jumper"
        background
        @size-change="handlePageSizeChange"
        @current-change="loadActions"
      />
    </div>

    <!-- Form Dialog -->
    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? '编辑公司行动' : '新增公司行动'"
      width="720px"
    >
      <el-form :model="form" :rules="rules" ref="formRef" label-width="100px">
        <!-- 基本信息 -->
        <el-divider content-position="left">基本信息</el-divider>

        <el-form-item label="券商账户">
          <el-select
            v-model="form.broker_account_id"
            placeholder="可选；历史记录请按实际来源归属"
            clearable
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
            <el-option label="现金股息" value="CASH_DIVIDEND" />
            <el-option label="股票股息/红股" value="STOCK_DIVIDEND" />
            <el-option label="配股" value="RIGHTS_ISSUE" />
            <el-option label="拆股" value="STOCK_SPLIT" />
            <el-option label="合股" value="REVERSE_SPLIT" />
            <el-option label="送股" value="BONUS_ISSUE" />
            <el-option label="期初建仓/转托管转入" value="OPENING_POSITION" />
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
            <el-input-number v-model="form.dividend_per_share" :min="0" :precision="8" />
          </el-form-item>

          <el-form-item label="股息总额" prop="total_dividend">
            <el-input-number v-model="form.total_dividend" :min="0" :precision="2" />
          </el-form-item>

          <el-form-item label="税率 (%)" prop="tax_rate">
            <el-input-number v-model="form.tax_rate_percent" :min="0" :max="100" :precision="2" />
            <div class="form-tip">常见税率：10% (红利税), 20% (利息税)</div>
          </el-form-item>
        </template>

        <!-- 股票股息/送股专用字段 -->
        <template
          v-if="form.action_type === 'STOCK_DIVIDEND' || form.action_type === 'BONUS_ISSUE'"
        >
          <el-divider content-position="left">股票股息</el-divider>

          <el-form-item label="获得股数" prop="shares_received">
            <el-input-number v-model="form.shares_received" :min="0" :precision="2" />
          </el-form-item>

          <el-form-item label="分配比例" prop="distribution_ratio">
            <el-input v-model="form.distribution_ratio" placeholder="如: 10:1 表示每10股送1股" />
          </el-form-item>
        </template>

        <!-- 配股专用字段 -->
        <template v-if="form.action_type === 'RIGHTS_ISSUE'">
          <el-divider content-position="left">配股信息</el-divider>

          <el-form-item label="认购价格" prop="subscription_price">
            <el-input-number v-model="form.subscription_price" :min="0" :precision="4" />
          </el-form-item>

          <el-form-item label="认购数量" prop="subscription_quantity">
            <el-input-number v-model="form.subscription_quantity" :min="0" :precision="2" />
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

          <el-form-item label="数量" prop="opening_quantity">
            <el-input-number v-model="form.opening_quantity" :min="0" :precision="4" />
          </el-form-item>

          <el-form-item label="单位成本" prop="opening_cost_per_share">
            <el-input-number v-model="form.opening_cost_per_share" :min="0" :precision="4" />
          </el-form-item>

          <el-form-item label="总成本" prop="opening_total_cost">
            <el-input-number v-model="form.opening_total_cost" :min="0" :precision="2" />
            <div class="form-tip">
              账户必选；两个成本都留空 = 成本未知，持仓成本与已实现盈亏将标记为估计值
            </div>
          </el-form-item>
        </template>

        <!-- 通用字段 -->
        <el-divider content-position="left">其他信息</el-divider>

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

    <!-- 期初建仓补录成本（#174）：导入建的行动只开放成本字段 -->
    <el-dialog
      v-model="costDialogVisible"
      title="补录期初建仓成本"
      :width="isMobileView ? '95%' : '480px'"
      :fullscreen="isMobileView"
    >
      <el-alert
        type="info"
        :closable="false"
        show-icon
        class="cost-dialog-tip"
        :title="`${costForm.symbol} 数量 ${formatNumber(costForm.quantity, 4)}，来自对账单，不可改`"
        description="填写单位成本或总成本其一即可；两者都填时须一致。保存后持仓与已实现盈亏立即重算。"
      />
      <el-form :model="costForm" label-width="100px" label-position="top">
        <el-form-item label="单位成本">
          <el-input-number v-model="costForm.cost_per_share" :min="0" :precision="4" />
        </el-form-item>
        <el-form-item label="总成本">
          <el-input-number v-model="costForm.total_cost" :min="0" :precision="2" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="costForm.notes" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="mobile-dialog-footer">
          <el-button @click="costDialogVisible = false">取消</el-button>
          <el-button type="primary" @click="handleSubmitCost" :loading="costSubmitting">
            保存并重算
          </el-button>
        </div>
      </template>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.cost-dialog-tip {
  margin-bottom: 16px;
}

.stats-alert {
  margin-bottom: 14px;
}

.filter-form {
  margin-bottom: 20px;
}

.action-detail {
  color: var(--app-text);
  font-size: 13px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.stats-row {
  margin-bottom: 20px;
}

.table-pagination {
  display: flex;
  justify-content: flex-end;
  padding-top: 16px;
}

.form-tip {
  font-size: 12px;
  color: var(--app-text-soft);
  margin-top: 5px;
}

.account-unassigned {
  color: var(--app-warning);
}

@media (max-width: 900px) {
  .header-actions {
    width: 100%;
  }

  .table-pagination {
    justify-content: flex-start;
  }

  .stats-row :deep(.el-col) {
    margin-bottom: 12px;
  }
}
</style>
