<script setup lang="ts">
import { useMediaQuery } from '@/composables/useMediaQuery'
import { formatDate, formatPrice, formatQuantity } from '@/utils/helpers'
import { holdingsLink } from '@/utils/securities'
import type { BrokerAccount } from '@/types'
import type { Transaction } from '@/stores/transactions'
import { accountLabel, transactionTypeLabel, transactionTypeTag } from '@/utils/labels'
import { isTransfer } from './shared'
import type { TransactionsListFeature } from './useTransactionsList'

const props = defineProps<{ list: TransactionsListFeature; brokerAccounts: BrokerAccount[] }>()

defineEmits<{ edit: [row: Transaction] }>()

const isMobileView = useMediaQuery('(max-width: 640px)')

function brokerAccountLabelById(id: number | null | undefined) {
  return accountLabel(props.brokerAccounts, id)
}
</script>

<template>
  <div v-if="!isMobileView" class="responsive-table desktop-data-table">
    <el-table
      :data="list.transactions"
      v-loading="list.loading"
      stripe
      row-key="id"
      max-height="560"
    >
      <template #empty>
        <el-empty description="暂无交易记录" :image-size="88" />
      </template>
      <!-- 服务端分页：不开前端 sortable（只会排当前页）；后端固定按日期倒序 -->
      <el-table-column prop="transaction_date" label="交易日期" width="120">
        <template #default="{ row }">
          {{ formatDate(row.transaction_date) }}
        </template>
      </el-table-column>
      <!-- 代码跳持仓页并定位到该标的（已清仓时持仓页会提示并给档案入口） -->
      <el-table-column prop="symbol" label="代码" width="100">
        <template #default="{ row }">
          <router-link
            :to="holdingsLink(row)"
            class="symbol-link"
            data-testid="transaction-symbol-link"
          >
            {{ row.symbol }}
          </router-link>
        </template>
      </el-table-column>
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
          <el-tag :type="transactionTypeTag(row.transaction_type)" size="small">
            {{ transactionTypeLabel(row.transaction_type) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="quantity" label="数量" width="100" align="right">
        <template #default="{ row }">
          {{ formatQuantity(row.quantity) }}
        </template>
      </el-table-column>
      <el-table-column prop="price" label="价格" width="100" align="right">
        <template #default="{ row }">
          {{ formatPrice(row.price) }}
        </template>
      </el-table-column>
      <el-table-column prop="fee" label="手续费" width="100" align="right">
        <template #default="{ row }">
          {{ formatPrice(row.fee) }}
        </template>
      </el-table-column>
      <el-table-column prop="currency" label="币种" width="80" />
      <el-table-column prop="notes" label="备注" min-width="150" show-overflow-tooltip />
      <el-table-column label="操作" width="150">
        <template #default="{ row }">
          <template v-if="!row.read_only">
            <el-button
              v-if="!isTransfer(row)"
              type="primary"
              size="small"
              text
              @click="$emit('edit', row)"
              >编辑</el-button
            >
            <el-button type="danger" size="small" text @click="list.handleDelete(row)"
              >删除</el-button
            >
          </template>
          <el-tag v-else type="info" size="small">对账单导入 · 只读</el-tag>
        </template>
      </el-table-column>
    </el-table>
  </div>

  <div v-else v-loading="list.loading" class="mobile-card-list">
    <article
      v-for="row in list.transactions"
      :key="row.id"
      class="mobile-card"
      data-testid="transaction-card"
    >
      <div class="mobile-card-head">
        <div class="mobile-card-title">
          <router-link
            :to="holdingsLink(row)"
            class="mobile-card-symbol symbol-link"
            data-testid="transaction-symbol-link"
          >
            {{ row.symbol }}
          </router-link>
          <span class="mobile-card-name">{{ row.name || row.market }}</span>
        </div>
        <el-tag :type="transactionTypeTag(row.transaction_type)" size="small">
          {{ transactionTypeLabel(row.transaction_type) }}
        </el-tag>
      </div>

      <div class="transaction-amount">
        <span>{{ formatDate(row.transaction_date) }} · {{ row.market }} · {{ row.currency }}</span>
        <strong>{{ formatQuantity(row.quantity) }} × {{ formatPrice(row.price) }}</strong>
      </div>

      <div class="mobile-card-meta">
        <span :class="{ 'account-unassigned': !row.broker_account_id }">
          账户：{{ brokerAccountLabelById(row.broker_account_id) }}
        </span>
        <span>手续费 {{ formatPrice(row.fee) }}</span>
        <span v-if="row.notes">{{ row.notes }}</span>
      </div>

      <div class="mobile-card-actions">
        <template v-if="!row.read_only">
          <el-button
            v-if="!isTransfer(row)"
            type="primary"
            size="small"
            text
            @click="$emit('edit', row)"
            >编辑</el-button
          >
          <el-button type="danger" size="small" text @click="list.handleDelete(row)"
            >删除</el-button
          >
        </template>
        <el-tag v-else type="info" size="small">对账单导入 · 只读</el-tag>
      </div>
    </article>
    <el-empty
      v-if="!list.loading && list.transactions.length === 0"
      description="暂无交易记录"
      :image-size="88"
    />
  </div>
</template>

<style scoped>
.account-unassigned {
  color: var(--app-warning);
  font-weight: 600;
}

.symbol-link {
  color: var(--app-primary);
  text-decoration: none;
}

.symbol-link:hover,
.symbol-link:focus-visible {
  text-decoration: underline;
}

:deep(.el-table .el-button.is-text) {
  padding-inline: 4px;
}

@media (max-width: 640px) {
  /* 卡片通用外观见 styles.css 的 .mobile-card 套件；这里只留交易特有的金额块 */
  .transaction-amount {
    display: grid;
    gap: 6px;
  }

  .transaction-amount span {
    color: var(--app-text-muted);
    font-size: 12px;
  }

  .transaction-amount strong {
    color: var(--app-text);
    font-size: 17px;
    line-height: 1.25;
    font-variant-numeric: tabular-nums;
  }
}
</style>
