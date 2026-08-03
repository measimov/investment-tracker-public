<template>
  <div class="corporate-actions-page">
    <el-tabs v-model="activeTab" class="page-tabs">
      <el-tab-pane label="公司行动记录" name="records" />
      <el-tab-pane name="suggestions">
        <template #label>
          <span>
            分红建议
            <el-badge
              v-if="suggestionPendingCount > 0"
              :value="suggestionPendingCount"
              class="tab-badge"
            />
          </span>
        </template>
      </el-tab-pane>
    </el-tabs>

    <el-card v-show="activeTab === 'records'">
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
          <el-table-column label="操作" width="150">
            <template #default="{ row }">
              <el-tag v-if="row.import_batch_id" type="info" effect="plain" size="small">
                导入只读
              </el-tag>
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
            <el-tag v-if="row.import_batch_id" type="info" effect="plain" size="small">
              导入只读
            </el-tag>
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
    </el-card>

    <!-- 分红建议（Tushare 公告同步；仅 A/B 股） -->
    <el-card v-show="activeTab === 'suggestions'">
      <template #header>
        <div class="page-header">
          <span>分红公告建议</span>
          <div class="header-actions">
            <el-select
              v-model="suggestionStatusFilter"
              class="suggestion-status-filter"
              @change="loadSuggestions"
            >
              <el-option label="待处理（新建议+已匹配）" value="" />
              <el-option label="仅新建议" value="NEW" />
              <el-option label="仅已匹配" value="MATCHED" />
              <el-option label="已接受" value="ACCEPTED" />
              <el-option label="已忽略" value="IGNORED" />
            </el-select>
            <el-button
              type="primary"
              :loading="syncing"
              data-testid="dividend-sync-button"
              @click="syncDividends"
            >
              同步 A 股分红公告
            </el-button>
          </div>
        </div>
      </template>

      <el-alert
        title="仅同步 A/B 股公告（Tushare）；港股/美股分红仍以券商对账单导入为准。建议不会自动入账——点“接受”才会创建正式公司行动记录。"
        type="info"
        :closable="false"
        show-icon
        class="suggestions-note"
      />

      <el-table v-loading="suggestionsLoading" :data="suggestions" stripe>
        <el-table-column label="代码/名称" min-width="130">
          <template #default="{ row }">
            <span class="suggestion-symbol">{{ row.symbol }}</span>
            <span v-if="row.name" class="suggestion-name">{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="100">
          <template #default="{ row }">
            <el-tag :type="getActionTypeTag(row.action_type)" size="small">
              {{ getActionTypeName(row.action_type) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="账户" min-width="110" show-overflow-tooltip>
          <template #default="{ row }">
            <span :class="{ 'account-unassigned': !row.broker_account_id }">
              {{
                row.broker_account_id
                  ? brokerAccountLabelById(row.broker_account_id)
                  : row.action_type === 'STOCK_DIVIDEND'
                    ? '全部账户'
                    : '未指定'
              }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="除权日" width="110">
          <template #default="{ row }">{{ formatDate(row.ex_date) }}</template>
        </el-table-column>
        <el-table-column label="派息日" width="110">
          <template #default="{ row }">{{
            row.pay_date ? formatDate(row.pay_date) : '—'
          }}</template>
        </el-table-column>
        <el-table-column label="每股税前(税后)" min-width="130" align="right">
          <template #default="{ row }">
            <template v-if="row.action_type === 'CASH_DIVIDEND'">
              {{ formatNumber(toNumber(row.cash_div_pre_tax), 4) }}
              <span v-if="row.cash_div_after_tax" class="after-tax">
                ({{ formatNumber(toNumber(row.cash_div_after_tax), 4) }})
              </span>
            </template>
            <template v-else
              >每股送转 {{ formatNumber(toNumber(row.stk_div_per_share), 4) }}</template
            >
          </template>
        </el-table-column>
        <el-table-column label="登记日持仓" min-width="110" align="right">
          <template #default="{ row }">
            <span>{{ formatNumber(toNumber(row.record_date_quantity), 0) }}</span>
            <el-tooltip
              v-if="row.quantity_basis === 'merged'"
              content="账户归属存在矛盾，按合并口径推算（数量总和可信）"
            >
              <el-tag type="warning" size="small" effect="plain">合并</el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="推算总额(税前)" min-width="120" align="right">
          <template #default="{ row }">
            {{
              row.estimated_total_dividend != null
                ? formatNumber(toNumber(row.estimated_total_dividend), 2)
                : '—'
            }}
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tooltip
              v-if="row.match_detail && row.match_detail.amount_diff != null"
              :content="`已按日期匹配到账本记录，但金额差 ${formatNumber(row.match_detail.amount_diff, 2)}，请核对`"
            >
              <el-tag type="warning" size="small">已匹配·金额差</el-tag>
            </el-tooltip>
            <el-tag v-else :type="suggestionStatusTag(row.status)" size="small">
              {{ suggestionStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <!-- 仅 NEW 可接受：MATCHED 已有账本记录，再入账即双计（后端同样拒绝） -->
            <template v-if="row.status === 'NEW'">
              <el-button type="primary" size="small" text @click="openAcceptDialog(row)">
                接受
              </el-button>
              <el-button type="info" size="small" text @click="ignoreSuggestion(row)">
                忽略
              </el-button>
            </template>
            <template v-else-if="row.status === 'MATCHED'">
              <el-tooltip content="账本已有匹配记录，无需入账；如有出入请先核对既有记录">
                <span class="accepted-hint">已在账</span>
              </el-tooltip>
              <el-button type="info" size="small" text @click="ignoreSuggestion(row)">
                忽略
              </el-button>
            </template>
            <el-button
              v-else-if="row.status === 'IGNORED'"
              type="primary"
              size="small"
              text
              @click="restoreSuggestion(row)"
            >
              恢复
            </el-button>
            <span v-else class="accepted-hint">已入账</span>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无分红建议；点击右上角同步公告" :image-size="88" />
        </template>
      </el-table>
    </el-card>

    <!-- 接受建议：账户归属与税额可改 -->
    <el-dialog v-model="acceptDialog.visible" title="接受分红建议" width="480px">
      <el-form label-width="110px">
        <el-form-item label="标的">
          <span>
            {{ acceptDialog.row?.symbol }} {{ acceptDialog.row?.name || '' }} （{{
              getActionTypeName(acceptDialog.row?.action_type || '')
            }}）
          </span>
        </el-form-item>
        <el-form-item label="券商账户">
          <el-select
            v-model="acceptDialog.brokerAccountId"
            placeholder="可选；按实际到账账户归属"
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
        <template v-if="acceptDialog.row?.action_type === 'CASH_DIVIDEND'">
          <el-form-item label="股息总额">
            <el-input-number
              v-model="acceptDialog.totalDividend"
              :min="0"
              :precision="2"
              :controls="false"
              class="amount-input"
            />
          </el-form-item>
          <el-form-item label="预扣税额">
            <el-input-number
              v-model="acceptDialog.taxWithheld"
              :min="0"
              :precision="2"
              :controls="false"
              class="amount-input"
            />
            <div class="field-hint">A 股券商到账通常为税前全额，税额保持 0 即可</div>
          </el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="acceptDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="accepting" @click="submitAccept">确认入账</el-button>
      </template>
    </el-dialog>

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
          <el-input v-model="form.symbol" placeholder="如: 600000, AAPL" />
        </el-form-item>

        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" placeholder="资产名称" />
        </el-form-item>

        <el-form-item label="市场" prop="market">
          <el-select v-model="form.market" placeholder="选择市场">
            <el-option label="A股" value="A股" />
            <el-option label="B股" value="B股" />
            <el-option label="港股" value="港股" />
            <el-option label="美股" value="美股" />
            <el-option label="新加坡股" value="新加坡股" />
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

        <!-- 通用字段 -->
        <el-divider content-position="left">其他信息</el-divider>

        <el-form-item label="币种" prop="currency">
          <el-select v-model="form.currency">
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

<script setup lang="ts">
import { Plus } from '@element-plus/icons-vue'
import { ref, reactive, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance } from 'element-plus'
import api from '../api'
import { useHoldingsStore } from '../stores/holdings'
import { useMediaQuery } from '../composables/useMediaQuery'
import { getApiErrorMessage } from '../utils/apiErrors'
import { formatNumber, formatDate, toNumber } from '../utils/helpers'
import { pollJobUntilDone } from '../utils/polling'

interface CorporateActionRow {
  id: number
  broker_account_id?: number | null
  symbol: string
  name?: string | null
  market: string
  action_type: string
  ex_date: string
  dividend_per_share?: number | string | null
  total_dividend?: number | string | null
  tax_rate?: number | string | null
  shares_received?: number | string | null
  distribution_ratio?: string | null
  subscription_price?: number | string | null
  subscription_quantity?: number | string | null
  split_ratio?: string | null
  currency?: string | null
  notes?: string | null
  [key: string]: unknown
}

interface BrokerAccount {
  id: number
  account_name: string
  account_number_masked?: string | null
  [key: string]: unknown
}

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

interface SuggestionRow {
  id: number
  broker_account_id?: number | null
  symbol: string
  name?: string | null
  market: string
  action_type: string
  ex_date: string
  pay_date?: string | null
  cash_div_pre_tax?: number | string | null
  cash_div_after_tax?: number | string | null
  stk_div_per_share?: number | string | null
  record_date_quantity?: number | string | null
  quantity_basis?: string | null
  estimated_total_dividend?: number | string | null
  status: string
  match_detail?: { amount_diff?: number | null; [key: string]: unknown } | null
  [key: string]: unknown
}

const isMobileView = useMediaQuery('(max-width: 640px)')
const loading = ref(false)
const holdingsStore = useHoldingsStore()
const actions = ref<CorporateActionRow[]>([])
const summary = ref<ActionsSummary | null>(null)
const brokerAccounts = ref<BrokerAccount[]>([])
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

const actionTypeNames: Record<string, string> = {
  CASH_DIVIDEND: '现金股息',
  STOCK_DIVIDEND: '股票股息',
  RIGHTS_ISSUE: '配股',
  STOCK_SPLIT: '拆股',
  REVERSE_SPLIT: '合股',
  BONUS_ISSUE: '送股'
}

const actionTypeTags: Record<string, string> = {
  CASH_DIVIDEND: 'success',
  STOCK_DIVIDEND: 'warning',
  RIGHTS_ISSUE: 'info',
  STOCK_SPLIT: 'primary',
  REVERSE_SPLIT: 'primary',
  BONUS_ISSUE: 'warning'
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
  }

  return parts.length ? parts.join(' | ') : '-'
}

function getActionTypeName(type: string) {
  return actionTypeNames[type] || type
}

function getActionTypeTag(type: string) {
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
  resetForm()
  dialogVisible.value = true
}

function handleEdit(row: CorporateActionRow) {
  isEdit.value = true
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

function brokerAccountLabel(account: BrokerAccount) {
  const suffix = account.account_number_masked ? ` · ${account.account_number_masked}` : ''
  return `${account.account_name}${suffix}`
}

function brokerAccountLabelById(accountId: number | null | undefined) {
  if (!accountId) return '未分配'
  const account = brokerAccounts.value.find((item) => item.id === accountId)
  return account ? brokerAccountLabel(account) : '已删除账户'
}

async function loadBrokerAccounts() {
  try {
    const response = await api.getBrokerAccounts({ limit: 1000 })
    brokerAccounts.value = response.data
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '加载券商账户失败'))
  }
}

// ---------------------------------------------------------------------------
// 分红建议 tab
// ---------------------------------------------------------------------------

const activeTab = ref<'records' | 'suggestions'>('records')
const suggestions = ref<SuggestionRow[]>([])
const suggestionsLoading = ref(false)
const suggestionStatusFilter = ref('')
const suggestionPendingCount = ref(0)
const syncing = ref(false)
const accepting = ref(false)

const acceptDialog = reactive<{
  visible: boolean
  row: SuggestionRow | null
  brokerAccountId: number | null
  totalDividend: number | null
  taxWithheld: number | null
}>({ visible: false, row: null, brokerAccountId: null, totalDividend: null, taxWithheld: null })

function suggestionStatusLabel(status: string) {
  return (
    (
      { NEW: '新建议', MATCHED: '已匹配', ACCEPTED: '已入账', IGNORED: '已忽略' } as Record<
        string,
        string
      >
    )[status] || status
  )
}

function suggestionStatusTag(status: string) {
  if (status === 'NEW') return 'primary'
  if (status === 'ACCEPTED') return 'success'
  if (status === 'IGNORED') return 'info'
  return 'info'
}

async function loadSuggestions() {
  suggestionsLoading.value = true
  try {
    const params: Record<string, unknown> = { limit: 200 }
    if (suggestionStatusFilter.value) params.status = suggestionStatusFilter.value
    const response = await api.listDividendSuggestions(params)
    suggestions.value = response.data
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '加载分红建议失败'))
  } finally {
    suggestionsLoading.value = false
  }
}

async function refreshSuggestionCount() {
  try {
    const response = await api.countDividendSuggestions()
    suggestionPendingCount.value = response.data.total || 0
  } catch {
    // 徽标计数失败静默：不打断主流程
  }
}

async function syncDividends() {
  syncing.value = true
  try {
    const startResponse = await api.startDividendSyncJob()
    const job = await pollJobUntilDone(() => api.getDividendSyncJob(startResponse.data.id), {
      intervalMs: 2000,
      maxAttempts: 150,
      failureMessage: '分红公告同步失败'
    })
    if (job) {
      const result = (job.result || {}) as Record<string, number | unknown[]>
      const failed = Array.isArray(result.failed) ? result.failed.length : 0
      ElMessage.success(
        `同步完成：扫描 ${result.symbols_scanned ?? 0} 只标的，新建议 ${result.new ?? 0} 条、` +
          `已匹配 ${result.matched ?? 0} 条、事件 ${result.events_upserted ?? 0} 条` +
          (failed ? `；${failed} 只标的失败` : '')
      )
    }
    await Promise.all([loadSuggestions(), refreshSuggestionCount()])
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '分红公告同步失败'))
  } finally {
    syncing.value = false
  }
}

function openAcceptDialog(row: SuggestionRow) {
  acceptDialog.row = row
  // 默认取建议行自身的账户归属（每账户一条建议）；NULL=未指定/合并口径
  acceptDialog.brokerAccountId = row.broker_account_id ?? null
  acceptDialog.totalDividend =
    row.estimated_total_dividend != null ? toNumber(row.estimated_total_dividend) : null
  acceptDialog.taxWithheld = 0
  acceptDialog.visible = true
}

async function submitAccept() {
  if (!acceptDialog.row) return
  accepting.value = true
  try {
    // 始终显式发送 broker_account_id（含 null）：省略该键时后端会沿用建议
    // 原账户，用户"清空账户"的意图会静默丢失。el-select 清空把 model 置为
    // undefined，而 undefined 在 JSON 序列化时被丢键，必须归一化为 null。
    const payload: Record<string, unknown> = {
      broker_account_id:
        typeof acceptDialog.brokerAccountId === 'number' ? acceptDialog.brokerAccountId : null
    }
    if (acceptDialog.row.action_type === 'CASH_DIVIDEND') {
      if (acceptDialog.totalDividend != null) payload.total_dividend = acceptDialog.totalDividend
      if (acceptDialog.taxWithheld != null) payload.tax_withheld = acceptDialog.taxWithheld
    }
    await api.acceptDividendSuggestion(acceptDialog.row.id, payload)
    ElMessage.success('已入账为正式公司行动记录')
    acceptDialog.visible = false
    await Promise.all([loadSuggestions(), refreshSuggestionCount(), loadActions()])
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '接受建议失败'))
    // 后端可能已在拒绝时把建议转为 MATCHED（迟到入账重判重）：刷新列表
    // 反映真实状态；若该行已不再是 NEW，关闭弹窗防止对旧状态重试。
    const failedId = acceptDialog.row?.id
    await Promise.all([loadSuggestions(), refreshSuggestionCount()])
    const fresh = suggestions.value.find((row) => row.id === failedId)
    if (!fresh || fresh.status !== 'NEW') acceptDialog.visible = false
  } finally {
    accepting.value = false
  }
}

async function ignoreSuggestion(row: SuggestionRow) {
  try {
    await api.ignoreDividendSuggestion(row.id)
    await Promise.all([loadSuggestions(), refreshSuggestionCount()])
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '忽略建议失败'))
  }
}

async function restoreSuggestion(row: SuggestionRow) {
  try {
    await api.restoreDividendSuggestion(row.id)
    await Promise.all([loadSuggestions(), refreshSuggestionCount()])
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '恢复建议失败'))
  }
}

watch(activeTab, (tab) => {
  if (tab === 'suggestions' && !suggestions.value.length) loadSuggestions()
})

onMounted(async () => {
  await loadBrokerAccounts()
  loadActions()
  refreshSuggestionCount()
})
</script>

<style scoped>
.stats-alert {
  margin-bottom: 14px;
}

.page-tabs {
  margin-bottom: 4px;
}

.tab-badge {
  margin-left: 6px;
  vertical-align: 2px;
}

.suggestion-status-filter {
  width: 200px;
  margin-right: 10px;
}

.suggestions-note {
  margin-bottom: 14px;
}

.suggestion-symbol {
  font-weight: 600;
  margin-right: 6px;
}

.suggestion-name {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.after-tax {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.amount-input {
  width: 100%;
}

.field-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.4;
}

.accepted-hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.corporate-actions-page {
  width: 100%;
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
