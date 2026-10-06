<script setup lang="ts">
import { h } from 'vue'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { NDataTable, NEmpty, NSpin, NTag, type DataTableColumns } from 'naive-ui'
import type { NotificationEventItem } from '@/types'
import { formatDateTime } from '@/utils/helpers'
import { eventKindLabel, eventStatus } from '../systemAlerts'
const props = defineProps<{
  rows: NotificationEventItem[]
  loading: boolean
  emptyDescription: string
}>()
const mobile = useMediaQuery('(max-width: 640px)')
function tagType(row: NotificationEventItem) {
  const type = eventStatus(row).type
  return type === 'danger' ? 'error' : type === 'info' ? 'default' : type
}
function message(text: string) {
  return text.length > 100
    ? h('details', { class: 'message-details' }, [
        h('summary', {}, text.slice(0, 80) + '…'),
        h('p', {}, text)
      ])
    : h('p', { class: 'event-message' }, text)
}
const columns: DataTableColumns<NotificationEventItem> = [
  { key: 'kind', title: '类型', width: 120, render: (row) => eventKindLabel(row.kind) },
  { key: 'content', title: '内容', width: 350, render: (row) => message(row.message || row.title) },
  {
    key: 'status',
    title: '推送',
    width: 230,
    render: (row) =>
      h('div', {}, [
        h(
          NTag,
          {
            size: 'small',
            bordered: false,
            type: tagType(row),
            themeOverrides: { colorSuccess: 'var(--app-success-soft)' }
          },
          { default: () => eventStatus(row).text }
        ),
        row.last_error && row.status !== 'sent' ? message(row.last_error) : null
      ])
  },
  {
    key: 'time',
    title: '时间',
    width: 180,
    render: (row) =>
      h('div', {}, [
        h('span', {}, formatDateTime(row.created_at)),
        row.sent_at ? h('p', { class: 'subline' }, `送达 ${formatDateTime(row.sent_at)}`) : null
      ])
  }
]
</script>
<template>
  <NSpin :show="loading">
    <div class="events-table">
      <div
        v-if="!mobile"
        class="table-scroll"
        tabindex="0"
        role="region"
        aria-label="最近提醒明细，可横向滚动"
      >
        <NDataTable
          :columns="columns"
          :data="rows"
          :row-key="(row: NotificationEventItem) => row.id"
          :single-line="true"
          :bordered="true"
          style="min-width: 880px"
          aria-label="最近提醒明细"
        >
          <template #empty
            ><NEmpty
              :description="emptyDescription"
              :theme-overrides="{ textColor: 'var(--app-text-muted)' }"
          /></template>
        </NDataTable>
      </div>
      <p v-if="!mobile" class="scroll-note">表格可横向滚动查看完整推送状态与时间。</p>
      <template v-else>
        <NEmpty
          v-if="!rows.length"
          :description="emptyDescription"
          :theme-overrides="{ textColor: 'var(--app-text-muted)' }"
        />
        <article
          v-for="row in rows"
          :key="row.id"
          class="event-card"
          data-testid="notification-event-card"
        >
          <header>
            <h3>{{ eventKindLabel(row.kind) }}</h3>
            <NTag
              size="small"
              :bordered="false"
              :type="tagType(row)"
              :theme-overrides="{ colorSuccess: 'var(--app-success-soft)' }"
              >{{ eventStatus(row).text }}</NTag
            >
          </header>
          <details v-if="(row.message || row.title).length > 100" class="message-details">
            <summary>{{ (row.message || row.title).slice(0, 80) }}…</summary>
            <p>{{ row.message || row.title }}</p>
          </details>
          <p v-else class="event-message">{{ row.message || row.title }}</p>
          <details
            v-if="row.last_error && row.status !== 'sent' && row.last_error.length > 100"
            class="message-details"
          >
            <summary>{{ row.last_error.slice(0, 80) }}…</summary>
            <p>{{ row.last_error }}</p>
          </details>
          <p v-else-if="row.last_error && row.status !== 'sent'" class="error-message">
            {{ row.last_error }}
          </p>
          <dl>
            <div>
              <dt>时间</dt>
              <dd>{{ formatDateTime(row.created_at) }}</dd>
            </div>
            <div v-if="row.sent_at">
              <dt>送达</dt>
              <dd>{{ formatDateTime(row.sent_at) }}</dd>
            </div>
          </dl>
        </article>
      </template>
    </div>
  </NSpin>
</template>
<style scoped>
.table-scroll {
  max-width: 100%;
  overflow-x: auto;
  border-radius: 4px;
}
.table-scroll:focus-visible,
.events-table :deep(summary:focus-visible) {
  outline: 2px solid var(--app-primary);
  outline-offset: 3px;
}
.events-table :deep(.event-message),
.events-table :deep(.message-details),
.events-table :deep(.subline),
.error-message {
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  margin: 6px 0 0;
}
.events-table :deep(.message-details),
.events-table :deep(.subline),
.error-message {
  color: var(--app-text-muted);
}
.events-table :deep(summary) {
  cursor: pointer;
  min-height: 24px;
}
.events-table :deep(.message-details p) {
  margin: 10px 0 0;
  white-space: pre-wrap;
}
.scroll-note {
  color: var(--app-text-muted);
  font-size: 12px;
  line-height: 1.7;
  margin: 10px 0 0;
}
.event-card {
  border-bottom: 1px solid var(--app-border);
  padding: 20px 0;
}
.event-card:first-child {
  padding-top: 0;
}
.event-card header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
h3 {
  font-size: 16px;
  line-height: 1.6;
  margin: 0;
  font-weight: 600;
}
.event-card header :deep(.n-tag) {
  flex-shrink: 0;
  max-width: 70%;
}
.event-card header :deep(.n-tag__content) {
  white-space: normal;
}
dl {
  display: grid;
  gap: 10px;
  margin: 16px 0 0;
  font-size: 13px;
  line-height: 1.7;
}
dl > div {
  display: grid;
  grid-template-columns: 52px minmax(0, 1fr);
  gap: 12px;
}
dt {
  color: var(--app-text-muted);
}
dd {
  margin: 0;
  overflow-wrap: anywhere;
  font-variant-numeric: tabular-nums;
}
@media (max-width: 640px) {
  .events-table :deep(summary) {
    min-height: 44px;
    display: list-item;
    align-content: center;
  }
}
</style>
