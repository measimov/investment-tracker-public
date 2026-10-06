<script setup lang="ts">
/**
 * 作者动态流（跨页共用：观点页「作者动态」tab + 标的详情页「相关作者动态」）。
 *
 * 逐作者折叠面板而非整块堆叠：高产作者的上百条会把其他作者压到几屏之外
 * （"只有某作者"反馈的展示侧根因）。默认全部收起，头部露出条数与最新日期，
 * 一眼可见有哪些作者。
 */
import { ref, watch } from 'vue'
import { NEmpty, NSpin, NTag } from 'naive-ui'
import type { OpinionFeedAuthor } from '@/types'
import { EMPTY, formatDate, formatDateTime } from '@/utils/helpers'

const props = defineProps<{
  authors: OpinionFeedAuthor[]
  loading: boolean
  emptyText?: string
}>()

const openPanels = ref<string[]>([])

// 数据换批（切标的/刷新）时收起全部，避免留着上一批的展开态
watch(
  () => props.authors,
  () => {
    openPanels.value = []
  }
)

// 发言时间是带时区的 ISO 串：必须按本地时区格式化，切片会让北京时间 0-8 点的发言显示成前一天
function latestDate(group: OpinionFeedAuthor): string {
  const date = group.items[0]?.date
  return date ? formatDate(date) : EMPTY
}
</script>

<template>
  <NSpin :show="loading" class="feed" data-testid="opinion-author-feed">
    <NEmpty
      v-if="!loading && !authors.length"
      :description="emptyText || '窗口内没有发言'"
      :theme-overrides="{ textColor: 'var(--app-text-muted)' }"
    />
    <el-collapse v-else v-model="openPanels">
      <el-collapse-item v-for="group in authors" :key="group.author" :name="group.author">
        <template #title>
          <span class="author-name">{{ group.author }}</span>
          <span class="author-meta">
            {{ group.total }} 条<template v-if="group.items.length < group.total">
              （显示最新 {{ group.items.length }} 条）</template
            >
            · 最新 {{ latestDate(group) }}
          </span>
        </template>
        <div v-for="(item, index) in group.items" :key="index" class="feed-item">
          <div class="feed-meta">
            <span>{{ formatDateTime(item.date) }}</span>
            <NTag size="small">{{ item.kind }}</NTag>
            <NTag
              v-for="ref in item.symbols"
              :key="`${ref.market}|${ref.symbol}`"
              size="small"
              type="default"
            >
              {{ ref.symbol }}
            </NTag>
          </div>
          <div class="feed-text">{{ item.text }}</div>
          <div v-if="item.context" class="feed-context">{{ item.context }}</div>
        </div>
      </el-collapse-item>
    </el-collapse>
  </NSpin>
</template>

<style scoped>
.author-name {
  font-family: var(--app-font-sans);
  font-size: 15px;
  font-weight: 600;
  margin-right: 8px;
  overflow-wrap: anywhere;
}
.author-meta {
  color: var(--app-text-muted);
  font-family: var(--app-font-sans);
  font-size: 13px;
  font-weight: 400;
}
.feed-item {
  padding: 8px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.feed-meta {
  display: flex;
  gap: 6px;
  align-items: center;
  flex-wrap: wrap;
  color: var(--app-text-muted);
  font-family: var(--app-font-sans);
  font-size: 13px;
  font-weight: 400;
  margin-bottom: 4px;
}
.feed-meta :deep(.n-tag) {
  font-size: 13px;
}
.feed {
  width: 100%;
  min-width: 0;
}
.feed :deep(.el-collapse-item__header) {
  height: auto;
  min-height: 48px;
  padding: 12px 0;
  line-height: 1.7;
  flex-wrap: wrap;
}
.feed :deep(.el-collapse-item__title) {
  display: flex;
  flex: 1;
  min-width: 0;
  align-items: baseline;
  flex-wrap: wrap;
}
.feed :deep(.el-collapse-item__arrow) {
  flex-shrink: 0;
}
.feed-text {
  max-width: 38em;
  font-family: var(--app-font-serif);
  font-size: 17px;
  line-height: 1.8;
  white-space: pre-wrap;
  word-break: break-word;
}
.feed-context {
  max-width: 38em;
  font-family: var(--app-font-serif);
  margin-top: 8px;
  padding-left: 12px;
  border-left: 1px solid var(--app-border-soft);
  color: var(--app-text-muted);
  font-size: 15px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
