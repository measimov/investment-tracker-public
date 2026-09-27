<script setup lang="ts">
import OpinionAuthorFeed from '@/components/OpinionAuthorFeed.vue'
import type { OpinionFeedAuthor } from '@/types'

defineProps<{
  authors: OpinionFeedAuthor[]
  loading: boolean
  /** 窗口天数（与标的详情页「相关作者动态」的 90 天不同，这里跟随后端 recent_days） */
  days: number
}>()
</script>

<template>
  <p class="window-note">近 {{ days }} 天关注作者的全部发言，按作者分组（最新在前）。</p>
  <OpinionAuthorFeed
    :authors="authors"
    :loading="loading"
    :empty-text="`近 ${days} 天关注作者没有发言`"
  />
</template>

<style scoped>
.window-note {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--app-text-muted);
}
</style>
