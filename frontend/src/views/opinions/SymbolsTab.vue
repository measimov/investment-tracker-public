<script setup lang="ts">
import { computed, h } from 'vue'
import { RouterLink } from 'vue-router'
import { NDataTable, NEmpty, NSpin, NTag, type DataTableColumns } from 'naive-ui'
import { useMediaQuery } from '@/composables/useMediaQuery'
import type { OpinionSummaryRow } from '@/types'
import { EMPTY, formatDate } from '@/utils/helpers'
import { opinionTagStyle } from './opinionTags'

const props = defineProps<{
  items: OpinionSummaryRow[]
  loading: boolean
  hasLoaded: boolean
  loadError: string
  sourceAvailable: boolean | null
}>()
const isMobile = useMediaQuery('(max-width: 640px)')
const ORIGIN_LABELS: Record<string, string> = {
  holding: '持仓',
  watchlist: '自选',
  both: '持仓+自选'
}
// 保留原表默认新发言降序；未知计数不是0，放在已知计数之后。
const rows = computed(() =>
  [...props.items].sort((a, b) => (b.new_utterance_count ?? -1) - (a.new_utterance_count ?? -1))
)
const emptyText = computed(() =>
  !props.hasLoaded
    ? props.loadError
      ? '观点概览尚未加载成功'
      : '正在加载观点概览'
    : props.sourceAvailable === false
      ? '雪球观点数据源当前不可用，尚不能确认近期发言'
      : '近期关注作者未提及任何持仓/自选标的'
)
const emptyTheme = { textColor: 'var(--app-text-muted)' }
function link(row: OpinionSummaryRow) {
  return `/securities/${encodeURIComponent(row.market)}/${encodeURIComponent(row.symbol)}`
}
function tagType(tag: string) {
  const type = opinionTagStyle(tag).type
  return type === 'info' ? 'default' : type
}
function tags(row: OpinionSummaryRow) {
  return row.tags.length
    ? h(
        'div',
        { class: 'opinion-tags' },
        row.tags.map((tag) =>
          h(
            NTag,
            {
              size: 'small',
              type: tagType(tag),
              bordered: opinionTagStyle(tag).change,
              color:
                opinionTagStyle(tag).effect === 'dark'
                  ? {
                      color:
                        tagType(tag) === 'warning'
                          ? 'var(--app-warning-text)'
                          : tagType(tag) === 'primary'
                            ? 'var(--app-primary-strong)'
                            : 'var(--app-text-muted)',
                      textColor: 'var(--app-on-primary)',
                      borderColor: 'transparent'
                    }
                  : undefined,
              class: { 'opinion-change': opinionTagStyle(tag).change }
            },
            () => opinionTagStyle(tag).label
          )
        )
      )
    : h('span', { class: 'muted' }, '未生成')
}
function summary(row: OpinionSummaryRow) {
  return row.summary && row.summary.length > 100
    ? h('details', { class: 'summary-detail' }, [
        h('summary', {}, row.summary.slice(0, 48) + '…'),
        h('p', {}, row.summary)
      ])
    : h('span', { class: 'summary-text' }, row.summary || EMPTY)
}
function newCount(row: OpinionSummaryRow) {
  return row.new_utterance_count === null
    ? EMPTY
    : row.new_utterance_count > 99
      ? '99+'
      : String(row.new_utterance_count)
}
const columns: DataTableColumns<OpinionSummaryRow> = [
  {
    title: '标的',
    key: 'symbol',
    width: 190,
    render: (row) =>
      h('div', { class: 'security-cell' }, [
        h(RouterLink, { to: link(row), class: 'symbol-link' }, () => row.name || row.symbol),
        h('span', { class: 'symbol-sub' }, `${row.symbol} · ${row.market}`)
      ])
  },
  {
    title: '来源',
    key: 'origin',
    width: 95,
    render: (row) =>
      h(
        NTag,
        { size: 'small', type: 'default', bordered: false },
        () => ORIGIN_LABELS[row.origin] || row.origin
      )
  },
  { title: '观点标签', key: 'tags', width: 200, render: tags },
  { title: '摘要', key: 'summary', width: 290, render: summary },
  {
    title: '发言',
    key: 'matched_count',
    width: 80,
    align: 'right',
    render: (row) => row.matched_count ?? EMPTY
  },
  {
    title: '新发言',
    key: 'new_utterance_count',
    width: 80,
    align: 'right',
    render: (row) =>
      row.new_utterance_count
        ? h(NTag, { type: 'warning', size: 'small', bordered: false }, () => newCount(row))
        : newCount(row)
  },
  { title: '生成时间', key: 'created_at', width: 115, render: (row) => formatDate(row.created_at) }
]
</script>

<template>
  <NSpin :show="loading" class="symbols-spin" data-testid="opinion-symbols-table">
    <NDataTable
      v-if="!isMobile && rows.length"
      :columns="columns"
      :data="rows"
      :row-key="(row) => `${row.market}|${row.symbol}`"
      :scroll-x="1050"
      size="small"
      class="symbols-table"
    />
    <div v-else-if="isMobile && rows.length" class="mobile-cards">
      <article v-for="row in rows" :key="`${row.market}|${row.symbol}`" class="opinion-card">
        <header>
          <RouterLink :to="link(row)" class="symbol-link">{{ row.name || row.symbol }}</RouterLink
          ><NTag size="small" type="default" :bordered="false">{{
            ORIGIN_LABELS[row.origin] || row.origin
          }}</NTag>
        </header>
        <p class="symbol-sub">{{ row.symbol }} · {{ row.market }}</p>
        <div class="opinion-tags">
          <NTag
            v-for="tag in row.tags"
            :key="tag"
            size="small"
            :type="tagType(tag)"
            :bordered="opinionTagStyle(tag).change"
            :color="
              opinionTagStyle(tag).effect === 'dark'
                ? {
                    color:
                      tagType(tag) === 'warning'
                        ? 'var(--app-warning-text)'
                        : tagType(tag) === 'primary'
                          ? 'var(--app-primary-strong)'
                          : 'var(--app-text-muted)',
                    textColor: 'var(--app-on-primary)',
                    borderColor: 'transparent'
                  }
                : undefined
            "
            :class="{ 'opinion-change': opinionTagStyle(tag).change }"
            >{{ opinionTagStyle(tag).label }}</NTag
          ><span v-if="!row.tags.length" class="muted">未生成</span>
        </div>
        <details v-if="row.summary && row.summary.length > 100" class="summary-detail">
          <summary>{{ row.summary.slice(0, 48) }}…</summary>
          <p>{{ row.summary }}</p>
        </details>
        <p v-else class="summary-text">{{ row.summary || EMPTY }}</p>
        <footer>
          <span
            >发言 {{ row.matched_count ?? EMPTY }} · 新发言
            <strong>{{ newCount(row) }}</strong></span
          ><span>{{ formatDate(row.created_at) }}</span>
        </footer>
      </article>
    </div>
    <NEmpty v-else :description="emptyText" :theme-overrides="emptyTheme" />
  </NSpin>
</template>

<style scoped>
.symbols-spin {
  width: 100%;
  min-width: 0;
}
.symbols-table :deep(.security-cell) {
  display: flex;
  flex-direction: column;
  gap: 5px;
  align-items: flex-start;
}
.symbols-table :deep(.symbol-link),
.symbol-link {
  color: var(--app-primary-strong);
  text-decoration: none;
  overflow-wrap: anywhere;
  font-weight: 600;
}
.symbols-table :deep(.symbol-link:hover),
.symbol-link:hover {
  text-decoration: underline;
}
.symbols-table :deep(.symbol-sub),
.symbol-sub {
  color: var(--app-text-muted);
  font-size: 12px;
}
.symbols-table :deep(.opinion-tags),
.opinion-tags {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.symbols-table :deep(.opinion-change),
.opinion-change {
  font-weight: 600;
}
.symbols-table :deep(.muted),
.muted {
  color: var(--app-text-muted);
}
.symbols-table :deep(.summary-detail),
.summary-detail,
.summary-text {
  overflow-wrap: anywhere;
  line-height: 1.8;
  white-space: pre-wrap;
}
.symbols-table :deep(.summary-detail summary),
.summary-detail summary {
  cursor: pointer;
}
.symbols-table :deep(.summary-detail p),
.summary-detail p {
  margin: 10px 0 0;
  font-family: var(--app-font-serif);
  font-size: 18px;
  line-height: 1.8;
  overflow-wrap: anywhere;
}
.symbol-link:focus-visible {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: 3px;
}
.opinion-card {
  padding: 20px 0;
  border-bottom: 1px solid var(--app-border-soft);
}
.opinion-card:first-child {
  padding-top: 0;
}
.opinion-card:last-child {
  border-bottom: 0;
  padding-bottom: 0;
}
.opinion-card header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.opinion-card .symbol-link {
  min-height: 44px;
  align-content: center;
  font-size: 17px;
}
.opinion-card .symbol-sub {
  margin: 0 0 12px;
}
.opinion-card .summary-text,
.opinion-card .summary-detail {
  margin: 16px 0;
  font-size: 16px;
}
.opinion-card .summary-detail summary {
  min-height: 44px;
}
.opinion-card footer {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 8px;
  color: var(--app-text-muted);
  font-size: 12px;
}
.opinion-card strong {
  color: var(--app-text);
  font-weight: 500;
}
@media (max-width: 640px) {
  .summary-detail p {
    font-size: 17px;
  }
}
</style>
