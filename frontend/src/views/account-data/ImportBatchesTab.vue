<script setup lang="ts">
import { ref } from 'vue'
import api from '@/api'
import { formatDateTime } from '@/utils/helpers'
import {
  type AccountRow,
  type ImportBatchRow,
  LIST_LIMIT,
  accountLabelIn,
  isAtListLimit
} from './shared'

const props = defineProps<{
  importBatches: ImportBatchRow[]
  accounts: AccountRow[]
  loading: boolean
}>()

const accountLabel = (id: unknown) => accountLabelIn(props.accounts, id)

const batchDrawerVisible = ref(false)
const selectedBatch = ref<ImportBatchRow | null>(null)

async function openBatchDetails(row: ImportBatchRow) {
  selectedBatch.value = row
  batchDrawerVisible.value = true
  try {
    const response = await api.getImportBatch(row.id)
    selectedBatch.value = response?.data || row
  } catch {
    // The list already contains enough information for the drawer.
  }
}

const reportPeriod = (row: ImportBatchRow) => {
  const start = row.period_start || row.statement_start_date
  const end = row.period_end || row.statement_end_date
  return start || end ? `${start || '?'} 至 ${end || '?'}` : '未声明报表区间'
}

const batchStatusLabel = (status: string | undefined) =>
  (
    ({
      COMPLETED: '完成',
      SUCCESS: '完成',
      FAILED: '失败',
      PARTIAL: '部分完成',
      PROCESSING: '处理中',
      PENDING: '等待中'
    }) as Record<string, string>
  )[String(status).toUpperCase()] ||
  status ||
  '未知'

const batchStatusTag = (status: string | undefined) => {
  const value = String(status).toUpperCase()
  if (['COMPLETED', 'SUCCESS'].includes(value)) return 'success'
  if (value === 'FAILED') return 'danger'
  if (value === 'PARTIAL') return 'warning'
  return 'info'
}
</script>

<template>
  <div>
    <div class="section-toolbar">
      <div>
        <h2>导入批次</h2>
        <p>每次券商导入留下的来源记录（只读）；对账单从「交易记录」页的「导入」上传。</p>
      </div>
    </div>

    <el-alert
      v-if="isAtListLimit(importBatches)"
      type="info"
      :closable="false"
      show-icon
      class="list-limit-alert"
      :title="`仅显示最近 ${LIST_LIMIT} 个导入批次`"
    />

    <div class="responsive-table">
      <el-table :data="importBatches" v-loading="loading" stripe row-key="id">
        <template #empty>
          <el-empty description="暂无可追溯的导入批次" :image-size="88" />
        </template>
        <el-table-column label="导入时间" min-width="160">
          <template #default="{ row }">{{
            formatDateTime(row.created_at || row.imported_at)
          }}</template>
        </el-table-column>
        <el-table-column label="账户" min-width="160">
          <template #default="{ row }">{{
            accountLabel(row.broker_account_id || row.account_id)
          }}</template>
        </el-table-column>
        <el-table-column label="文件" min-width="230" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="primary-cell">
              <strong>{{ row.source_filename || row.original_filename || '未命名来源' }}</strong>
              <span>{{ reportPeriod(row) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="结果" min-width="300">
          <template #default="{ row }">
            {{ row.archived_count ?? 0 }} 来源归档 /
            {{ row.imported_count ?? row.rows_imported ?? 0 }} 本批入账 /
            {{ row.duplicate_count ?? 0 }} 已有重复 /
            {{ row.skipped_count ?? row.rows_skipped ?? 0 }} 未入账
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <!-- 部分完成/失败的原因在 error_message：悬停即可看到，不必打开抽屉 -->
            <el-tooltip
              :disabled="!row.error_message"
              :content="row.error_message || ''"
              placement="top"
              popper-class="batch-status-tooltip"
            >
              <el-tag
                :type="batchStatusTag(row.status)"
                size="small"
                :class="{ 'status-with-reason': row.error_message }"
              >
                {{ batchStatusLabel(row.status) }}
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" text @click="openBatchDetails(row)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-drawer v-model="batchDrawerVisible" title="导入批次详情" size="min(520px, 92%)">
      <el-descriptions v-if="selectedBatch" :column="1" border>
        <el-descriptions-item label="文件">{{
          selectedBatch.source_filename || selectedBatch.original_filename || '-'
        }}</el-descriptions-item>
        <el-descriptions-item label="账户">{{
          accountLabel(selectedBatch.broker_account_id || selectedBatch.account_id)
        }}</el-descriptions-item>
        <el-descriptions-item label="导入时间">{{
          formatDateTime(selectedBatch.created_at || selectedBatch.imported_at)
        }}</el-descriptions-item>
        <el-descriptions-item label="报表区间">{{
          reportPeriod(selectedBatch)
        }}</el-descriptions-item>
        <el-descriptions-item label="来源类型">{{
          selectedBatch.source_type || '-'
        }}</el-descriptions-item>
        <el-descriptions-item label="解析器">
          {{
            [selectedBatch.parser_name, selectedBatch.parser_version].filter(Boolean).join(' ') ||
            '-'
          }}
        </el-descriptions-item>
        <el-descriptions-item label="文件哈希">
          <code class="hash-value">{{
            selectedBatch.source_sha256 || selectedBatch.file_sha256 || '-'
          }}</code>
        </el-descriptions-item>
        <el-descriptions-item label="总行数">{{
          selectedBatch.row_count ?? selectedBatch.total_rows ?? '-'
        }}</el-descriptions-item>
        <el-descriptions-item label="来源归档">{{
          selectedBatch.archived_count ?? 0
        }}</el-descriptions-item>
        <el-descriptions-item label="本批入账">{{
          selectedBatch.imported_count ?? 0
        }}</el-descriptions-item>
        <el-descriptions-item label="已有重复">{{
          selectedBatch.duplicate_count ?? 0
        }}</el-descriptions-item>
        <el-descriptions-item label="未入账">{{
          selectedBatch.skipped_count ?? 0
        }}</el-descriptions-item>
        <el-descriptions-item label="待处理行">{{
          selectedBatch.error_count ?? 0
        }}</el-descriptions-item>
        <el-descriptions-item label="状态说明">{{
          selectedBatch.error_message || '—'
        }}</el-descriptions-item>
      </el-descriptions>
    </el-drawer>
  </div>
</template>

<style scoped>
.hash-value {
  word-break: break-all;
}

.list-limit-alert {
  margin-bottom: 12px;
}

.status-with-reason {
  cursor: help;
  text-decoration: underline dotted;
}
</style>

<style>
/* tooltip 挂在 body 上，scoped 样式够不到；长原因按视口宽度折行 */
.batch-status-tooltip {
  max-width: min(420px, 90vw);
  line-height: 1.5;
}
</style>
