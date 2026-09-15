<script setup lang="ts">
/**
 * 作者动态流（跨页共用：观点页「作者动态」tab + 标的详情页「相关作者动态」）。
 *
 * 逐作者折叠面板而非整块堆叠：高产作者的上百条会把其他作者压到几屏之外
 * （"只有管我财"反馈的展示侧根因）。默认全部收起，头部露出条数与最新日期，
 * 一眼可见有哪些作者。
 */
import { ref, watch } from 'vue'
import type { OpinionFeedAuthor } from '@/types'

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

function latestDate(group: OpinionFeedAuthor): string {
  return group.items[0]?.date?.slice(0, 10) || '—'
}
</script>

<template>
  <div v-loading="loading" class="feed" data-testid="opinion-author-feed">
    <el-empty
      v-if="!loading && !authors.length"
      :description="emptyText || '窗口内没有匹配到标的的发言'"
      :image-size="60"
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
            <span>{{ item.date ? item.date.slice(0, 10) : '—' }}</span>
            <el-tag size="small" effect="plain">{{ item.kind }}</el-tag>
            <el-tag
              v-for="ref in item.symbols"
              :key="`${ref.market}|${ref.symbol}`"
              size="small"
              type="info"
              effect="plain"
            >
              {{ ref.symbol }}
            </el-tag>
          </div>
          <div class="feed-text">{{ item.text }}</div>
          <div v-if="item.context" class="feed-context">↳ {{ item.context }}</div>
        </div>
      </el-collapse-item>
    </el-collapse>
  </div>
</template>

<style scoped>
.author-name {
  font-weight: 600;
  margin-right: 8px;
}
.author-meta {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.feed-item {
  padding: 8px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.feed-meta {
  display: flex;
  gap: 6px;
  align-items: center;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin-bottom: 4px;
}
.feed-text {
  white-space: pre-wrap;
  word-break: break-word;
}
.feed-context {
  margin-top: 4px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
