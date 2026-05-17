<template>
  <div class="transactions-page">
    <el-card class="transactions-card">
      <template #header>
        <div class="page-header">
          <span>交易记录管理</span>
          <div class="header-actions">
            <el-button type="success" @click="showImportDialog = true">
              <el-icon><Upload /></el-icon>
              导入
            </el-button>
            <el-button type="primary" @click="handleExport">
              <el-icon><Download /></el-icon>
              导出
            </el-button>
            <el-button type="primary" @click="handleAdd">
              <el-icon><Plus /></el-icon>
              新增交易
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
            <el-option label="加密货币" value="加密货币" />
          </el-select>
        </el-form-item>
        <el-form-item label="类型">
          <el-select v-model="filters.transaction_type" placeholder="交易类型" clearable @change="handleSearch" @clear="handleSearch">
            <el-option label="买入" value="BUY" />
            <el-option label="卖出" value="SELL" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="handleSearch">查询</el-button>
          <el-button @click="resetFilters">重置</el-button>
        </el-form-item>
      </el-form>

      <!-- Table -->
      <div v-if="!isMobileView" class="responsive-table desktop-data-table">
        <el-table
          :data="transactions"
          v-loading="loading"
          stripe
          row-key="id"
          max-height="560"
        >
          <el-table-column prop="transaction_date" label="交易日期" width="120" sortable>
            <template #default="{ row }">
              {{ formatDate(row.transaction_date) }}
            </template>
          </el-table-column>
          <el-table-column prop="symbol" label="代码" width="100" />
          <el-table-column prop="name" label="名称" width="120" />
          <el-table-column prop="market" label="市场" width="100" />
          <el-table-column prop="transaction_type" label="类型" width="80">
            <template #default="{ row }">
              <el-tag :type="row.transaction_type === 'BUY' ? 'success' : 'danger'" size="small">
                {{ row.transaction_type === 'BUY' ? '买入' : '卖出' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="quantity" label="数量" width="100" align="right">
            <template #default="{ row }">
              {{ formatNumber(row.quantity, 4) }}
            </template>
          </el-table-column>
          <el-table-column prop="price" label="价格" width="100" align="right">
            <template #default="{ row }">
              {{ formatNumber(row.price, 4) }}
            </template>
          </el-table-column>
          <el-table-column prop="fee" label="手续费" width="100" align="right">
            <template #default="{ row }">
              {{ formatNumber(row.fee, 2) }}
            </template>
          </el-table-column>
          <el-table-column prop="currency" label="币种" width="80" />
          <el-table-column prop="notes" label="备注" min-width="150" show-overflow-tooltip />
          <el-table-column label="操作" width="150">
            <template #default="{ row }">
              <el-button type="primary" size="small" text @click="handleEdit(row)">编辑</el-button>
              <el-button type="danger" size="small" text @click="handleDelete(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div v-else v-loading="loading" class="mobile-transaction-list">
        <article v-for="row in transactions" :key="row.id" class="mobile-transaction-card">
          <div class="mobile-row-head">
            <div class="asset-title">
              <span class="asset-symbol">{{ row.symbol }}</span>
              <span class="asset-name">{{ row.name || row.market }}</span>
            </div>
            <el-tag :type="row.transaction_type === 'BUY' ? 'success' : 'danger'" size="small">
              {{ row.transaction_type === 'BUY' ? '买入' : '卖出' }}
            </el-tag>
          </div>

          <div class="transaction-amount">
            <span>{{ formatDate(row.transaction_date) }} · {{ row.market }} · {{ row.currency }}</span>
            <strong>{{ formatNumber(row.quantity, 4) }} × {{ formatNumber(row.price, 4) }}</strong>
          </div>

          <div class="mobile-transaction-meta">
            <span>手续费 {{ formatNumber(row.fee, 2) }}</span>
            <span v-if="row.notes">{{ row.notes }}</span>
          </div>

          <div class="mobile-card-actions">
            <el-button type="primary" size="small" text @click="handleEdit(row)">编辑</el-button>
            <el-button type="danger" size="small" text @click="handleDelete(row)">删除</el-button>
          </div>
        </article>
      </div>

      <div class="pagination-bar">
        <span class="pagination-info">
          共 {{ pagination.total }} 条，每页只渲染当前页以提升性能
        </span>
        <el-pagination
          v-model:current-page="pagination.page"
          v-model:page-size="pagination.pageSize"
          :page-sizes="[25, 50, 100, 200]"
          :total="pagination.total"
          layout="sizes, prev, pager, next, jumper"
          background
          @size-change="handlePageSizeChange"
          @current-change="loadTransactions"
        />
      </div>
    </el-card>

    <!-- Transaction Form Dialog -->
    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? '编辑交易' : '新增交易'"
      width="600px"
    >
      <el-form :model="form" :rules="rules" ref="formRef" label-width="100px">
        <el-form-item label="股票代码" prop="symbol">
          <el-input v-model="form.symbol" placeholder="如: AAPL, 600000" />
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
            <el-option label="加密货币" value="加密货币" />
          </el-select>
        </el-form-item>
        <el-form-item label="交易类型" prop="transaction_type">
          <el-radio-group v-model="form.transaction_type">
            <el-radio label="BUY">买入</el-radio>
            <el-radio label="SELL">卖出</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="数量" prop="quantity">
          <el-input-number v-model="form.quantity" :min="0" :precision="8" style="width: 100%" />
        </el-form-item>
        <el-form-item label="价格" prop="price">
          <el-input-number v-model="form.price" :min="0" :precision="8" style="width: 100%" />
        </el-form-item>
        <el-form-item label="手续费" prop="fee">
          <el-input-number v-model="form.fee" :min="0" :precision="2" style="width: 100%" />
        </el-form-item>
        <el-form-item label="交易日期" prop="transaction_date">
          <el-date-picker
            v-model="form.transaction_date"
            type="date"
            placeholder="选择日期"
            style="width: 100%"
            value-format="YYYY-MM-DD"
          />
        </el-form-item>
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

    <!-- Import Dialog -->
    <el-dialog v-model="showImportDialog" title="导入交易记录" width="640px">
      <el-tabs v-model="importMode" class="import-tabs">
        <el-tab-pane label="标准交易文件" name="standard" />
        <el-tab-pane label="招商证券资金流水" name="cmb" />
        <el-tab-pane label="IBKR 活动报表" name="ibkr" />
        <el-tab-pane label="东方财富对账单" name="eastmoney" />
      </el-tabs>
      <el-upload
        drag
        :auto-upload="false"
        :on-change="handleFileChange"
        :on-remove="handleFileRemove"
        :limit="1"
        :accept="importAccept"
      >
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">
          拖拽文件到此处或 <em>点击上传</em>
        </div>
        <template #tip>
          <div v-if="importMode === 'standard'" class="el-upload__tip">
            支持 CSV 或 Excel 文件，需包含以下列：symbol, market, transaction_type, quantity, price, transaction_date
          </div>
          <div v-else class="el-upload__tip">
            <span v-if="importMode === 'cmb'">
              支持招商证券导出的历史资金流水，仅导入“证券买入”和“证券卖出”，重复流水会自动跳过
            </span>
            <span v-else-if="importMode === 'ibkr'">
              支持 IBKR Activity Statement CSV，导入普通股票/ETF买卖、股息和外国预扣税；期权、外汇、利息暂不导入
            </span>
            <span v-else>
              支持已解密的东方财富 PDF 股票明细对账单，仅导入股票买卖、红利入账和红利税；基金、逆回购、银证流水暂不导入
            </span>
          </div>
        </template>
      </el-upload>

      <div v-if="brokerPreview" class="import-preview">
        <el-descriptions :column="3" border size="small" class="responsive-descriptions">
          <el-descriptions-item label="券商">{{ brokerPreview.broker }}</el-descriptions-item>
          <el-descriptions-item label="总行数">{{ brokerPreview.total_rows }}</el-descriptions-item>
          <el-descriptions-item label="买卖记录">{{ brokerPreview.eligible_trade_rows }}</el-descriptions-item>
          <el-descriptions-item label="股息分红">{{ brokerPreview.eligible_dividend_rows }}</el-descriptions-item>
          <el-descriptions-item label="红利税">{{ brokerPreview.eligible_tax_rows }}</el-descriptions-item>
          <el-descriptions-item label="重复跳过">{{ brokerPreview.duplicate_rows }}</el-descriptions-item>
          <el-descriptions-item label="非买卖跳过">{{ brokerPreview.skipped_non_trade_rows }}</el-descriptions-item>
          <el-descriptions-item label="无效跳过">{{ brokerPreview.skipped_invalid_rows }}</el-descriptions-item>
          <el-descriptions-item v-if="importMode === 'ibkr'" label="期权跳过">{{ brokerPreview.skipped_option_rows }}</el-descriptions-item>
          <el-descriptions-item v-if="importMode === 'ibkr'" label="外汇跳过">{{ brokerPreview.skipped_fx_rows }}</el-descriptions-item>
          <el-descriptions-item v-if="importMode === 'ibkr'" label="现金类跳过">{{ brokerPreview.skipped_cash_rows }}</el-descriptions-item>
          <el-descriptions-item v-if="importMode === 'eastmoney'" label="基金跳过">{{ brokerPreview.skipped_cash_rows }}</el-descriptions-item>
          <el-descriptions-item v-if="importMode === 'eastmoney'" label="不支持跳过">{{ brokerPreview.skipped_unsupported_rows }}</el-descriptions-item>
          <el-descriptions-item label="日期范围">{{ brokerPreview.date_start }} ~ {{ brokerPreview.date_end }}</el-descriptions-item>
        </el-descriptions>

        <el-alert
          v-if="brokerPreview.duplicate_rows > 0"
          class="preview-alert"
          type="warning"
          :closable="false"
          show-icon
          :title="`发现 ${brokerPreview.duplicate_rows} 条重复流水，正式导入时会跳过`"
        />
        <el-alert
          v-else
          class="preview-alert"
          type="success"
          :closable="false"
          show-icon
          title="未发现重复的买卖流水"
        />
      </div>
      <template #footer>
        <div class="mobile-dialog-footer">
        <el-button @click="showImportDialog = false">取消</el-button>
        <el-button
          v-if="importMode !== 'standard' && !brokerPreview"
          type="primary"
          @click="handleImportPreview"
          :loading="importing"
        >
          预览
        </el-button>
        <el-button v-else type="primary" @click="handleImport" :loading="importing">导入</el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, ref, reactive, watch, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'
import { formatNumber, formatDate, downloadFile } from '../utils/helpers'

const loading = ref(false)
const transactions = ref([])
const pagination = reactive({
  page: 1,
  pageSize: 50,
  total: 0
})
const dialogVisible = ref(false)
const isEdit = ref(false)
const formRef = ref(null)
const submitting = ref(false)
const showImportDialog = ref(false)
const importing = ref(false)
const uploadFile = ref(null)
const importMode = ref('standard')
const brokerPreview = ref(null)
const isMobileView = ref(false)
const importAccept = computed(() => {
  if (importMode.value === 'ibkr') return '.csv'
  if (importMode.value === 'eastmoney') return '.pdf'
  return '.csv,.xlsx,.xls'
})

function updateResponsiveState() {
  isMobileView.value = window.matchMedia('(max-width: 640px)').matches
}

const filters = reactive({
  symbol: '',
  market: '',
  transaction_type: ''
})

const form = reactive({
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
  transaction_date: [{ required: true, message: '请选择交易日期', trigger: 'change' }]
}

async function loadTransactions() {
  loading.value = true
  try {
    const params = buildQueryParams()
    params.skip = (pagination.page - 1) * pagination.pageSize
    params.limit = pagination.pageSize

    const [transactionsRes, countRes] = await Promise.all([
      api.getTransactions(params),
      api.getTransactionsCount(buildQueryParams())
    ])

    transactions.value = transactionsRes.data
    pagination.total = countRes.data.total || 0

    const maxPage = Math.max(1, Math.ceil(pagination.total / pagination.pageSize))
    if (pagination.page > maxPage) {
      pagination.page = maxPage
      await loadTransactions()
    }
  } catch (error) {
    ElMessage.error('加载交易记录失败')
  } finally {
    loading.value = false
  }
}

function buildQueryParams() {
  const params = {}
  if (filters.symbol) params.symbol = filters.symbol
  if (filters.market) params.market = filters.market
  if (filters.transaction_type) params.transaction_type = filters.transaction_type
  return params
}

function handleSearch() {
  pagination.page = 1
  loadTransactions()
}

function handlePageSizeChange() {
  pagination.page = 1
  loadTransactions()
}

function resetFilters() {
  filters.symbol = ''
  filters.market = ''
  filters.transaction_type = ''
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
    transaction_type: row.transaction_type,
    quantity: parseFloat(row.quantity),
    price: parseFloat(row.price),
    fee: parseFloat(row.fee),
    transaction_date: row.transaction_date,
    currency: row.currency,
    notes: row.notes || ''
  })
  dialogVisible.value = true
}

async function handleSubmit() {
  const valid = await formRef.value.validate()
  if (!valid) return

  submitting.value = true
  try {
    if (isEdit.value) {
      await api.updateTransaction(form.id, form)
      ElMessage.success('更新成功')
    } else {
      await api.createTransaction(form)
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    handleSearch()
  } catch (error) {
    ElMessage.error(isEdit.value ? '更新失败' : '创建失败')
  } finally {
    submitting.value = false
  }
}

function handleDelete(row) {
  ElMessageBox.confirm('确定要删除这条交易记录吗？', '提示', {
    confirmButtonText: '确定',
    cancelButtonText: '取消',
    type: 'warning'
  }).then(async () => {
    try {
      await api.deleteTransaction(row.id)
      ElMessage.success('删除成功')
      loadTransactions()
    } catch (error) {
      ElMessage.error('删除失败')
    }
  })
}

async function handleExport() {
  try {
    const response = await api.exportExcel()
    downloadFile(response.data, `transactions_${new Date().toISOString().split('T')[0]}.xlsx`)
    ElMessage.success('导出成功')
  } catch (error) {
    ElMessage.error('导出失败')
  }
}

function handleFileChange(file) {
  uploadFile.value = file.raw
  brokerPreview.value = null
}

function handleFileRemove() {
  uploadFile.value = null
  brokerPreview.value = null
}

watch(importMode, () => {
  uploadFile.value = null
  brokerPreview.value = null
})

async function handleImportPreview() {
  if (!uploadFile.value) {
    ElMessage.warning('请选择文件')
    return
  }

  importing.value = true
  try {
    let response
    if (importMode.value === 'ibkr') {
      response = await api.previewIbkrActivity(uploadFile.value)
    } else if (importMode.value === 'eastmoney') {
      response = await api.previewEastmoneyStatement(uploadFile.value)
    } else {
      response = await api.previewCmbFundFlows(uploadFile.value)
    }
    brokerPreview.value = response.data
    ElMessage.success('预览完成')
  } catch (error) {
    ElMessage.error('预览失败：' + (error.response?.data?.detail || error.message))
  } finally {
    importing.value = false
  }
}

async function handleImport() {
  if (!uploadFile.value) {
    ElMessage.warning('请选择文件')
    return
  }

  importing.value = true
  try {
    let response
    if (importMode.value === 'cmb') {
      response = await api.importCmbFundFlows(uploadFile.value)
      ElMessage.success(
        `导入交易 ${response.data.imported_transactions} 条，` +
        `公司行动 ${response.data.imported_corporate_actions} 条，` +
        `红利税调整 ${response.data.imported_tax_adjustments} 条，` +
        `跳过重复 ${response.data.duplicate_rows} 条`
      )
    } else if (importMode.value === 'ibkr') {
      response = await api.importIbkrActivity(uploadFile.value)
      ElMessage.success(
        `导入交易 ${response.data.imported_transactions} 条，` +
        `公司行动 ${response.data.imported_corporate_actions} 条，` +
        `预扣税调整 ${response.data.imported_tax_adjustments} 条，` +
        `跳过重复 ${response.data.duplicate_rows} 条`
      )
    } else if (importMode.value === 'eastmoney') {
      response = await api.importEastmoneyStatement(uploadFile.value)
      ElMessage.success(
        `导入交易 ${response.data.imported_transactions} 条，` +
        `公司行动 ${response.data.imported_corporate_actions} 条，` +
        `红利税调整 ${response.data.imported_tax_adjustments} 条，` +
        `跳过重复 ${response.data.duplicate_rows} 条`
      )
    } else {
      const isExcel = uploadFile.value.name.endsWith('.xlsx') || uploadFile.value.name.endsWith('.xls')
      response = isExcel
        ? await api.importExcel(uploadFile.value)
        : await api.importCSV(uploadFile.value)
      ElMessage.success(response.data.message)
    }

    showImportDialog.value = false
    uploadFile.value = null
    brokerPreview.value = null
    loadTransactions()
  } catch (error) {
    ElMessage.error('导入失败：' + (error.response?.data?.detail || error.message))
  } finally {
    importing.value = false
  }
}

function resetForm() {
  Object.assign(form, {
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

onMounted(() => {
  updateResponsiveState()
  window.addEventListener('resize', updateResponsiveState)
  loadTransactions()
})

onUnmounted(() => {
  window.removeEventListener('resize', updateResponsiveState)
})
</script>

<style scoped>
.transactions-page {
  width: 100%;
}

.transactions-card {
  overflow: hidden;
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

.pagination-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding-top: 16px;
}

.pagination-info {
  color: var(--app-text-muted);
  font-size: 13px;
  white-space: nowrap;
}

:deep(.filter-form .el-form-item__label) {
  color: var(--app-text-muted);
  font-weight: 650;
}

:deep(.el-table .el-button.is-text) {
  padding-inline: 4px;
}

.import-tabs {
  margin-bottom: 12px;
}

.import-preview {
  margin-top: 16px;
}

.preview-alert {
  margin-top: 12px;
}

.mobile-transaction-list {
  display: none;
}

@media (max-width: 900px) {
  .header-actions {
    width: 100%;
  }

  .pagination-bar {
    align-items: flex-start;
    flex-direction: column;
  }

  .pagination-info {
    white-space: normal;
  }

  :deep(.el-upload-dragger) {
    width: 100%;
  }

  :deep(.responsive-descriptions .el-descriptions__body) {
    overflow-x: auto;
  }
}

@media (max-width: 640px) {
  .desktop-data-table {
    display: none;
  }

  .mobile-transaction-list {
    display: grid;
    gap: 12px;
  }

  .mobile-transaction-card {
    padding: 14px;
    background: var(--app-surface);
    border: 1px solid var(--app-border-soft);
    border-radius: 8px;
    box-shadow: 0 2px 10px rgba(15, 23, 42, 0.04);
  }

  .mobile-row-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }

  .asset-title {
    min-width: 0;
  }

  .asset-symbol {
    display: block;
    color: var(--app-text);
    font-size: 18px;
    font-weight: 760;
    line-height: 1.2;
  }

  .asset-name {
    display: block;
    margin-top: 3px;
    color: var(--app-text-muted);
    font-size: 13px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .transaction-amount {
    display: grid;
    gap: 6px;
    margin-top: 12px;
  }

  .transaction-amount span,
  .mobile-transaction-meta {
    color: var(--app-text-muted);
    font-size: 12px;
  }

  .transaction-amount strong {
    color: var(--app-text);
    font-size: 17px;
    line-height: 1.25;
  }

  .mobile-transaction-meta {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    margin-top: 8px;
  }

  .mobile-card-actions {
    display: flex;
    justify-content: flex-end;
    gap: 6px;
    margin-top: 10px;
    padding-top: 8px;
    border-top: 1px solid var(--app-border-soft);
  }
}
</style>
