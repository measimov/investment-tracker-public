<script setup lang="ts">
/**
 * 公司行动记录 tab：筛选 + 表格/卡片 + 分页（列表状态在 useCorporateActionsList），新增/编辑与
 * 补录成本各是一个对话框组件（#284 拆分，此前 1158 行）。
 */
import { ref } from 'vue'
import { Plus } from '@element-plus/icons-vue'
import SecuritySelect from '@/components/SecuritySelect.vue'
import { useMediaQuery } from '@/composables/useMediaQuery'
import type { BrokerAccount, CorporateAction } from '@/types'
import { formatCurrency, formatDate, formatQuantity } from '@/utils/helpers'
import {
  accountLabel,
  accountOptionLabel,
  ACTION_TYPE_LABELS,
  actionTypeLabel,
  actionTypeTag,
  optionsOf,
  UNASSIGNED_ACCOUNT,
  UNASSIGNED_ACCOUNT_LABEL
} from '@/utils/labels'
import { MARKETS } from '@/utils/securities'
import CorporateActionFormDialog from './CorporateActionFormDialog.vue'
import OpeningCostDialog from './OpeningCostDialog.vue'
import { cashDividendAmounts, openingPositionCostKnown } from './shared'
import { useCorporateActionsList } from './useCorporateActionsList'

type CorporateActionRow = CorporateAction

const props = defineProps<{ brokerAccounts: BrokerAccount[] }>()

const isMobileView = useMediaQuery('(max-width: 640px)')
const actionTypeOptions = optionsOf(ACTION_TYPE_LABELS)
const list = useCorporateActionsList()
const formDialog = ref<InstanceType<typeof CorporateActionFormDialog> | null>(null)
const costDialog = ref<InstanceType<typeof OpeningCostDialog> | null>(null)

function brokerAccountLabelById(accountId: number | null | undefined) {
  return accountLabel(props.brokerAccounts, accountId)
}

// 详情文案：桌面表格与移动卡片共用一份。
// 后端 CorporateActionCreate 允许若干字段二选一（送股=比例或绝对股数、
// 拆股=比例或拆后股数），所以这里只拼存在的字段——直接模板插值会把
// 合法的"只填绝对股数"记录显示成 `比例: null`，还会丢掉唯一有效的值。
// 字段顺序与 portfolio/semantics.py 的优先级一致：决定复算的比例在前。
// 金额一律带币种符号（港/美股息不能看起来像人民币）；数量走 formatQuantity
function actionDetail(row: CorporateActionRow): string {
  const currency = row.currency || 'CNY'
  const money = (value: unknown, precision = 2) =>
    formatCurrency(value as number | string, currency, precision)
  const qty = (value: unknown) => formatQuantity(value as number | string)
  const has = (value: unknown) => value !== null && value !== undefined && value !== ''
  const parts: string[] = []

  switch (row.action_type) {
    case 'CASH_DIVIDEND': {
      if (has(row.dividend_per_share)) parts.push(`每股: ${money(row.dividend_per_share, 4)}`)
      if (has(row.total_dividend)) parts.push(`总额: ${money(row.total_dividend)}`)
      const { tax, net } = cashDividendAmounts(row)
      if (tax > 0) parts.push(`预扣税: ${money(tax)}`)
      if (has(row.total_dividend) && (tax > 0 || has(row.net_dividend)))
        parts.push(`税后: ${money(net)}`)
      break
    }
    case 'STOCK_DIVIDEND':
    case 'BONUS_ISSUE':
      if (has(row.distribution_ratio)) parts.push(`比例: ${row.distribution_ratio}`)
      if (has(row.shares_received)) parts.push(`获得股数: ${qty(row.shares_received)}`)
      break
    case 'RIGHTS_ISSUE':
      if (has(row.subscription_price)) parts.push(`认购价: ${money(row.subscription_price, 4)}`)
      if (has(row.subscription_quantity)) parts.push(`数量: ${qty(row.subscription_quantity)}`)
      break
    case 'STOCK_SPLIT':
    case 'REVERSE_SPLIT':
      if (has(row.split_ratio)) parts.push(`拆分比例: ${row.split_ratio}`)
      if (has(row.new_shares)) parts.push(`拆后股数: ${qty(row.new_shares)}`)
      break
    case 'OPENING_POSITION':
      if (has(row.adjusted_quantity)) parts.push(`数量: ${qty(row.adjusted_quantity)}`)
      if (has(row.cost_basis_adjustment)) parts.push(`总成本: ${money(row.cost_basis_adjustment)}`)
      else if (has(row.adjusted_cost_per_share))
        parts.push(`单位成本: ${money(row.adjusted_cost_per_share, 4)}`)
      else parts.push('成本未知')
      break
  }

  return parts.length ? parts.join(' | ') : '—'
}

// 只读判定与后端同一判据：带导入批次，或被券商来源流水引用（后端 read_only 字段）
function isReadOnly(row: CorporateActionRow): boolean {
  return Boolean(row.read_only || row.import_batch_id)
}

// 跨 tab 刷新入口：分红建议"接受"入账后由壳层调用（新记录要出现在列表里）
defineExpose({ reload: list.loadActions })
</script>

<template>
  <el-card>
    <template #header>
      <div class="page-header">
        <span>公司行动管理</span>
        <div class="header-actions">
          <el-button type="primary" :icon="Plus" @click="formDialog?.openAdd()">新增记录</el-button>
        </div>
      </div>
    </template>

    <!-- Filters -->
    <el-form :inline="true" class="filter-form">
      <el-form-item label="账户">
        <el-select
          v-model="list.filters.account"
          placeholder="全部账户"
          clearable
          @change="list.handleSearch"
          @clear="list.handleSearch"
        >
          <el-option :label="UNASSIGNED_ACCOUNT_LABEL" :value="UNASSIGNED_ACCOUNT" />
          <el-option
            v-for="account in brokerAccounts"
            :key="account.id"
            :label="accountOptionLabel(account)"
            :value="account.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="代码">
        <SecuritySelect
          v-model="list.filters.symbol"
          :resolve="false"
          placeholder="股票代码"
          class="filter-symbol"
          @select="list.onFilterSymbolSelected"
          @clear="list.handleSearch"
          @keyup.enter="list.handleSearch"
        />
      </el-form-item>
      <el-form-item label="市场">
        <el-select
          v-model="list.filters.market"
          placeholder="选择市场"
          clearable
          @change="list.handleSearch"
          @clear="list.handleSearch"
        >
          <el-option v-for="m in MARKETS" :key="m" :label="m" :value="m" />
        </el-select>
      </el-form-item>
      <el-form-item label="类型">
        <el-select
          v-model="list.filters.action_type"
          placeholder="行动类型"
          clearable
          @change="list.handleSearch"
          @clear="list.handleSearch"
        >
          <el-option
            v-for="item in actionTypeOptions"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="日期">
        <el-date-picker
          v-model="list.filters.date_range"
          type="daterange"
          value-format="YYYY-MM-DD"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          range-separator="至"
          clearable
          @change="list.handleSearch"
          @clear="list.handleSearch"
        />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="list.handleSearch">查询</el-button>
        <el-button @click="list.resetFilters">重置</el-button>
      </el-form-item>
    </el-form>

    <!-- Statistics Summary -->
    <el-alert
      v-if="list.summary?.cash_dividends?.missing_rate_currencies?.length"
      type="warning"
      :closable="false"
      show-icon
      class="stats-alert"
      :title="`缺少 ${list.summary.cash_dividends.missing_rate_currencies.join('/')} 汇率，对应股息未计入 CNY 折算总额，请先在汇率页补录`"
    />
    <el-row :gutter="20" class="stats-row" v-if="list.summary">
      <el-col :xs="12" :md="6">
        <el-statistic title="总记录数" :value="list.summary.total_count" />
      </el-col>
      <el-col :xs="12" :md="6">
        <el-statistic
          title="股息总额（CNY折算）"
          :value="list.summary.cash_dividends?.total_dividend || 0"
          :precision="2"
          prefix="¥"
        />
      </el-col>
      <el-col :xs="12" :md="6">
        <el-statistic
          title="预扣税（CNY折算）"
          :value="list.summary.cash_dividends?.total_tax || 0"
          :precision="2"
          prefix="¥"
        />
      </el-col>
      <el-col :xs="12" :md="6">
        <el-statistic
          title="税后净额（CNY折算）"
          :value="list.summary.cash_dividends?.net_dividend || 0"
          :precision="2"
          prefix="¥"
        />
      </el-col>
    </el-row>

    <!-- Table -->
    <div v-if="!isMobileView" class="responsive-table desktop-data-table">
      <el-table :data="list.actions" v-loading="list.loading" stripe row-key="id" max-height="560">
        <template #empty>
          <el-empty description="暂无公司行动记录" :image-size="88" />
        </template>
        <!-- 服务端分页：不开前端 sortable（只会排当前页）；后端固定按除权除息日倒序 -->
        <el-table-column prop="ex_date" label="除权除息日" width="120">
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
            <el-tag :type="actionTypeTag(row.action_type)" size="small">
              {{ actionTypeLabel(row.action_type) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="currency" label="币种" width="70" />
        <el-table-column label="详情" min-width="240">
          <template #default="{ row }">{{ actionDetail(row) }}</template>
        </el-table-column>
        <el-table-column prop="notes" label="备注" min-width="150" show-overflow-tooltip />
        <el-table-column label="操作" width="170">
          <template #default="{ row }">
            <template v-if="isReadOnly(row)">
              <el-tag type="info" effect="plain" size="small">导入只读</el-tag>
              <el-button
                v-if="row.action_type === 'OPENING_POSITION' && !openingPositionCostKnown(row)"
                type="warning"
                size="small"
                text
                @click="costDialog?.open(row)"
              >
                补录成本
              </el-button>
            </template>
            <template v-else>
              <el-button type="primary" size="small" text @click="formDialog?.openEdit(row)">
                编辑
              </el-button>
              <el-button type="danger" size="small" text @click="list.handleDelete(row)">
                删除
              </el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div v-else v-loading="list.loading" class="mobile-card-list">
      <el-empty v-if="!list.actions.length" description="暂无公司行动记录" :image-size="88" />
      <article
        v-for="row in list.actions"
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
            <el-tag :type="actionTypeTag(row.action_type)" size="small">
              {{ actionTypeLabel(row.action_type) }}
            </el-tag>
          </div>
        </div>

        <div class="action-detail">{{ actionDetail(row) }}</div>

        <div class="mobile-card-meta">
          <span>除权除息日 {{ formatDate(row.ex_date) }}</span>
          <span>{{ row.market }} · {{ row.currency }}</span>
          <span :class="{ 'account-unassigned': !row.broker_account_id }">
            {{ brokerAccountLabelById(row.broker_account_id) }}
          </span>
          <span v-if="row.notes">{{ row.notes }}</span>
        </div>

        <div class="mobile-card-actions">
          <template v-if="isReadOnly(row)">
            <el-tag type="info" effect="plain" size="small">导入只读</el-tag>
            <el-button
              v-if="row.action_type === 'OPENING_POSITION' && !openingPositionCostKnown(row)"
              type="warning"
              size="small"
              text
              @click="costDialog?.open(row)"
            >
              补录成本
            </el-button>
          </template>
          <template v-else>
            <el-button type="primary" size="small" text @click="formDialog?.openEdit(row)"
              >编辑</el-button
            >
            <el-button type="danger" size="small" text @click="list.handleDelete(row)"
              >删除</el-button
            >
          </template>
        </div>
      </article>
    </div>

    <div class="table-pagination">
      <el-pagination
        v-model:current-page="list.pagination.page"
        v-model:page-size="list.pagination.pageSize"
        :total="list.pagination.total"
        :page-sizes="[25, 50, 100, 200]"
        layout="total, sizes, prev, pager, next, jumper"
        background
        @size-change="list.handlePageSizeChange"
        @current-change="list.loadActions"
      />
    </div>

    <CorporateActionFormDialog
      ref="formDialog"
      :broker-accounts="brokerAccounts"
      @saved="list.loadActions"
    />
    <OpeningCostDialog ref="costDialog" @saved="list.loadActions" />
  </el-card>
</template>

<style scoped>
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
