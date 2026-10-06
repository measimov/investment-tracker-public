<script setup lang="ts">
import { computed, h } from 'vue'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { NDataTable, NEmpty, NSpin, NTag, type DataTableColumns } from 'naive-ui'
import type { AlertItem } from '@/types'
import { EMPTY, formatDateTime } from '@/utils/helpers'
import {
  durationText,
  notifyStatusText,
  severityLabel,
  severityTagType,
  sourceLabel
} from '../systemAlerts'
const props = defineProps<{
  rows: AlertItem[]
  loading: boolean
  emptyDescription: string
  resolved?: boolean
  minSeverity: string
  nowIso: string
}>()
const mobile = useMediaQuery('(max-width: 640px)')
const tableLabel = computed(() => (props.resolved ? '已恢复告警明细' : '当前告警明细'))
function tagType(row: AlertItem) {
  const type = severityTagType(row.severity)
  return type === 'danger' ? 'error' : type === 'info' ? 'default' : type
}
function message(text: string) {
  return text.length > 100
    ? h('details', { class: 'message-details' }, [
        h('summary', {}, text.slice(0, 80) + '…'),
        h('p', {}, text)
      ])
    : h('p', { class: 'alert-message' }, text)
}
const columns = computed<DataTableColumns<AlertItem>>(() => {
  const cols: DataTableColumns<AlertItem> = [
    {
      key: 'severity',
      title: '级别',
      width: 90,
      render: (row) =>
        h(
          NTag,
          { size: 'small', bordered: false, type: tagType(row) },
          { default: () => severityLabel(row.severity) }
        )
    },
    {
      key: 'content',
      title: '告警',
      width: 340,
      render: (row) =>
        h('div', {}, [
          h('strong', { class: 'alert-title' }, row.title),
          row.message ? message(row.message) : null
        ])
    },
    { key: 'source', title: '来源', width: 120, render: (row) => sourceLabel(row.source) },
    {
      key: 'time',
      title: props.resolved ? '恢复时间' : '首次发现',
      width: 180,
      render: (row) =>
        h('div', {}, [
          h('span', {}, formatDateTime(props.resolved ? row.resolved_at : row.first_seen_at)),
          h(
            'p',
            { class: 'subline' },
            `${props.resolved ? '持续' : '已持续'} ${durationText(row.first_seen_at, props.resolved ? row.resolved_at : props.nowIso) || EMPTY}`
          )
        ])
    }
  ]
  if (!props.resolved)
    cols.push({
      key: 'notify',
      title: '推送',
      width: 190,
      render: (row) =>
        h('div', {}, [
          h('span', {}, notifyStatusText(row, props.minSeverity)),
          row.last_notified_at
            ? h('p', { class: 'subline' }, `最近 ${formatDateTime(row.last_notified_at)}`)
            : null
        ])
    })
  return cols
})
</script>
<template>
  <NSpin :show="loading">
    <div class="alerts-table">
      <div
        v-if="!mobile"
        class="table-scroll"
        tabindex="0"
        role="region"
        :aria-label="`${tableLabel}，可横向滚动`"
      >
        <NDataTable
          :columns="columns"
          :data="rows"
          :row-key="(row: AlertItem) => row.alert_key"
          :single-line="true"
          :bordered="true"
          :style="{ minWidth: resolved ? '730px' : '920px' }"
          :aria-label="tableLabel"
        >
          <template #empty
            ><NEmpty
              :description="emptyDescription"
              :theme-overrides="{ textColor: 'var(--app-text-muted)' }"
          /></template>
        </NDataTable>
      </div>
      <p v-if="!mobile" class="scroll-note">表格可横向滚动查看完整时间与推送状态。</p>
      <template v-else>
        <NEmpty
          v-if="!rows.length"
          :description="emptyDescription"
          :theme-overrides="{ textColor: 'var(--app-text-muted)' }"
        />
        <article
          v-for="row in rows"
          :key="row.alert_key"
          class="alert-card"
          data-testid="alert-card"
        >
          <header>
            <h3>{{ row.title }}</h3>
            <NTag size="small" :bordered="false" :type="tagType(row)">{{
              severityLabel(row.severity)
            }}</NTag>
          </header>
          <details v-if="row.message && row.message.length > 100" class="message-details">
            <summary>{{ row.message.slice(0, 80) }}…</summary>
            <p>{{ row.message }}</p>
          </details>
          <p v-else-if="row.message" class="alert-message">{{ row.message }}</p>
          <dl>
            <div>
              <dt>来源</dt>
              <dd>{{ sourceLabel(row.source) }}</dd>
            </div>
            <div>
              <dt>{{ resolved ? '恢复时间' : '首次发现' }}</dt>
              <dd>{{ formatDateTime(resolved ? row.resolved_at : row.first_seen_at) }}</dd>
            </div>
            <div>
              <dt>{{ resolved ? '持续' : '已持续' }}</dt>
              <dd>
                {{ durationText(row.first_seen_at, resolved ? row.resolved_at : nowIso) || EMPTY }}
              </dd>
            </div>
            <template v-if="!resolved"
              ><div>
                <dt>推送</dt>
                <dd>{{ notifyStatusText(row, minSeverity) }}</dd>
              </div>
              <div v-if="row.last_notified_at">
                <dt>最近推送</dt>
                <dd>{{ formatDateTime(row.last_notified_at) }}</dd>
              </div></template
            >
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
.alerts-table :deep(summary:focus-visible) {
  outline: 2px solid var(--app-primary);
  outline-offset: 3px;
}
.alerts-table :deep(.alert-title) {
  display: block;
  font-size: 14px;
  font-weight: 600;
  overflow-wrap: anywhere;
}
.alerts-table :deep(.alert-message),
.alerts-table :deep(.message-details),
.alerts-table :deep(.subline) {
  color: var(--app-text-muted);
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  margin: 6px 0 0;
}
.alerts-table :deep(summary) {
  cursor: pointer;
  min-height: 24px;
}
.alerts-table :deep(.message-details p) {
  margin: 10px 0 0;
  white-space: pre-wrap;
}
.scroll-note {
  color: var(--app-text-muted);
  font-size: 12px;
  line-height: 1.7;
  margin: 10px 0 0;
}
.alert-card {
  border-bottom: 1px solid var(--app-border);
  padding: 20px 0;
}
.alert-card:first-child {
  padding-top: 0;
}
.alert-card header {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  justify-content: space-between;
}
h3 {
  margin: 0;
  font-size: 16px;
  line-height: 1.6;
  font-weight: 600;
  overflow-wrap: anywhere;
}
.alert-card header :deep(.n-tag) {
  flex-shrink: 0;
}
dl {
  margin: 16px 0 0;
  display: grid;
  gap: 10px;
  font-size: 13px;
  line-height: 1.7;
}
dl > div {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
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
  .alerts-table :deep(summary) {
    min-height: 44px;
    display: list-item;
    align-content: center;
  }
}
</style>
