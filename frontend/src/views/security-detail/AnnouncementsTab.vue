<script setup lang="ts">
/**
 * 「公告」tab：交易所官方公告时间线（巨潮 / 披露易 / EDGAR，#306）。
 * 同日同类的文件合为一组，组可展开看全部文件；标题为交易所原文，链接到原文。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useAliveGuard } from '@/composables/useAliveGuard'
import { formatDateTime } from '@/utils/helpers'
import {
  ANNOUNCEMENT_CATEGORIES,
  IMPORTANCE_FILTER_OPTIONS,
  groupsByDate,
  importanceLabel,
  importanceTagType,
  safeAnnouncementUrl,
  sourceLabel,
  syncStatusText
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
        <el-radio-group v-model="state.importance" size="small" @change="reload">
          <el-radio-button
            v-for="option in IMPORTANCE_FILTER_OPTIONS"
            :key="option.value"
            :value="option.value"
          >
            {{ option.label }}
          </el-radio-button>
        </el-radio-group>
        <el-select
          v-model="state.category"
          size="small"
          clearable
          placeholder="全部类别"
          class="category-select"
          @change="reload"
        >
          <el-option
            v-for="option in ANNOUNCEMENT_CATEGORIES"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
      </div>
    </div>

    <p
      v-if="syncHint"
      class="sd-meta-time"
      :class="{ 'sync-warning': syncHint.type === 'warning' }"
    >
      {{ syncHint.text }}
    </p>
    <el-alert
      v-if="state.error"
      :title="state.error"
      type="error"
      :closable="false"
      show-icon
      class="section-alert"
    />

    <div v-loading="state.loading" class="announcement-timeline">
      <el-empty
        v-if="!state.loading && !state.error && state.groups.length === 0"
        :description="state.sync?.sync_status === 'synced' ? '筛选范围内没有公告' : '暂无公告'"
        :image-size="72"
      />
      <div v-for="section in sections" :key="section.date" class="announcement-day">
        <div class="announcement-date">{{ section.date }}</div>
        <div
          v-for="group in section.groups"
          :key="group.group_key"
          class="announcement-group"
          data-testid="announcement-group"
        >
          <div class="announcement-head">
            <el-tag :type="importanceTagType(group.importance)" size="small" effect="plain">
              {{ importanceLabel(group.importance) }}
            </el-tag>
            <el-tag size="small" type="info" effect="plain">{{ group.category_label }}</el-tag>
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
              <el-button
                link
                size="small"
                type="primary"
                data-testid="announcement-toggle"
                @click="toggle(group.group_key)"
              >
                {{ expanded.has(group.group_key) ? '收起' : `共 ${group.document_count} 份文件` }}
              </el-button>
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
        <el-button size="small" :loading="state.loadingMore" @click="loadMore">
          加载更早的公告
        </el-button>
      </div>
    </div>
    <p class="sd-footnote">
      来源：巨潮资讯（A/B 股）、披露易（港股）、SEC EDGAR（美股）。只同步持仓与观察清单里的标的；
      分类由标题规则判定，重要程度仅供参考，以原文为准。
    </p>
  </section>
</template>

<style scoped>
.announcement-filters {
  flex-wrap: wrap;
  gap: 8px;
}

.category-select {
  width: 150px;
}

.sync-warning {
  color: var(--el-color-warning);
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
  color: var(--el-text-color-primary);
}

a.announcement-title:hover {
  color: var(--el-color-primary);
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
