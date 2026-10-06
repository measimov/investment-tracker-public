<script setup lang="ts">
import { computed, h } from 'vue'
import { NButton, NDataTable, NEmpty, NSpin, NTag, type DataTableColumns } from 'naive-ui'
import type { ExchangeRate } from '@/types'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { formatDate, formatDateTime, formatNumber } from '@/utils/helpers'
import { sourceLabel, sourceTagType } from './sources'
const props = defineProps<{
  rows: ExchangeRate[]
  loading: boolean
  emptyDescription: string
  canEdit: boolean
}>()
const emit = defineEmits<{ edit: [row: ExchangeRate]; deactivate: [id: number] }>()
const isMobileView = useMediaQuery('(max-width: 640px)')
const isManual = (row: ExchangeRate) => !row.source || row.source === 'manual'
const sourceType = (source: string | null | undefined) => {
  const type = sourceTagType(source)
  return type === 'danger' ? 'error' : type === 'info' ? 'default' : type
}
const deactivateTheme = {
  textColorTextHoverError: 'var(--app-danger-text)',
  textColorTextPressedError: 'var(--app-danger-text)',
  textColorTextFocusError: 'var(--app-danger-text)'
}
const editName = (row: ExchangeRate) =>
  `编辑 ${row.from_currency}/${row.to_currency} ${formatDate(row.effective_date)} 汇率`
const deactivateName = (row: ExchangeRate) =>
  `停用 ${row.from_currency}/${row.to_currency} ${formatDate(row.effective_date)} 汇率`
const columns = computed<DataTableColumns<ExchangeRate>>(() => {
  const result: DataTableColumns<ExchangeRate> = [
    { title: '源币种', key: 'from_currency', width: 90 },
    { title: '目标币种', key: 'to_currency', width: 90 },
    {
      title: '汇率',
      key: 'rate',
      width: 110,
      align: 'right',
      render: (row) => h('span', { class: 'num' }, formatNumber(row.rate, 4))
    },
    {
      title: '生效日期',
      key: 'effective_date',
      width: 120,
      render: (row) => h('span', { class: 'num' }, formatDate(row.effective_date))
    },
    {
      title: '来源',
      key: 'source',
      width: 180,
      render: (row) =>
        h('div', { class: 'source-tags' }, [
          h(NTag, { size: 'small', bordered: false, type: sourceType(row.source) }, () =>
            sourceLabel(row.source)
          ),
          row.is_active === false
            ? h(NTag, { size: 'small', bordered: false }, () => '已停用')
            : null
        ])
    },
    {
      title: '创建时间',
      key: 'created_at',
      width: 160,
      render: (row) => h('span', { class: 'num cell-sub' }, formatDateTime(row.created_at))
    }
  ]
  if (props.canEdit)
    result.push({
      title: '操作',
      key: 'actions',
      width: 140,
      fixed: 'right',
      render: (row) =>
        h('div', { class: 'row-actions' }, [
          h(
            NButton,
            {
              text: true,
              type: 'primary',
              'aria-label': editName(row),
              onClick: () => emit('edit', row)
            },
            () => '编辑'
          ),
          isManual(row) && row.is_active !== false
            ? h(
                NButton,
                {
                  text: true,
                  type: 'error',
                  themeOverrides: deactivateTheme,
                  'aria-label': deactivateName(row),
                  onClick: () => emit('deactivate', row.id)
                },
                () => '停用'
              )
            : null
        ])
    })
  return result
})
</script>
<template>
  <NSpin :show="loading">
    <NEmpty
      v-if="!rows.length"
      :description="emptyDescription"
      :theme-overrides="{ textColor: 'var(--app-text-muted)' }"
    />
    <div v-else-if="isMobileView" class="history-cards">
      <article v-for="row in rows" :key="row.id" class="history-card">
        <header>
          <span>{{ row.from_currency }} <span class="cell-sub">兑</span> {{ row.to_currency }}</span
          ><strong class="num">{{ formatNumber(row.rate, 4) }}</strong>
        </header>
        <p class="effective-date">生效 {{ formatDate(row.effective_date) }}</p>
        <div class="source-tags">
          <NTag size="small" :bordered="false" :type="sourceType(row.source)">{{
            sourceLabel(row.source)
          }}</NTag
          ><NTag v-if="row.is_active === false" size="small" :bordered="false">已停用</NTag>
        </div>
        <details>
          <summary>创建记录</summary>
          <p class="cell-sub num">{{ formatDateTime(row.created_at) }}</p>
        </details>
        <div v-if="canEdit" class="card-actions">
          <NButton :aria-label="editName(row)" @click="emit('edit', row)">编辑</NButton
          ><NButton
            v-if="isManual(row) && row.is_active !== false"
            type="error"
            secondary
            :aria-label="deactivateName(row)"
            @click="emit('deactivate', row.id)"
            >停用</NButton
          >
        </div>
      </article>
    </div>
    <NDataTable
      v-else
      class="rate-history-table"
      :columns="columns"
      :data="rows"
      :row-key="(row: ExchangeRate) => row.id"
      :scroll-x="canEdit ? 990 : 850"
      :bordered="false"
      ><template #empty><NEmpty :description="emptyDescription" /></template
    ></NDataTable>
  </NSpin>
</template>
<style scoped>
.num,
.rate-history-table :deep(.num) {
  font-variant-numeric: tabular-nums;
}
.cell-sub,
.rate-history-table :deep(.cell-sub) {
  color: var(--app-text-muted);
  font-size: 12px;
}
.source-tags,
.rate-history-table :deep(.source-tags) {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.rate-history-table :deep(.row-actions) {
  display: flex;
  gap: 16px;
}
.rate-history-table :deep(.row-actions .n-button) {
  min-height: 24px;
}
.history-cards {
  display: grid;
  gap: 12px;
}
.history-card {
  padding: 18px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 6px;
  min-width: 0;
}
.history-card header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.history-card strong {
  font-size: 23px;
  font-weight: 550;
}
.effective-date {
  margin: 10px 0;
  color: var(--app-text-muted);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
.history-card summary {
  min-height: 44px;
  display: list-item;
  align-content: center;
  font-size: 13px;
  color: var(--app-text-muted);
  cursor: pointer;
}
.history-card details p {
  margin: 0 0 10px;
}
.card-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  border-top: 1px solid var(--app-border);
  padding-top: 12px;
}
.card-actions :deep(.n-button) {
  min-height: 44px;
}
</style>
