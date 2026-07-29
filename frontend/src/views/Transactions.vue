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
          <el-select
            v-model="filters.market"
            placeholder="选择市场"
            clearable
            @change="handleSearch"
            @clear="handleSearch"
          >
            <el-option label="A股" value="A股" />
            <el-option label="B股" value="B股" />
            <el-option label="港股" value="港股" />
            <el-option label="美股" value="美股" />
            <el-option label="新加坡股" value="新加坡股" />
            <el-option label="加密货币" value="加密货币" />
          </el-select>
        </el-form-item>
        <el-form-item label="类型">
          <el-select
            v-model="filters.transaction_type"
            placeholder="交易类型"
            clearable
            @change="handleSearch"
            @clear="handleSearch"
          >
            <el-option label="买入" value="BUY" />
            <el-option label="卖出" value="SELL" />
            <el-option label="转出" value="TRANSFER_OUT" />
            <el-option label="转入" value="TRANSFER_IN" />
          </el-select>
        </el-form-item>
        <el-form-item label="账户">
          <el-select
            v-model="filters.account"
            placeholder="全部账户"
            clearable
            @change="handleSearch"
            @clear="handleSearch"
          >
            <el-option label="未分配" value="UNASSIGNED" />
            <el-option
              v-for="account in brokerAccounts"
              :key="account.id"
              :label="brokerAccountLabel(account)"
              :value="account.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="handleSearch">查询</el-button>
          <el-button @click="resetFilters">重置</el-button>
        </el-form-item>
      </el-form>

      <!-- Table -->
      <div v-if="!isMobileView" class="responsive-table desktop-data-table">
        <el-table :data="transactions" v-loading="loading" stripe row-key="id" max-height="560">
          <template #empty>
            <el-empty description="暂无交易记录" :image-size="88" />
          </template>
          <el-table-column prop="transaction_date" label="交易日期" width="120" sortable>
            <template #default="{ row }">
              {{ formatDate(row.transaction_date) }}
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
          <el-table-column prop="transaction_type" label="类型" width="80">
            <template #default="{ row }">
              <el-tag :type="typeTagKind(row.transaction_type)" size="small">
                {{ typeLabel(row.transaction_type) }}
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
              <template v-if="!row.import_batch_id">
                <el-button
                  v-if="!isTransfer(row)"
                  type="primary"
                  size="small"
                  text
                  @click="handleEdit(row)"
                  >编辑</el-button
                >
                <el-button type="danger" size="small" text @click="handleDelete(row)"
                  >删除</el-button
                >
              </template>
              <el-tag v-else type="info" size="small">对账单导入 · 只读</el-tag>
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
            <el-tag :type="typeTagKind(row.transaction_type)" size="small">
              {{ typeLabel(row.transaction_type) }}
            </el-tag>
          </div>

          <div class="transaction-amount">
            <span
              >{{ formatDate(row.transaction_date) }} · {{ row.market }} · {{ row.currency }}</span
            >
            <strong>{{ formatNumber(row.quantity, 4) }} × {{ formatNumber(row.price, 4) }}</strong>
          </div>

          <div class="mobile-transaction-meta">
            <span :class="{ 'account-unassigned': !row.broker_account_id }">
              账户：{{ brokerAccountLabelById(row.broker_account_id) }}
            </span>
            <span>手续费 {{ formatNumber(row.fee, 2) }}</span>
            <span v-if="row.notes">{{ row.notes }}</span>
          </div>

          <div class="mobile-card-actions">
            <template v-if="!row.import_batch_id">
              <el-button
                v-if="!isTransfer(row)"
                type="primary"
                size="small"
                text
                @click="handleEdit(row)"
                >编辑</el-button
              >
              <el-button type="danger" size="small" text @click="handleDelete(row)">删除</el-button>
            </template>
            <el-tag v-else type="info" size="small">对账单导入 · 只读</el-tag>
          </div>
        </article>
        <el-empty
          v-if="!loading && transactions.length === 0"
          description="暂无交易记录"
          :image-size="88"
        />
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
    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑交易' : '新增交易'" width="600px">
      <el-form :model="form" :rules="rules" ref="formRef" label-width="100px">
        <el-form-item label="券商账户">
          <el-select
            v-model="form.broker_account_id"
            clearable
            placeholder="可选；用于账户归属"
            :loading="brokerAccountsLoading"
            style="width: 100%"
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
            <el-radio value="BUY">买入</el-radio>
            <el-radio value="SELL">卖出</el-radio>
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
    <el-dialog v-model="showImportDialog" title="导入数据" width="640px">
      <el-tabs v-model="importMode" class="import-tabs">
        <el-tab-pane label="标准交易文件" name="standard" />
        <el-tab-pane label="标准公司行动文件" name="corporate_actions" />
        <el-tab-pane label="招商证券对账单" name="cmb" />
        <el-tab-pane label="IBKR 活动报表" name="ibkr" />
        <el-tab-pane label="东方财富对账单" name="eastmoney" />
      </el-tabs>
      <div v-if="isBrokerImportMode" class="import-account-field">
        <span>导入到</span>
        <el-select
          v-model="importBrokerAccountId"
          clearable
          placeholder="选择匹配的券商账户"
          :loading="brokerAccountsLoading"
        >
          <el-option
            v-for="account in brokerImportAccountOptions"
            :key="account.id"
            :label="brokerAccountLabel(account)"
            :value="account.id"
          />
        </el-select>
        <small v-if="importMode === 'eastmoney'">
          预览和导入都按所选账户去重；普通股票与港股通两份对账单必须选择同一个账户
        </small>
        <small v-else-if="importMode === 'cmb'">预览和正式导入都必须选择匹配账户</small>
        <small v-else>预览和正式导入都必须选择匹配账户</small>
      </div>
      <div v-else class="import-account-field">
        <span>归属账户</span>
        <el-select
          v-model="importBrokerAccountId"
          clearable
          placeholder="不指定账户（默认）"
          :loading="brokerAccountsLoading"
        >
          <el-option
            v-for="account in brokerAccounts"
            :key="account.id"
            :label="brokerAccountLabel(account)"
            :value="account.id"
          />
        </el-select>
        <small>
          可选：把导入的记录归属到某个券商账户（如 HSBC 手工整理的标准 CSV）；不选则落"未指定账户"
        </small>
      </div>
      <el-upload
        drag
        :auto-upload="false"
        :on-change="handleFileChange"
        :on-remove="handleFileRemove"
        :limit="1"
        :accept="importAccept"
      >
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">拖拽文件到此处或 <em>点击上传</em></div>
        <template #tip>
          <div v-if="importMode === 'standard'" class="el-upload__tip">
            支持 CSV 或 Excel 文件，需包含以下列：symbol, market, transaction_type, quantity, price,
            transaction_date
          </div>
          <div v-else-if="importMode === 'corporate_actions'" class="el-upload__tip">
            支持 CSV 或 Excel 文件，需包含以下列：symbol, market, action_type, ex_date；可选
            total_dividend, net_dividend, split_ratio, shares_received 等公司行动字段
          </div>
          <div v-else class="el-upload__tip">
            <span v-if="importMode === 'cmb'">
              支持招商证券营业部普通对账单（年度）PDF，含沪港通交易（以 HKD
              记账、按行内推导结算汇率换算费用）；资金流水 Excel 不再作为导入来源
            </span>
            <span v-else-if="importMode === 'ibkr'">
              支持 trade_history.xlsx（规范格式）与 Activity Statement
              CSV（历史回填），导入普通股票/ETF买卖、股息和外国预扣税；期权跳过但归档，外汇、利息暂不导入
            </span>
            <span v-else>
              支持已解密的东方财富普通股票和港股通 PDF
              对账单；可导入股票、场内基金、港股通成交、红利、红利税和港股通组合费
            </span>
          </div>
        </template>
      </el-upload>

      <el-alert
        v-if="importMode === 'eastmoney'"
        class="preview-alert"
        type="info"
        :closable="false"
        show-icon
        title="请把两份互补对账单分别导入同一账户"
        description="先导入普通股票明细对账单，再导入港股通股票明细对账单；单独一份不能代表东方财富账户的完整交易历史。"
      />

      <div v-if="brokerPreview" class="import-preview">
        <el-descriptions :column="3" border size="small" class="responsive-descriptions">
          <el-descriptions-item label="券商">{{ brokerPreview.broker }}</el-descriptions-item>
          <el-descriptions-item v-if="importMode === 'eastmoney'" label="对账单范围">
            {{ brokerPreview.statement_scope === 'hk_connect' ? '港股通' : '普通股票' }}
          </el-descriptions-item>
          <el-descriptions-item
            v-if="importMode === 'cmb' && brokerPreview.source_account_masks?.length"
            label="单据账户尾号"
          >
            {{ brokerPreview.source_account_masks.join(' / ') }}
          </el-descriptions-item>
          <el-descriptions-item label="总行数">{{ brokerPreview.total_rows }}</el-descriptions-item>
          <el-descriptions-item v-if="brokerPreview.archived_source_rows != null" label="来源归档">
            {{ brokerPreview.archived_source_rows }}
          </el-descriptions-item>
          <el-descriptions-item label="买卖记录">{{
            brokerPreview.eligible_trade_rows
          }}</el-descriptions-item>
          <el-descriptions-item label="股息分红">{{
            brokerPreview.eligible_dividend_rows
          }}</el-descriptions-item>
          <el-descriptions-item label="红利税">{{
            brokerPreview.eligible_tax_rows
          }}</el-descriptions-item>
          <el-descriptions-item label="重复跳过">{{
            brokerPreview.duplicate_rows
          }}</el-descriptions-item>
          <el-descriptions-item label="非买卖跳过">{{
            brokerPreview.skipped_non_trade_rows
          }}</el-descriptions-item>
          <el-descriptions-item label="无效跳过">{{
            brokerPreview.skipped_invalid_rows
          }}</el-descriptions-item>
          <el-descriptions-item
            v-if="importMode === 'cmb' || importMode === 'eastmoney'"
            label="排除清单跳过"
          >
            {{ brokerPreview.skipped_excluded_rows || 0 }}
          </el-descriptions-item>
          <el-descriptions-item v-if="importMode === 'ibkr'" label="期权跳过">{{
            brokerPreview.skipped_option_rows
          }}</el-descriptions-item>
          <el-descriptions-item v-if="importMode === 'ibkr'" label="外汇跳过">{{
            brokerPreview.skipped_fx_rows
          }}</el-descriptions-item>
          <el-descriptions-item v-if="importMode === 'ibkr'" label="现金类跳过">{{
            brokerPreview.skipped_cash_rows
          }}</el-descriptions-item>
          <el-descriptions-item
            v-if="importMode === 'eastmoney' || importMode === 'cmb'"
            :label="importMode === 'cmb' ? '现金收益' : '港股通组合费'"
          >
            {{ brokerPreview.eligible_cash_rows || 0 }}
          </el-descriptions-item>
          <el-descriptions-item v-if="importMode === 'eastmoney'" label="不支持跳过">{{
            brokerPreview.skipped_unsupported_rows
          }}</el-descriptions-item>
          <el-descriptions-item v-if="importMode === 'eastmoney'" label="范围冲突">{{
            brokerPreview.skipped_conflict_rows || 0
          }}</el-descriptions-item>
          <el-descriptions-item v-if="importMode === 'eastmoney'" label="期末持仓">{{
            brokerPreview.reported_position_count || 0
          }}</el-descriptions-item>
          <el-descriptions-item label="日期范围"
            >{{ brokerPreview.date_start }} ~ {{ brokerPreview.date_end }}</el-descriptions-item
          >
        </el-descriptions>

        <el-alert
          v-if="brokerPreview.skipped_excluded_rows > 0"
          class="preview-alert"
          type="info"
          :closable="false"
          show-icon
          :title="`${brokerPreview.skipped_excluded_rows} 条流水命中排除清单，将只归档不入账（账户数据 → 排除清单 可调整）`"
        />
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
        <el-alert
          v-if="brokerPreview.warnings?.length"
          class="preview-alert"
          type="warning"
          :closable="false"
          show-icon
          title="存在待人工复核的未入账来源，可继续导入并保留审计记录"
          :description="brokerPreview.warnings.slice(0, 8).join('；')"
        />
        <el-alert
          v-if="brokerPreview.errors?.length"
          class="preview-alert"
          type="error"
          :closable="false"
          show-icon
          title="存在需要先处理的数据问题，当前不允许正式导入"
          :description="brokerPreview.errors.slice(0, 8).join('；')"
        />
      </div>
      <template #footer>
        <div class="mobile-dialog-footer">
          <el-button @click="showImportDialog = false">取消</el-button>
          <el-button
            v-if="isBrokerImportMode && !brokerPreview"
            type="primary"
            @click="handleImportPreview"
            :loading="importing"
            :disabled="requiresAccountScopedPreview && !importBrokerAccountId"
          >
            预览
          </el-button>
          <el-button
            v-else
            type="primary"
            @click="handleImport"
            :loading="importing"
            :disabled="
              (isBrokerImportMode && !importBrokerAccountId) || brokerPreviewHasBlockingErrors
            "
          >
            导入
          </el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, ref, reactive, watch, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'
import { useTransactionsStore } from '../stores/transactions'
import { useMediaQuery } from '../composables/useMediaQuery'
import { getApiErrorMessage } from '../utils/apiErrors'
import { formatNumber, formatDate, downloadFile } from '../utils/helpers'

const loading = ref(false)
const transactionsStore = useTransactionsStore()
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
const brokerAccounts = ref([])
const brokerAccountsLoading = ref(false)
const importBrokerAccountId = ref(null)
const isMobileView = useMediaQuery('(max-width: 640px)')
const importAccept = computed(() => {
  // IBKR：规范格式为 trade_history.xlsx；Activity CSV 保留供历史回填
  if (importMode.value === 'ibkr') return '.csv,.xlsx'
  if (importMode.value === 'eastmoney') return '.pdf'
  if (importMode.value === 'cmb') return '.pdf'
  return '.csv,.xlsx,.xls'
})
const isBrokerImportMode = computed(() => ['cmb', 'ibkr', 'eastmoney'].includes(importMode.value))
const requiresAccountScopedPreview = computed(() =>
  ['cmb', 'ibkr', 'eastmoney'].includes(importMode.value)
)
const brokerPreviewHasBlockingErrors = computed(
  () =>
    Boolean(brokerPreview.value?.errors?.length) ||
    brokerPreview.value?.batch_status === 'FAILED' ||
    brokerPreview.value?.reconciliation_status === 'MISMATCHED'
)
const brokerImportAccountOptions = computed(() => {
  const keywords = {
    cmb: ['招商'],
    ibkr: ['IBKR', 'INTERACTIVE'],
    eastmoney: ['东方']
  }[importMode.value]
  if (!keywords) return []
  return brokerAccounts.value.filter(
    (account) =>
      account.is_active !== false &&
      keywords.some((keyword) =>
        String(account.broker || '')
          .toUpperCase()
          .includes(keyword)
      )
  )
})

const filters = reactive({
  symbol: '',
  market: '',
  transaction_type: '',
  account: ''
})

const form = reactive({
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
  transaction_date: [{ required: true, message: '请选择交易日期', trigger: 'change' }]
}

async function loadTransactions(options = {}) {
  loading.value = true
  try {
    const params = buildQueryParams()
    params.skip = (pagination.page - 1) * pagination.pageSize
    params.limit = pagination.pageSize

    const [transactionsData, total] = await Promise.all([
      transactionsStore.fetchTransactions(params, { force: options?.force === true }),
      transactionsStore.fetchTransactionsCount(buildQueryParams(), {
        force: options?.force === true
      })
    ])

    transactions.value = transactionsData
    pagination.total = total

    const maxPage = Math.max(1, Math.ceil(pagination.total / pagination.pageSize))
    if (pagination.page > maxPage) {
      pagination.page = maxPage
      await loadTransactions()
    }
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '加载交易记录失败'))
  } finally {
    loading.value = false
  }
}

function buildQueryParams() {
  const params = {}
  if (filters.symbol) params.symbol = filters.symbol
  if (filters.market) params.market = filters.market
  if (filters.transaction_type) params.transaction_type = filters.transaction_type
  if (filters.account === 'UNASSIGNED') {
    params.unassigned_account = true
  } else if (filters.account !== '' && filters.account != null) {
    params.broker_account_id = filters.account
  }
  return params
}

function handleSearch() {
  pagination.page = 1
  loadTransactions({ force: true })
}

function handlePageSizeChange() {
  pagination.page = 1
  loadTransactions()
}

function resetFilters() {
  filters.symbol = ''
  filters.market = ''
  filters.transaction_type = ''
  filters.account = ''
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
    broker_account_id: row.broker_account_id || null,
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
      await transactionsStore.updateTransaction(form.id, form)
      ElMessage.success('更新成功')
    } else {
      await transactionsStore.createTransaction(form)
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    handleSearch()
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, isEdit.value ? '更新失败' : '创建失败'))
  } finally {
    submitting.value = false
  }
}

const TYPE_LABELS = {
  BUY: '买入',
  SELL: '卖出',
  TRANSFER_OUT: '转出',
  TRANSFER_IN: '转入'
}

const TYPE_TAG_KINDS = {
  BUY: 'success',
  SELL: 'danger',
  TRANSFER_OUT: 'warning',
  TRANSFER_IN: 'info'
}

function isTransfer(row) {
  return row.transaction_type === 'TRANSFER_OUT' || row.transaction_type === 'TRANSFER_IN'
}

function typeLabel(type) {
  return TYPE_LABELS[type] || type
}

function typeTagKind(type) {
  return TYPE_TAG_KINDS[type] || 'info'
}

function handleDelete(row) {
  const message = isTransfer(row)
    ? '这是转仓交易：删除将同时删除配对的另一腿，并重算相关持仓。确定继续吗？'
    : '确定要删除这条交易记录吗？'
  ElMessageBox.confirm(message, '提示', {
    confirmButtonText: '确定',
    cancelButtonText: '取消',
    type: 'warning'
  }).then(async () => {
    try {
      await transactionsStore.deleteTransaction(row.id)
      ElMessage.success('删除成功')
      loadTransactions()
    } catch (error) {
      ElMessage.error(getApiErrorMessage(error, '删除失败'))
    }
  })
}

async function handleExport() {
  try {
    const response = await api.exportExcel()
    downloadFile(response.data, `transactions_${new Date().toISOString().split('T')[0]}.xlsx`)
    ElMessage.success('导出成功')
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '导出失败'))
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
  importBrokerAccountId.value = null
})

watch(importBrokerAccountId, () => {
  brokerPreview.value = null
})

async function handleImportPreview() {
  if (!uploadFile.value) {
    ElMessage.warning('请选择文件')
    return
  }
  if (!isBrokerImportMode.value) {
    return
  }
  if (requiresAccountScopedPreview.value && !importBrokerAccountId.value) {
    ElMessage.warning('请选择匹配账户后再预览')
    return
  }

  importing.value = true
  try {
    let response
    if (importMode.value === 'ibkr') {
      response = await api.previewIbkrActivity(uploadFile.value, importBrokerAccountId.value)
    } else if (importMode.value === 'eastmoney') {
      response = await api.previewEastmoneyStatement(uploadFile.value, importBrokerAccountId.value)
    } else {
      response = await api.previewCmbFundFlows(uploadFile.value, importBrokerAccountId.value)
    }
    brokerPreview.value = response.data
    ElMessage.success('预览完成')
  } catch (error) {
    ElMessage.error('预览失败：' + getApiErrorMessage(error))
  } finally {
    importing.value = false
  }
}

async function handleImport() {
  if (!uploadFile.value) {
    ElMessage.warning('请选择文件')
    return
  }
  if (isBrokerImportMode.value && !importBrokerAccountId.value) {
    ElMessage.warning('正式券商导入必须选择匹配账户')
    return
  }
  importing.value = true
  try {
    let response
    let successMessage
    if (importMode.value === 'cmb') {
      response = await api.importCmbFundFlows(uploadFile.value, importBrokerAccountId.value)
      successMessage =
        `导入交易 ${response.data.imported_transactions} 条，` +
        `公司行动 ${response.data.imported_corporate_actions} 条，` +
        `红利税调整 ${response.data.imported_tax_adjustments} 条，` +
        `现金收益 ${response.data.imported_cash_events || 0} 条，` +
        `承接旧 Excel ${response.data.migrated_legacy_rows || 0} 条，` +
        `跳过重复 ${response.data.duplicate_rows} 条`
    } else if (importMode.value === 'ibkr') {
      response = await api.importIbkrActivity(uploadFile.value, importBrokerAccountId.value)
      successMessage =
        `导入交易 ${response.data.imported_transactions} 条，` +
        `公司行动 ${response.data.imported_corporate_actions} 条，` +
        `预扣税调整 ${response.data.imported_tax_adjustments} 条，` +
        `跳过重复 ${response.data.duplicate_rows} 条`
    } else if (importMode.value === 'eastmoney') {
      response = await api.importEastmoneyStatement(uploadFile.value, importBrokerAccountId.value)
      successMessage =
        `导入交易 ${response.data.imported_transactions} 条，` +
        `公司行动 ${response.data.imported_corporate_actions} 条，` +
        `红利税调整 ${response.data.imported_tax_adjustments} 条，` +
        `组合费 ${response.data.imported_cash_events || 0} 条，` +
        `跳过重复 ${response.data.duplicate_rows} 条`
    } else if (importMode.value === 'corporate_actions') {
      const isExcel =
        uploadFile.value.name.endsWith('.xlsx') || uploadFile.value.name.endsWith('.xls')
      response = isExcel
        ? await api.importCorporateActionsExcel(uploadFile.value, importBrokerAccountId.value)
        : await api.importCorporateActionsCSV(uploadFile.value, importBrokerAccountId.value)
      successMessage = response.data.message
    } else {
      const isExcel =
        uploadFile.value.name.endsWith('.xlsx') || uploadFile.value.name.endsWith('.xls')
      response = isExcel
        ? await api.importExcel(uploadFile.value, importBrokerAccountId.value)
        : await api.importCSV(uploadFile.value, importBrokerAccountId.value)
      successMessage = response.data.message
    }

    if (isBrokerImportMode.value) {
      const result = response.data
      const hasIssues =
        result.batch_status === 'PARTIAL' ||
        result.batch_status === 'FAILED' ||
        result.reconciliation_status === 'MISMATCHED' ||
        Boolean(result.errors?.length)
      if (hasIssues) {
        brokerPreview.value = result
        ElMessage.warning('导入未达到完整入账标准，请按页面提示处理后再导入')
        transactionsStore.invalidateDependentData()
        await loadTransactions({ force: true })
        return
      }
    }

    ElMessage.success(successMessage)
    showImportDialog.value = false
    uploadFile.value = null
    brokerPreview.value = null
    importBrokerAccountId.value = null
    transactionsStore.invalidateDependentData()
    loadTransactions()
  } catch (error) {
    ElMessage.error('导入失败：' + getApiErrorMessage(error))
  } finally {
    importing.value = false
  }
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

const brokerAccountLabel = (account) =>
  [account.account_name, account.broker, account.account_number_masked].filter(Boolean).join(' · ')
const brokerAccountLabelById = (id) => {
  if (id == null) return '待分配'
  const account = brokerAccounts.value.find((item) => String(item.id) === String(id))
  return account ? brokerAccountLabel(account) : '已删除账户'
}

async function loadBrokerAccounts() {
  brokerAccountsLoading.value = true
  try {
    const response = await api.getBrokerAccounts()
    brokerAccounts.value = Array.isArray(response.data) ? response.data : response.data?.items || []
  } catch (error) {
    if (import.meta.env.DEV) {
      console.warn('券商账户加载失败:', getApiErrorMessage(error))
    }
  } finally {
    brokerAccountsLoading.value = false
  }
}

onMounted(() => {
  loadTransactions()
  loadBrokerAccounts()
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

.import-account-field {
  display: grid;
  grid-template-columns: auto minmax(220px, 1fr);
  align-items: center;
  gap: 6px 12px;
  margin-bottom: 14px;
  padding: 12px 14px;
  border-radius: var(--app-radius-inner);
  background: var(--app-surface-muted);
}

.import-account-field > span {
  color: var(--app-text-muted);
  font-size: 14px;
  font-weight: 600;
}

.import-account-field > small {
  grid-column: 2;
  color: var(--app-text-soft);
}

.account-unassigned {
  color: var(--app-warning);
  font-weight: 600;
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
    font-weight: 600;
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

  .import-account-field {
    grid-template-columns: 1fr;
  }

  .import-account-field > small {
    grid-column: 1;
  }
}
</style>
