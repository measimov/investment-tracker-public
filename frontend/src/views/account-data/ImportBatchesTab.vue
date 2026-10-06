<script setup lang="ts">
import { type AccountListStatus, accountLabel } from '@/utils/labels'
import { computed, h, ref, watch } from 'vue'
import { NAlert, NButton, NDataTable, NEmpty, NSpin, NTag, type DataTableColumns } from 'naive-ui'
import { useLatestRequest } from '@/composables/useLatestRequest'
import { useMediaQuery } from '@/composables/useMediaQuery'
import api from '@/api'
import { EMPTY, formatDate, formatDateTime } from '@/utils/helpers'
import { type AccountRow, type ImportBatchRow, LIST_LIMIT, isAtListLimit } from './shared'

const props = defineProps<{
  importBatches: ImportBatchRow[]
  accounts: AccountRow[]
  accountsStatus: AccountListStatus
  loading: boolean
  hasLoaded: boolean
  loadError: boolean
  reload: () => Promise<unknown>
}>()

const accountLabelOf = (id: unknown) =>
  accountLabel(props.accounts, id, { status: props.accountsStatus })

const isMobileView = useMediaQuery('(max-width: 640px)')
const emptyDescription = computed(() =>
  props.loadError
    ? '尚未确认导入批次，请重试'
    : !props.hasLoaded
      ? '导入批次正在加载'
      : '暂无可追溯的导入批次'
)
const detailRequests = useLatestRequest()
const detailLoading = ref(false)
const detailError = ref(false)
const batchDrawerVisible = ref(false)
const selectedBatch = ref<ImportBatchRow | null>(null)

watch(batchDrawerVisible, (visible) => {
  if (!visible) {
    detailRequests.invalidate()
    detailLoading.value = false
  }
})
async function openBatchDetails(row: ImportBatchRow) {
  const token = detailRequests.begin()
  selectedBatch.value = row
  detailError.value = false
  detailLoading.value = true
  batchDrawerVisible.value = true
  try {
    const response = await api.getImportBatch(row.id)
    if (
      detailRequests.isCurrent(token) &&
      batchDrawerVisible.value &&
      selectedBatch.value?.id === row.id
    )
      selectedBatch.value = response.data
  } catch {
    // 保留当前批次的列表元数据，明确未确认详情；旧批次失败不能污染新选择。
    if (
      detailRequests.isCurrent(token) &&
      batchDrawerVisible.value &&
      selectedBatch.value?.id === row.id
    )
      detailError.value = true
  } finally {
    if (detailRequests.isCurrent(token)) detailLoading.value = false
  }
}

const reportPeriod = (row: ImportBatchRow) => {
  const start = row.period_start
  const end = row.period_end
  return start || end ? `${formatDate(start)} 至 ${formatDate(end)}` : '未声明报表区间'
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
  if (value === 'FAILED') return 'error'
  if (value === 'PARTIAL') return 'warning'
  return 'default'
}

const resultText = (row: ImportBatchRow) =>
  `${row.archived_count ?? 0} 来源归档 / ${row.imported_count ?? 0} 本批入账 / ${row.duplicate_count ?? 0} 已有重复 / ${row.skipped_count ?? 0} 未入账`
const columns: DataTableColumns<ImportBatchRow> = [
  {
    title: '导入时间',
    key: 'created_at',
    width: 165,
    render: (row) => formatDateTime(row.created_at)
  },
  {
    title: '账户',
    key: 'broker_account_id',
    width: 190,
    render: (row) => accountLabelOf(row.broker_account_id)
  },
  {
    title: '文件',
    key: 'source_filename',
    width: 260,
    render: (row) =>
      h('div', { class: 'primary-cell' }, [
        h('strong', row.source_filename || '未命名来源'),
        h('span', reportPeriod(row))
      ])
  },
  { title: '结果', key: 'result', width: 285, render: resultText },
  {
    title: '状态',
    key: 'status',
    width: 190,
    cellProps: () => ({ style: { verticalAlign: 'top' } }),
    render: (row) =>
      h('div', [
        h(NTag, { size: 'small', bordered: false, type: batchStatusTag(row.status) }, () =>
          batchStatusLabel(row.status)
        ),
        row.error_message
          ? h('details', { class: 'read-details' }, [
              h('summary', '查看原因'),
              h('p', row.error_message)
            ])
          : null
      ])
  },
  {
    title: '操作',
    key: 'actions',
    width: 90,
    fixed: 'right',
    render: (row) =>
      h('div', { class: 'row-actions' }, [
        h(
          NButton,
          {
            text: true,
            type: 'primary',
            'aria-label': `查看 ${row.source_filename || row.id} 导入批次详情`,
            onClick: () => openBatchDetails(row)
          },
          () => '详情'
        )
      ])
  }
]
</script>

<template>
  <div>
    <div class="section-toolbar">
      <div>
        <h2>导入批次</h2>
        <p>每次券商导入留下的来源记录（只读）；对账单从「交易记录」页的「导入」上传。</p>
      </div>
      <NButton :loading="loading" aria-label="重新加载导入批次" @click="reload">重新加载</NButton>
    </div>

    <NAlert
      v-if="loadError"
      type="warning"
      :show-icon="false"
      class="read-alert"
      title="导入批次加载失败"
      >{{ hasLoaded ? '显示上次成功加载的批次，尚未确认最新结果。' : '尚未确认导入批次，请重试。' }}
      <NButton text type="primary" @click="reload">重试导入批次</NButton></NAlert
    >
    <p v-else-if="loading && hasLoaded" class="read-note" role="status">
      正在重新加载，以下为上次成功批次。
    </p>
    <p v-if="isAtListLimit(importBatches)" class="read-note">
      仅显示最近 {{ LIST_LIMIT }} 个导入批次
    </p>
    <NSpin :show="loading">
      <div v-if="isMobileView" class="mobile-card-list">
        <NEmpty
          v-if="!importBatches.length"
          :description="emptyDescription"
          :theme-overrides="{ textColor: 'var(--app-text-muted)' }"
        />
        <article
          v-for="row in importBatches"
          :key="row.id"
          class="mobile-card"
          data-testid="import-batch-card"
        >
          <div class="mobile-card-head">
            <div class="mobile-card-title">
              <strong class="mobile-card-symbol">{{ row.source_filename || '未命名来源' }}</strong
              ><span class="mobile-card-name">{{ accountLabelOf(row.broker_account_id) }}</span>
            </div>
            <NTag size="small" :bordered="false" :type="batchStatusTag(row.status)">{{
              batchStatusLabel(row.status)
            }}</NTag>
          </div>
          <div class="mobile-card-meta">
            <span>{{ formatDateTime(row.created_at) }}</span
            ><span>{{ reportPeriod(row) }}</span
            ><span>{{ resultText(row) }}</span>
          </div>
          <details v-if="row.error_message" class="read-details">
            <summary>查看原因</summary>
            <p>{{ row.error_message }}</p>
          </details>
          <div class="mobile-card-actions">
            <NButton
              type="primary"
              text
              :aria-label="`查看 ${row.source_filename || row.id} 导入批次详情`"
              @click="openBatchDetails(row)"
              >详情</NButton
            >
          </div>
        </article>
      </div>
      <NDataTable
        v-else
        class="batch-table"
        :data="importBatches"
        :columns="columns"
        :row-key="(row: ImportBatchRow) => row.id"
        :scroll-x="1180"
        :bordered="false"
        ><template #empty
          ><NEmpty
            :description="emptyDescription"
            :theme-overrides="{ textColor: 'var(--app-text-muted)' }" /></template
      ></NDataTable>
    </NSpin>

    <el-drawer v-model="batchDrawerVisible" title="导入批次详情" size="min(520px, 92%)">
      <NAlert
        v-if="detailError"
        type="warning"
        :show-icon="false"
        title="此批次详情加载失败"
        class="read-alert"
        >以下为
        {{ selectedBatch?.source_filename || '当前批次' }} 的列表元数据，详情尚未确认。<NButton
          text
          type="primary"
          @click="selectedBatch && openBatchDetails(selectedBatch)"
          >重试此批次详情</NButton
        ></NAlert
      >
      <p v-else-if="detailLoading" class="read-note" role="status">
        正在加载此批次详情，以下为列表元数据。
      </p>
      <el-descriptions v-if="selectedBatch" :column="1" border>
        <el-descriptions-item label="文件">{{
          selectedBatch.source_filename || EMPTY
        }}</el-descriptions-item>
        <el-descriptions-item label="账户">{{
          accountLabelOf(selectedBatch.broker_account_id)
        }}</el-descriptions-item>
        <el-descriptions-item label="导入时间">{{
          formatDateTime(selectedBatch.created_at)
        }}</el-descriptions-item>
        <el-descriptions-item label="报表区间">{{
          reportPeriod(selectedBatch)
        }}</el-descriptions-item>
        <el-descriptions-item label="来源类型">{{
          selectedBatch.source_type || EMPTY
        }}</el-descriptions-item>
        <el-descriptions-item label="解析器">
          {{
            [selectedBatch.parser_name, selectedBatch.parser_version].filter(Boolean).join(' ') ||
            EMPTY
          }}
        </el-descriptions-item>
        <el-descriptions-item label="文件哈希">
          <code class="hash-value">{{ selectedBatch.source_sha256 || EMPTY }}</code>
        </el-descriptions-item>
        <el-descriptions-item label="总行数">{{
          selectedBatch.row_count ?? EMPTY
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
</style>
