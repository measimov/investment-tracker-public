<template>
  <div class="corporate-actions-page">
    <el-card>
      <template #header>
        <div class="page-header">
          <span>公司行动管理</span>
          <div class="header-actions">
            <el-button type="primary" @click="handleAdd">
              <el-icon><Plus /></el-icon>
              新增记录
            </el-button>
          </div>
        </div>
      </template>

      <!-- Filters -->
      <el-form :inline="true" class="filter-form">
        <el-form-item label="代码">
          <el-input
            v-model="filters.symbol"
            placeholder="股票代码"
            clearable
            @clear="handleSearch"
            @keyup.enter="handleSearch"
          />
        </el-form-item>
        <el-form-item label="市场">
          <el-select v-model="filters.market" placeholder="选择市场" clearable @change="handleSearch" @clear="handleSearch">
            <el-option label="A股" value="A股" />
            <el-option label="B股" value="B股" />
            <el-option label="港股" value="港股" />
            <el-option label="美股" value="美股" />
            <el-option label="新加坡股" value="新加坡股" />
          </el-select>
        </el-form-item>
        <el-form-item label="类型">
          <el-select v-model="filters.action_type" placeholder="行动类型" clearable @change="handleSearch" @clear="handleSearch">
            <el-option label="现金股息" value="CASH_DIVIDEND" />
            <el-option label="股票股息" value="STOCK_DIVIDEND" />
            <el-option label="配股" value="RIGHTS_ISSUE" />
            <el-option label="拆股" value="STOCK_SPLIT" />
            <el-option label="合股" value="REVERSE_SPLIT" />
            <el-option label="送股" value="BONUS_ISSUE" />
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
      <el-row :gutter="20" class="stats-row" v-if="summary">
        <el-col :xs="12" :md="6">
          <el-statistic title="总记录数" :value="summary.total_count" />
        </el-col>
        <el-col :xs="12" :md="6">
          <el-statistic title="股息总额" :value="summary.cash_dividends?.total_dividend || 0" :precision="2" suffix="元" />
        </el-col>
        <el-col :xs="12" :md="6">
          <el-statistic title="预扣税" :value="summary.cash_dividends?.total_tax || 0" :precision="2" suffix="元" />
        </el-col>
        <el-col :xs="12" :md="6">
          <el-statistic title="税后净额" :value="summary.cash_dividends?.net_dividend || 0" :precision="2" suffix="元" />
        </el-col>
      </el-row>

      <!-- Table -->
      <div class="responsive-table">
        <el-table :data="actions" v-loading="loading" stripe row-key="id" max-height="560">
          <el-table-column prop="ex_date" label="除权除息日" width="120" sortable>
            <template #default="{ row }">
              {{ formatDate(row.ex_date) }}
            </template>
          </el-table-column>
          <el-table-column prop="symbol" label="代码" width="100" />
          <el-table-column prop="name" label="名称" width="120" />
          <el-table-column prop="market" label="市场" width="100" />
          <el-table-column prop="action_type" label="类型" width="120">
            <template #default="{ row }">
              <el-tag :type="getActionTypeTag(row.action_type)" size="small">
                {{ getActionTypeName(row.action_type) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="详情" min-width="220">
            <template #default="{ row }">
              <div v-if="row.action_type === 'CASH_DIVIDEND'">
                每股: {{ formatNumber(row.dividend_per_share, 4) }}
                | 总额: {{ formatNumber(row.total_dividend, 2) }}
                | 税后: {{ formatNumber(row.net_dividend, 2) }}
              </div>
              <div v-else-if="row.action_type === 'STOCK_DIVIDEND' || row.action_type === 'BONUS_ISSUE'">
                获得股数: {{ formatNumber(row.shares_received, 2) }}
                | 比例: {{ row.distribution_ratio }}
              </div>
              <div v-else-if="row.action_type === 'RIGHTS_ISSUE'">
                认购价: {{ formatNumber(row.subscription_price, 2) }}
                | 数量: {{ formatNumber(row.subscription_quantity, 2) }}
              </div>
              <div v-else-if="row.action_type === 'STOCK_SPLIT' || row.action_type === 'REVERSE_SPLIT'">
                拆分比例: {{ row.split_ratio }}
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="notes" label="备注" min-width="150" show-overflow-tooltip />
          <el-table-column label="操作" width="150">
            <template #default="{ row }">
              <el-button type="primary" size="small" text @click="handleEdit(row)">编辑</el-button>
              <el-button type="danger" size="small" text @click="handleDelete(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
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
    </el-card>

    <!-- Form Dialog -->
    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? '编辑公司行动' : '新增公司行动'"
      width="700px"
    >
      <el-form :model="form" :rules="rules" ref="formRef" label-width="120px">
        <!-- 基本信息 -->
        <el-divider content-position="left">基本信息</el-divider>

        <el-form-item label="股票代码" prop="symbol">
          <el-input v-model="form.symbol" placeholder="如: 600000, AAPL" />
        </el-form-item>

        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" placeholder="资产名称" />
        </el-form-item>

        <el-form-item label="市场" prop="market">
          <el-select v-model="form.market" placeholder="选择市场" style="width: 100%">
            <el-option label="A股" value="A股" />
            <el-option label="B股" value="B股" />
            <el-option label="港股" value="港股" />
            <el-option label="美股" value="美股" />
            <el-option label="新加坡股" value="新加坡股" />
          </el-select>
        </el-form-item>

        <el-form-item label="行动类型" prop="action_type">
          <el-select v-model="form.action_type" placeholder="选择类型" style="width: 100%" @change="handleActionTypeChange">
            <el-option label="现金股息" value="CASH_DIVIDEND" />
            <el-option label="股票股息/红股" value="STOCK_DIVIDEND" />
            <el-option label="配股" value="RIGHTS_ISSUE" />
            <el-option label="拆股" value="STOCK_SPLIT" />
            <el-option label="合股" value="REVERSE_SPLIT" />
            <el-option label="送股" value="BONUS_ISSUE" />
          </el-select>
        </el-form-item>

        <el-form-item label="除权除息日" prop="ex_date">
          <el-date-picker
            v-model="form.ex_date"
            type="date"
            placeholder="选择日期"
            style="width: 100%"
            value-format="YYYY-MM-DD"
          />
        </el-form-item>

        <!-- 现金股息专用字段 -->
        <template v-if="form.action_type === 'CASH_DIVIDEND'">
          <el-divider content-position="left">现金股息</el-divider>

          <el-form-item label="每股股息" prop="dividend_per_share">
            <el-input-number v-model="form.dividend_per_share" :min="0" :precision="8" style="width: 100%" />
          </el-form-item>

          <el-form-item label="股息总额" prop="total_dividend">
            <el-input-number v-model="form.total_dividend" :min="0" :precision="2" style="width: 100%" />
          </el-form-item>

          <el-form-item label="税率 (%)" prop="tax_rate">
            <el-input-number v-model="form.tax_rate_percent" :min="0" :max="100" :precision="2" style="width: 100%" />
            <div class="form-tip">常见税率：10% (红利税), 20% (利息税)</div>
          </el-form-item>
        </template>

        <!-- 股票股息/送股专用字段 -->
        <template v-if="form.action_type === 'STOCK_DIVIDEND' || form.action_type === 'BONUS_ISSUE'">
          <el-divider content-position="left">股票股息</el-divider>

          <el-form-item label="获得股数" prop="shares_received">
            <el-input-number v-model="form.shares_received" :min="0" :precision="2" style="width: 100%" />
          </el-form-item>

          <el-form-item label="分配比例" prop="distribution_ratio">
            <el-input v-model="form.distribution_ratio" placeholder="如: 10:1 表示每10股送1股" />
          </el-form-item>
        </template>

        <!-- 配股专用字段 -->
        <template v-if="form.action_type === 'RIGHTS_ISSUE'">
          <el-divider content-position="left">配股信息</el-divider>

          <el-form-item label="认购价格" prop="subscription_price">
            <el-input-number v-model="form.subscription_price" :min="0" :precision="4" style="width: 100%" />
          </el-form-item>

          <el-form-item label="认购数量" prop="subscription_quantity">
            <el-input-number v-model="form.subscription_quantity" :min="0" :precision="2" style="width: 100%" />
          </el-form-item>

          <el-form-item label="配股比例" prop="distribution_ratio">
            <el-input v-model="form.distribution_ratio" placeholder="如: 10:2 表示每10股配2股" />
          </el-form-item>
        </template>

        <!-- 拆股/合股专用字段 -->
        <template v-if="form.action_type === 'STOCK_SPLIT' || form.action_type === 'REVERSE_SPLIT'">
          <el-divider content-position="left">拆股/合股</el-divider>

          <el-form-item label="拆分比例" prop="split_ratio">
            <el-input v-model="form.split_ratio" placeholder="如: 1:2 表示1股拆成2股, 10:1 表示10股合成1股" />
          </el-form-item>
        </template>

        <!-- 通用字段 -->
        <el-divider content-position="left">其他信息</el-divider>

        <el-form-item label="币种" prop="currency">
          <el-select v-model="form.currency" style="width: 100%">
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
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'
import { formatNumber, formatDate } from '../utils/helpers'

const loading = ref(false)
const actions = ref([])
const summary = ref(null)
const dialogVisible = ref(false)
const isEdit = ref(false)
const formRef = ref(null)
const submitting = ref(false)

const filters = reactive({
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

const form = reactive({
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

const actionTypeNames = {
  'CASH_DIVIDEND': '现金股息',
  'STOCK_DIVIDEND': '股票股息',
  'RIGHTS_ISSUE': '配股',
  'STOCK_SPLIT': '拆股',
  'REVERSE_SPLIT': '合股',
  'BONUS_ISSUE': '送股'
}

const actionTypeTags = {
  'CASH_DIVIDEND': 'success',
  'STOCK_DIVIDEND': 'warning',
  'RIGHTS_ISSUE': 'info',
  'STOCK_SPLIT': 'primary',
  'REVERSE_SPLIT': 'primary',
  'BONUS_ISSUE': 'warning'
}

function getActionTypeName(type) {
  return actionTypeNames[type] || type
}

function getActionTypeTag(type) {
  return actionTypeTags[type] || ''
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
    ElMessage.error('加载公司行动记录失败')
  } finally {
    loading.value = false
  }

  loadSummary(baseParams)
}

function buildQueryParams() {
  const params = {}
  const symbol = filters.symbol.trim()

  if (symbol) params.symbol = symbol
  if (filters.market) params.market = filters.market
  if (filters.action_type) params.action_type = filters.action_type
  if (filters.date_range?.length === 2) {
    params.start_date = filters.date_range[0]
    params.end_date = filters.date_range[1]
  }

  return params
}

function handleSearch() {
  pagination.page = 1
  loadActions()
}

function handlePageSizeChange() {
  pagination.page = 1
  loadActions()
}

async function loadSummary(params = {}) {
  try {
    const response = await api.getCorporateActionsSummary(params)
    summary.value = response.data
  } catch (error) {
    summary.value = null
    console.error('加载统计失败', error)
  }
}

function resetFilters() {
  filters.symbol = ''
  filters.market = ''
  filters.action_type = ''
  filters.date_range = []
  handleSearch()
}

function handleAdd() {
  isEdit.value = false
  resetForm()
  dialogVisible.value = true
}

function handleEdit(row) {
  isEdit.value = true
  Object.assign(form, {
    id: row.id,
    symbol: row.symbol,
    name: row.name || '',
    market: row.market,
    action_type: row.action_type,
    ex_date: row.ex_date,
    dividend_per_share: row.dividend_per_share ? parseFloat(row.dividend_per_share) : null,
    total_dividend: row.total_dividend ? parseFloat(row.total_dividend) : null,
    tax_rate_percent: row.tax_rate ? parseFloat(row.tax_rate) * 100 : 10,
    shares_received: row.shares_received ? parseFloat(row.shares_received) : null,
    distribution_ratio: row.distribution_ratio || '',
    subscription_price: row.subscription_price ? parseFloat(row.subscription_price) : null,
    subscription_quantity: row.subscription_quantity ? parseFloat(row.subscription_quantity) : null,
    split_ratio: row.split_ratio || '',
    currency: row.currency || 'CNY',
    notes: row.notes || ''
  })
  dialogVisible.value = true
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
}

async function handleSubmit() {
  const valid = await formRef.value.validate()
  if (!valid) return

  submitting.value = true
  try {
    // 准备提交数据
    const submitData = {
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
    }

    if (isEdit.value) {
      await api.updateCorporateAction(form.id, submitData)
      ElMessage.success('更新成功')
    } else {
      await api.createCorporateAction(submitData)
      ElMessage.success('创建成功')
    }

    dialogVisible.value = false
    loadActions()
  } catch (error) {
    ElMessage.error(isEdit.value ? '更新失败' : '创建失败')
    console.error(error)
  } finally {
    submitting.value = false
  }
}

function handleDelete(row) {
  ElMessageBox.confirm('确定要删除这条公司行动记录吗？', '提示', {
    confirmButtonText: '确定',
    cancelButtonText: '取消',
    type: 'warning'
  }).then(async () => {
    try {
      await api.deleteCorporateAction(row.id)
      ElMessage.success('删除成功')
      loadActions()
    } catch (error) {
      ElMessage.error('删除失败')
    }
  })
}

function resetForm() {
  Object.assign(form, {
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

onMounted(() => {
  loadActions()
})
</script>

<style scoped>
.corporate-actions-page {
  width: 100%;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.header-actions {
  display: flex;
  gap: 10px;
}

.filter-form {
  margin-bottom: 20px;
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
  color: #909399;
  margin-top: 5px;
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
