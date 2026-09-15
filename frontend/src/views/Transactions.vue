<template>
  <div class="transactions-page">
    <el-card class="transactions-card">
      <template #header>
        <div class="page-header">
          <span>交易记录管理</span>
          <div class="header-actions">
            <el-button :icon="Upload" @click="importDialog?.open()">导入</el-button>
            <el-button :icon="Download" @click="handleExport">导出</el-button>
            <el-button type="primary" :icon="Plus" @click="formDialog?.openAdd()">
              新增交易
            </el-button>
          </div>
        </div>
      </template>

      <!-- Filters -->
      <el-form :inline="true" class="filter-form">
        <el-form-item label="代码">
          <SecuritySelect
            v-model="list.filters.symbol"
            :resolve="false"
            placeholder="股票代码"
            class="filter-symbol"
            @select="onFilterSymbolSelected"
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
            v-model="list.filters.transaction_type"
            placeholder="交易类型"
            clearable
            @change="list.handleSearch"
            @clear="list.handleSearch"
          >
            <el-option label="买入" value="BUY" />
            <el-option label="卖出" value="SELL" />
            <el-option label="转出" value="TRANSFER_OUT" />
            <el-option label="转入" value="TRANSFER_IN" />
          </el-select>
        </el-form-item>
        <el-form-item label="账户">
          <el-select
            v-model="list.filters.account"
            placeholder="全部账户"
            clearable
            @change="list.handleSearch"
            @clear="list.handleSearch"
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
          <el-button type="primary" @click="list.handleSearch">查询</el-button>
          <el-button @click="list.resetFilters">重置</el-button>
        </el-form-item>
      </el-form>

      <TransactionsTable
        :list="list"
        :broker-accounts="brokerAccounts"
        @edit="(row) => formDialog?.openEdit(row)"
      />

      <div class="pagination-bar">
        <span class="pagination-info">
          共 {{ list.pagination.total }} 条，每页只渲染当前页以提升性能
        </span>
        <el-pagination
          v-model:current-page="list.pagination.page"
          v-model:page-size="list.pagination.pageSize"
          :page-sizes="[25, 50, 100, 200]"
          :total="list.pagination.total"
          layout="sizes, prev, pager, next, jumper"
          background
          @size-change="list.handlePageSizeChange"
          @current-change="() => list.loadTransactions()"
        />
      </div>
    </el-card>

    <TransactionFormDialog
      ref="formDialog"
      :broker-accounts="brokerAccounts"
      :broker-accounts-loading="brokerAccountsLoading"
      @saved="list.handleSearch"
    />

    <ImportDialog
      ref="importDialog"
      :broker-accounts="brokerAccounts"
      :broker-accounts-loading="brokerAccountsLoading"
      @imported="handleImported"
    />
  </div>
</template>

<script setup lang="ts">
import { Upload, Download, Plus } from '@element-plus/icons-vue'
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'
import SecuritySelect from '../components/SecuritySelect.vue'
import { useTransactionsStore } from '../stores/transactions'
import { getApiErrorMessage } from '../utils/apiErrors'
import type { BrokerAccount, SecuritySearchItem } from '../types'
import { downloadFile, todayLocalISODate } from '../utils/helpers'
import { MARKETS } from '../utils/securities'
import TransactionsTable from './transactions/TransactionsTable.vue'
import TransactionFormDialog from './transactions/TransactionFormDialog.vue'
import ImportDialog from './transactions/ImportDialog.vue'
import { brokerAccountLabel } from './transactions/shared'
import { useTransactionsList } from './transactions/useTransactionsList'

// 壳层职责（issue #140）：页头（导入/导出/新增）、过滤表单、分页与三个
// 子件的编排。列表数据面在 useTransactionsList；新增编辑表单与导入向导
// 各自成自足 dialog（expose open*，保存/入账后回调壳层刷新）。
const transactionsStore = useTransactionsStore()
const list = useTransactionsList()

// 筛选框选中候选：代码与市场一起定，否则 00700 配 A股 筛选查空
function onFilterSymbolSelected(item: SecuritySearchItem) {
  list.filters.market = item.market
  list.handleSearch()
}

const formDialog = ref<InstanceType<typeof TransactionFormDialog> | null>(null)
const importDialog = ref<InstanceType<typeof ImportDialog> | null>(null)

const brokerAccounts = ref<BrokerAccount[]>([])
const brokerAccountsLoading = ref(false)

async function loadBrokerAccounts() {
  brokerAccountsLoading.value = true
  try {
    const response = await api.getBrokerAccounts()
    brokerAccounts.value = response.data
  } catch (error) {
    if (import.meta.env.DEV) {
      console.warn('券商账户加载失败:', getApiErrorMessage(error))
    }
  } finally {
    brokerAccountsLoading.value = false
  }
}

async function handleExport() {
  try {
    const response = await api.exportExcel()
    downloadFile(response.data, `transactions_${todayLocalISODate()}.xlsx`)
    ElMessage.success('导出成功')
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '导出失败'))
  }
}

// 导入入账后：持仓/统计等派生数据一并失效，再按导入结果的口径刷新列表
// （部分入账时 force 强制重取，与拆分前一致）
async function handleImported(options: { force: boolean }) {
  transactionsStore.invalidateDependentData()
  await list.loadTransactions(options)
}

onMounted(() => {
  list.loadTransactions()
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
}
</style>
