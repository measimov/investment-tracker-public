<script setup lang="ts">
/**
 * 「公告」tab：交易所官方公告时间线（巨潮 / 披露易 / EDGAR，#306）。
 * 同日同类的文件合为一组，组可展开看全部文件；标题为交易所原文，链接到原文。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NEmpty, NSpin, NTag } from 'naive-ui'
import { ChevronDown } from '@lucide/vue'
import { useAliveGuard } from '@/composables/useAliveGuard'
import { formatDate, formatDateTime } from '@/utils/helpers'
import {
  ANNOUNCEMENT_CATEGORIES,
  IMPORTANCE_FILTER_OPTIONS,
  groupsByDate,
  importanceLabel,
  importanceTagType,
  safeAnnouncementUrl,
  sourceLabel,
  syncStatusText,
  type ImportanceFilter
} from '@/utils/announcements'
import { useAnnouncements } from './useAnnouncements'

const props = defineProps<{ symbol: string; market: string }>()

const { isUnmounted } = useAliveGuard()
const { state, load, loadMore, reset } = useAnnouncements({
  symbol: () => props.symbol,
  market: () => props.market,
  isUnmounted
})

const expanded = ref(new Set<string>())
const sections = computed(() => groupsByDate(state.groups))
const syncHint = computed(() => (state.sync ? syncStatusText(state.sync) : null))

function toggle(groupKey: string) {
  const next = new Set(expanded.value)
  if (next.has(groupKey)) next.delete(groupKey)
  else next.add(groupKey)
  expanded.value = next
}

function selectImportance(value: ImportanceFilter) {
  state.importance = value
  reload()
}

function reload() {
  expanded.value = new Set()
  load()
}

onMounted(load)
watch(
  () => [props.symbol, props.market],
  () => {
    reset()
    reload()
  }
)
</script>

<template>
  <section class="sd-block" data-testid="announcements-section">
    <div class="sd-block-header">
      <h3 class="sd-block-title">官方公告</h3>
      <div class="sd-block-actions announcement-filters">
        <div class="importance-options" role="group" aria-label="公告重要程度">
          <button
            v-for="option in IMPORTANCE_FILTER_OPTIONS"
            :key="option.value"
            type="button"
            :aria-pressed="state.importance === option.value"
            @click="selectImportance(option.value)"
          >
            {{ option.label }}
          </button>
        </div>
        <span class="category-field">
          <select
            v-model="state.category"
            aria-label="公告类别"
            class="category-select"
            @change="reload"
          >
            <option value="">全部类别</option>
            <option
              v-for="option in ANNOUNCEMENT_CATEGORIES"
              :key="option.value"
              :value="option.value"
            >
              {{ option.label }}
            </option>
          </select>
          <ChevronDown aria-hidden="true" />
        </span>
      </div>
    </div>

    <p
      v-if="syncHint"
      class="sd-meta-time"
      :class="{ 'sync-warning': syncHint.type === 'warning' }"
    >
      {{ syncHint.text }}
    </p>
    <NAlert v-if="state.error" type="error" :closable="false" class="section-alert"
      >{{ state.error }}
      <div class="alert-actions">
        <NButton :disabled="state.loading || state.loadingMore" @click="reload"
          >重新加载公告</NButton
        >
      </div></NAlert
    >

    <NSpin :show="state.loading" aria-label="正在加载官方公告"
      ><div class="announcement-timeline">
        <NEmpty
          v-if="!state.loading && !state.error && state.groups.length === 0"
          :description="state.sync?.sync_status === 'synced' ? '筛选范围内没有公告' : '暂无公告'"
        />
        <div v-for="section in sections" :key="section.date" class="announcement-day">
          <div class="announcement-date">{{ formatDate(section.date) }}</div>
          <div
            v-for="group in section.groups"
            :key="group.group_key"
            class="announcement-group"
            data-testid="announcement-group"
          >
            <div class="announcement-head">
              <NTag
                :type="
                  importanceTagType(group.importance) === 'danger'
                    ? 'error'
                    : importanceTagType(group.importance) === 'warning'
                      ? 'warning'
                      : 'default'
                "
                size="small"
                :bordered="false"
              >
                {{ importanceLabel(group.importance) }}
              </NTag>
              <NTag size="small" type="default" :bordered="false">{{ group.category_label }}</NTag>
              <a
                v-if="safeAnnouncementUrl(group.url)"
                :href="safeAnnouncementUrl(group.url)!"
                target="_blank"
                rel="noopener noreferrer"
                class="announcement-title"
              >
                {{ group.title }}
              </a>
              <span v-else class="announcement-title">{{ group.title }}</span>
            </div>
            <div class="announcement-meta sd-meta-time">
              {{ sourceLabel(group.source) }}
              <template v-if="group.document_count > 1">
                ·
                <NButton
                  text
                  size="small"
                  data-testid="announcement-toggle"
                  :aria-expanded="expanded.has(group.group_key)"
                  :aria-label="`${group.title}：${expanded.has(group.group_key) ? '收起文件' : '展开全部文件'}`"
                  @click="toggle(group.group_key)"
                >
                  {{ expanded.has(group.group_key) ? '收起' : `共 ${group.document_count} 份文件` }}
                </NButton>
              </template>
            </div>
            <ul v-if="expanded.has(group.group_key)" class="announcement-docs">
              <li v-for="doc in group.documents" :key="doc.url + doc.title">
                <span class="sd-meta-time">{{ formatDateTime(doc.published_at) }}</span>
                <a
                  v-if="safeAnnouncementUrl(doc.url)"
                  :href="safeAnnouncementUrl(doc.url)!"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {{ doc.title }}
                </a>
                <span v-else>{{ doc.title }}</span>
              </li>
            </ul>
          </div>
        </div>
        <div v-if="state.hasMore" class="announcement-more">
          <NButton size="small" :loading="state.loadingMore" @click="loadMore">
            加载更早的公告
          </NButton>
        </div>
      </div></NSpin
    >
    <p class="sd-footnote">
      来源：巨潮资讯（A/B 股）、披露易（港股）、SEC EDGAR（美股）。只同步持仓与观察清单里的标的；
      分类由标题规则判定，重要程度仅供参考，以原文为准。
    </p>
  </section>
</template>

<style scoped>
.announcement-filters {
  --announcement-filter-height: 36px;
  flex-wrap: wrap;
  gap: 8px;
}

.category-field {
  position: relative;
  display: inline-flex;
  width: 150px;
  max-width: 100%;
}
.category-field svg {
  position: absolute;
  right: 10px;
  top: 50%;
  transform: translateY(-50%);
  width: 14px;
  height: 14px;
  color: var(--app-text-muted);
  pointer-events: none;
}
.category-select {
  appearance: none;
  box-sizing: border-box;
  width: 100%;
  height: var(--announcement-filter-height);
  padding: 0 32px 0 10px;
  color: var(--app-text);
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 6px;
  font: inherit;
}
.importance-options {
  display: flex;
  gap: 4px;
}
.importance-options button {
  box-sizing: border-box;
  height: var(--announcement-filter-height);
  padding: 0 12px;
  background: var(--app-surface);
  color: var(--app-text-muted);
  border: 1px solid var(--app-border);
  border-radius: 6px;
  font: inherit;
  cursor: pointer;
}
.importance-options button[aria-pressed='true'] {
  background: var(--app-surface-secondary);
  color: var(--app-primary-strong);
  border-color: var(--app-primary);
}
.importance-options button:focus-visible,
.category-select:focus-visible {
  outline: 2px solid var(--app-primary);
  outline-offset: 2px;
}
@media (max-width: 640px) {
  .announcement-filters {
    --announcement-filter-height: 44px;
  }
}

.sync-warning {
  color: var(--app-warning-text);
}

.announcement-timeline {
  min-height: 80px;
}

.announcement-day + .announcement-day {
  margin-top: 12px;
}

.announcement-date {
  font-size: 12px;
  font-weight: 600;
  color: var(--app-text-soft);
  font-variant-numeric: tabular-nums;
  padding-bottom: 4px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.announcement-group {
  padding: 8px 0;
  border-bottom: 1px dashed var(--el-border-color-lighter);
}

.announcement-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px;
}

.announcement-title {
  flex: 1 1 240px;
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--app-text);
}

a.announcement-title:hover {
  color: var(--app-primary-strong);
}

.announcement-meta {
  margin-top: 2px;
}

.announcement-docs {
  margin: 6px 0 0;
  padding-left: 18px;
  display: grid;
  gap: 4px;
}

.announcement-docs li {
  overflow-wrap: anywhere;
}

.announcement-docs .sd-meta-time {
  margin-right: 6px;
}

.announcement-more {
  display: flex;
  justify-content: center;
  padding-top: 12px;
}
</style>
