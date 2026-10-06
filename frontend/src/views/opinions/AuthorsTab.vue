<script setup lang="ts">
import { computed } from 'vue'
import { NAlert, NButton } from 'naive-ui'
import OpinionAuthorFeed from '@/components/OpinionAuthorFeed.vue'
import type { OpinionFeedAuthor, OpinionFreshness } from '@/types'
import { formatDateTime } from '@/utils/helpers'
const props = defineProps<{
  authors: OpinionFeedAuthor[]
  loading: boolean
  hasLoaded: boolean
  loadError: string
  sourceAvailable: boolean | null
  freshness: OpinionFreshness | null
  days: number
}>()
defineEmits<{ retry: [] }>()
const emptyText = computed(() =>
  !props.hasLoaded
    ? props.loadError
      ? '作者动态尚未加载成功'
      : '作者动态尚未加载'
    : props.sourceAvailable === false
      ? '作者发言数据源当前不可用，不能确认窗口内发言'
      : `近 ${props.days} 天关注作者没有发言`
)
</script>
<template>
  <div class="author-toolbar">
    <p class="window-note">近 {{ days }} 天关注作者的全部发言，按作者分组（最新在前）。</p>
    <NButton :loading="loading" aria-label="重新加载作者动态" @click="$emit('retry')"
      >重新加载</NButton
    >
  </div>
  <NAlert v-if="loadError" type="error" :title="loadError">{{
    hasLoaded
      ? '保留上次成功的作者动态，未确认最新结果。'
      : '尚未确认作者动态，不能据此判断没有发言。'
  }}</NAlert>
  <p v-else-if="loading && hasLoaded" class="window-note">
    正在重新加载，当前保留上次成功的作者动态。
  </p>
  <NAlert v-if="sourceAvailable === false" type="warning" title="作者发言数据源当前不可用"
    >本次作者读取未确认来源可用，不能据此判断窗口内没有发言。</NAlert
  >
  <p v-if="freshness?.available" class="window-note">
    最近采集 {{ formatDateTime(freshness.latest_scan_at) }} · 最新发言
    {{ formatDateTime(freshness.latest_utterance_at) }}
  </p>
  <OpinionAuthorFeed :authors="authors" :loading="loading" :empty-text="emptyText" />
</template>
<style scoped>
.author-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}
.window-note {
  margin: 0 0 10px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--app-text-muted);
}
.author-toolbar .window-note {
  margin: 0;
}
.n-alert {
  margin-bottom: 12px;
}
@media (max-width: 640px) {
  .author-toolbar {
    align-items: flex-start;
  }
  .author-toolbar :deep(.n-button) {
    min-height: 44px;
  }
}
</style>
