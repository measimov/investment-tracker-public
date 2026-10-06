<script setup lang="ts">
import { h, computed } from 'vue'
import { RouterLink } from 'vue-router'
import { useWindowSize } from '@vueuse/core'
import { NButton, NDataTable, NEmpty, NPopover, NSpin, NTag, type DataTableColumns } from 'naive-ui'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { EMPTY, formatDate, formatPrice, pnlClass, todayLocalISODate } from '@/utils/helpers'
import type { announcementBadge } from '@/utils/announcements'
import type { WatchlistItem } from '@/types'
import { grahamSummaryTagType } from '../security-detail/grahamFormat'
import ResearchNote from '../security-detail/ResearchNote.vue'
import { describePrice } from '../holdings/display'
import { describeChangeSinceAdded, missingChangeReason } from './priceChange'

const props = defineProps<{
  items: WatchlistItem[]
  loading: boolean
  emptyDescription: string
  badgeFor: (row: WatchlistItem) => ReturnType<typeof announcementBadge>
}>()
const emit = defineEmits<{ edit: [row: WatchlistItem]; remove: [row: WatchlistItem] }>()
const isMobileView = useMediaQuery('(max-width: 640px)')
const { width } = useWindowSize()
const explanationWidth = computed(() => Math.min(320, width.value - 48))
const grahamExplanation =
  '“未同步档案”表示暂未取得准则判定；“不可判定”表示数据源边界，均不等于不达标。点击代码可阅读标的档案、AI 分析及财报摘要。'
const deleteTheme = {
  textColorTextHoverError: 'var(--app-danger-text)',
  textColorTextPressedError: 'var(--app-danger-text)',
  textColorTextFocusError: 'var(--app-danger-text)'
}

function detailLink(row: WatchlistItem) {
  return `/securities/${encodeURIComponent(row.market)}/${encodeURIComponent(row.symbol)}`
}
function priceInfoOf(row: WatchlistItem) {
  return describePrice(
    {
      price:
        row.current_price === null || row.current_price === undefined
          ? null
          : Number(row.current_price),
      priceAsOf: row.price_as_of,
      priceUpdatedAt: row.price_updated_at,
      priceSource: row.price_source
    },
    todayLocalISODate()
  )
}
function grahamText(row: WatchlistItem) {
  const summary = row.graham_summary
  return summary
    ? `达标 ${summary.passed} / 不达标 ${summary.failed} / 不可判定 ${summary.indeterminate}（数据年度 ${summary.as_of_year ?? '未知'}，详情见标的档案）`
    : '生成 AI 分析或同步档案后可见准则判定'
}
function grahamType(row: WatchlistItem) {
  const type = row.graham_summary ? grahamSummaryTagType(row.graham_summary) : 'info'
  return type === 'info' ? 'default' : type
}
function explanation(
  label: string,
  lines: string[],
  text: string,
  placement: 'bottom-start' | 'bottom-end' = 'bottom-end',
  className = ''
) {
  return h(
    NPopover,
    { trigger: 'click', placement, width: explanationWidth.value },
    {
      trigger: () =>
        h(
          'button',
          { type: 'button', class: ['explanation-trigger', className], 'aria-label': label },
          text
        ),
      default: () =>
        lines.map((line) =>
          h(
            'p',
            { class: 'explanation-line', style: { margin: '0 0 6px', overflowWrap: 'anywhere' } },
            line
          )
        )
    }
  )
}
function actions(row: WatchlistItem) {
  return h('div', { class: 'row-actions' }, [
    h(
      NButton,
      {
        text: true,
        type: 'primary',
        'aria-label': `编辑 ${row.symbol} 观察标的`,
        onClick: () => emit('edit', row)
      },
      () => '编辑'
    ),
    h(
      NButton,
      {
        text: true,
        type: 'error',
        themeOverrides: deleteTheme,
        'aria-label': `移出 ${row.symbol} 观察标的`,
        onClick: () => emit('remove', row)
      },
      () => '移出'
    )
  ])
}
const columns: DataTableColumns<WatchlistItem> = [
  {
    title: '标的',
    key: 'symbol',
    width: 180,
    render: (row) => {
      const badge = props.badgeFor(row)
      return h('div', { class: 'security-cell' }, [
        h(RouterLink, { to: detailLink(row), class: 'symbol-link' }, () => row.symbol),
        h('span', { class: 'security-name' }, row.name || EMPTY),
        badge
          ? explanation(
              `查看 ${row.symbol} 近期公告`,
              badge.lines,
              badge.text,
              'bottom-start',
              'announcement-badge'
            )
          : null
      ])
    }
  },
  { title: '市场', key: 'market', width: 80 },
  {
    title: '现价',
    key: 'current_price',
    width: 150,
    align: 'right',
    render: (row) => {
      const info = priceInfoOf(row)
      return h('div', { 'data-testid': 'watchlist-price' }, [
        h('div', { class: 'num' }, formatPrice(row.current_price)),
        explanation(
          `查看 ${row.symbol} 行情来源`,
          info?.tooltip || [missingChangeReason({ current_price: null })],
          info?.label || '尚未取到现价',
          'bottom-end',
          info?.stale ? 'is-stale' : 'cell-sub'
        )
      ])
    }
  },
  {
    title: '加入以来',
    key: 'change_since_added_pct',
    width: 120,
    align: 'right',
    render: (row) => {
      const change = describeChangeSinceAdded(row)
      return h(
        'div',
        { 'data-testid': 'watchlist-change', class: pnlClass(row.change_since_added_pct) },
        [
          explanation(
            `查看 ${row.symbol} 加入以来涨跌口径`,
            change?.tooltip || [missingChangeReason(row)],
            change?.text || EMPTY,
            'bottom-end',
            `num ${pnlClass(row.change_since_added_pct)}`
          )
        ]
      )
    }
  },
  {
    title: '观察理由',
    key: 'note',
    width: 260,
    render: (row) => h('span', { class: 'note-text' }, row.note || EMPTY)
  },
  {
    title: () =>
      h('div', { class: 'graham-heading' }, [
        h('span', '格雷厄姆准则'),
        h(ResearchNote, {
          popover: true,
          label: '格雷厄姆准则判定说明',
          text: grahamExplanation
        })
      ]),
    key: 'graham_summary',
    width: 160,
    render: (row) =>
      h(
        NPopover,
        { trigger: 'click', placement: 'bottom-end', width: explanationWidth.value },
        {
          trigger: () =>
            h(
              'button',
              {
                type: 'button',
                class: 'explanation-trigger',
                'aria-label': `查看 ${row.symbol} 准则判定说明`
              },
              [
                row.graham_summary
                  ? h(
                      NTag,
                      { type: grahamType(row), size: 'small', bordered: false },
                      () => `达标 ${row.graham_summary!.passed} / ${row.graham_summary!.total}`
                    )
                  : '未同步档案'
              ]
            ),
          default: () => grahamText(row)
        }
      )
  },
  { title: '加入日期', key: 'created_at', width: 115, render: (row) => formatDate(row.created_at) },
  { title: '操作', key: 'actions', width: 130, render: actions }
]
</script>

<template>
  <NDataTable
    v-if="!isMobileView"
    :columns="columns"
    :data="items"
    :loading="loading"
    :row-key="(row: WatchlistItem) => row.id"
    :scroll-x="1195"
    :bordered="false"
    :theme-overrides="{ tdPaddingMedium: '8px 14px' }"
    class="watchlist-table desktop-data-table"
    data-testid="watchlist-table"
  >
    <template #empty><NEmpty :description="emptyDescription" /></template>
  </NDataTable>
  <NSpin v-else :show="loading" class="mobile-spin">
    <div class="mobile-card-list">
      <article v-for="row in items" :key="row.id" class="mobile-card" data-testid="watchlist-card">
        <div class="mobile-card-head">
          <RouterLink :to="detailLink(row)" class="mobile-card-title symbol-link">
            <span class="mobile-card-symbol">{{ row.symbol }}</span>
            <span v-if="row.name" class="mobile-card-name">{{ row.name }}</span>
          </RouterLink>
          <NTag size="small" :bordered="false">{{ row.market }}</NTag>
        </div>
        <p v-if="row.note" class="observation-note">{{ row.note }}</p>
        <div class="card-metrics">
          <div>
            <span class="metric-label">现价</span>
            <span class="num price-value" data-testid="watchlist-price">{{
              formatPrice(row.current_price)
            }}</span>
            <details class="metric-explanation">
              <summary :class="{ 'is-stale': priceInfoOf(row)?.stale }">
                {{ priceInfoOf(row)?.label || '尚未取到现价' }}
              </summary>
              <p
                v-for="line in priceInfoOf(row)?.tooltip || [
                  missingChangeReason({ current_price: null })
                ]"
                :key="line"
              >
                {{ line }}
              </p>
            </details>
          </div>
          <div>
            <span class="metric-label">加入以来</span>
            <span
              class="num price-value"
              :class="pnlClass(row.change_since_added_pct)"
              data-testid="watchlist-change"
              >{{ describeChangeSinceAdded(row)?.text || EMPTY }}</span
            >
            <details class="metric-explanation">
              <summary>涨跌口径</summary>
              <p
                v-for="line in describeChangeSinceAdded(row)?.tooltip || [missingChangeReason(row)]"
                :key="line"
              >
                {{ line }}
              </p>
            </details>
          </div>
        </div>
        <details class="card-detail">
          <summary>
            <NTag v-if="row.graham_summary" :type="grahamType(row)" size="small" :bordered="false"
              >达标 {{ row.graham_summary.passed }} / {{ row.graham_summary.total }}</NTag
            >
            <span v-else>未同步档案</span>
            <span class="detail-hint">准则说明</span>
          </summary>
          <p>{{ grahamText(row) }}</p>
          <p>{{ grahamExplanation }}</p>
        </details>
        <details v-if="badgeFor(row)" class="card-detail announcement-detail">
          <summary data-testid="watchlist-card-announcement">{{ badgeFor(row)!.text }}</summary>
          <p v-for="line in badgeFor(row)!.lines" :key="line">{{ line }}</p>
        </details>
        <div class="card-footer">
          <span class="joined-date">加入 {{ formatDate(row.created_at) }}</span>
          <div class="row-actions">
            <NButton
              text
              type="primary"
              :aria-label="`编辑 ${row.symbol} 观察标的`"
              @click="emit('edit', row)"
              >编辑</NButton
            >
            <NButton
              text
              type="error"
              :theme-overrides="deleteTheme"
              :aria-label="`移出 ${row.symbol} 观察标的`"
              @click="emit('remove', row)"
              >移出</NButton
            >
          </div>
        </div>
      </article>
      <NEmpty v-if="items.length === 0" :description="emptyDescription" />
    </div>
  </NSpin>
</template>

<style scoped>
.watchlist-table :deep(.security-cell) {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}
.watchlist-table :deep(.security-name) {
  color: var(--app-text-muted);
  overflow-wrap: anywhere;
  font-size: 12px;
}
.watchlist-table :deep(.symbol-link),
.symbol-link {
  color: var(--app-primary-strong);
  text-decoration: none;
}
.watchlist-table :deep(.symbol-link:hover),
.symbol-link:hover {
  text-decoration: underline;
}
.watchlist-table :deep(.explanation-trigger) {
  border: 0;
  padding: 2px 0;
  background: transparent;
  color: inherit;
  font: inherit;
  cursor: pointer;
  min-height: 24px;
  text-decoration: underline;
  text-decoration-style: dotted;
  text-underline-offset: 4px;
}
.watchlist-table :deep(.cell-sub) {
  color: var(--app-text-soft);
  font-size: 12px;
}
.watchlist-table :deep(.is-stale),
.is-stale {
  color: var(--app-warning-text);
  font-size: 12px;
}
.watchlist-table :deep(.announcement-badge),
.announcement-detail {
  color: var(--app-danger-text);
  font-size: 12px;
}
.watchlist-table :deep(.row-actions),
.row-actions {
  display: flex;
  gap: 14px;
}
.watchlist-table :deep(.explanation-trigger:focus-visible),
.symbol-link:focus-visible {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: 3px;
}
.explanation-line {
  margin: 0 0 6px;
  overflow-wrap: anywhere;
}
.explanation-line:last-child {
  margin-bottom: 0;
}
.mobile-card {
  min-width: 0;
}
.mobile-card-name {
  white-space: normal;
  overflow: visible;
}
.mobile-card-symbol {
  color: var(--app-primary-strong);
}
.watchlist-table :deep(.graham-heading) {
  display: flex;
  align-items: center;
  gap: 6px;
}
.watchlist-table :deep(.note-text) {
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
.mobile-spin {
  width: 100%;
}
.mobile-card-title {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  min-height: 44px;
}
.mobile-card-name {
  color: var(--app-text-muted);
  font-size: 14px;
  overflow-wrap: anywhere;
}
.mobile-card-symbol {
  overflow-wrap: anywhere;
}
.observation-note {
  color: var(--app-text);
  margin: 16px 0;
  line-height: 1.75;
  overflow-wrap: anywhere;
}
.card-metrics {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-top: 14px;
}
.card-metrics > div {
  min-width: 0;
}
.metric-label {
  display: block;
  color: var(--app-text-muted);
  font-size: 12px;
  margin-bottom: 6px;
}
.price-value {
  display: block;
  font-size: 22px;
  overflow-wrap: anywhere;
}
.metric-explanation,
.card-detail {
  font-size: 12px;
  color: var(--app-text-muted);
}
.metric-explanation summary,
.card-detail summary {
  min-height: 44px;
  cursor: pointer;
  align-content: center;
}
.metric-explanation p,
.card-detail p {
  line-height: 1.7;
  margin: 4px 0 12px;
  overflow-wrap: anywhere;
}
.card-detail {
  border-top: 1px solid var(--app-border);
}
.detail-hint {
  margin-left: 6px;
}
.card-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.joined-date {
  font-size: 12px;
  color: var(--app-text-muted);
}
.row-actions :deep(.n-button) {
  min-height: 44px;
  min-width: 44px;
}
.watchlist-table :deep(.row-actions .n-button) {
  min-height: 24px;
}
</style>
