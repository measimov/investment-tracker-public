<script setup lang="ts">
/**
 * 雪球帖子列表（按标的采集的公告/讨论）：时间 · 作者 · 互动数，
 * 标题/正文摘要，原帖与附件外链（只放行 http(s)）。纯展示，不接 LLM。
 */
import type { XueqiuFeedPost } from '@/types'
import { formatDateTime } from '@/utils/helpers'
import { isSafeExternalUrl, postExcerpt, postHeadline, postTimeMs, safeLinks } from './xueqiuFeed'

withDefaults(
  defineProps<{
    posts: XueqiuFeedPost[]
    emptyText?: string
    testid?: string
  }>(),
  { emptyText: '暂无数据', testid: 'xueqiu-post-list' }
)
</script>

<template>
  <div :data-testid="testid">
    <el-empty v-if="!posts.length" :description="emptyText" :image-size="48" />
    <ol v-else class="post-list">
      <li v-for="post in posts" :key="post.post_id" class="post-item">
        <div class="post-meta">
          <span>{{ formatDateTime(postTimeMs(post)) }}</span>
          <span v-if="post.author_name">· {{ post.author_name }}</span>
          <span v-if="post.reply_count">· 评 {{ post.reply_count }}</span>
          <span v-if="post.like_count">· 赞 {{ post.like_count }}</span>
          <el-link
            v-if="isSafeExternalUrl(post.url)"
            :href="post.url"
            target="_blank"
            rel="noopener noreferrer"
            type="primary"
            class="post-link"
          >
            原帖
          </el-link>
          <el-link
            v-for="(link, index) in safeLinks(post)"
            :key="link"
            :href="link"
            target="_blank"
            rel="noopener noreferrer"
            type="primary"
            class="post-link"
          >
            附件{{ safeLinks(post).length > 1 ? index + 1 : '' }}
          </el-link>
        </div>
        <div class="post-headline">{{ postHeadline(post) }}</div>
        <div v-if="postExcerpt(post)" class="post-excerpt">{{ postExcerpt(post) }}</div>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.post-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.post-item {
  padding: 8px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.post-item:last-child {
  border-bottom: none;
}
.post-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--app-text-muted);
}
.post-link {
  margin-left: 6px;
  font-size: 12px;
}
.post-headline {
  margin-top: 2px;
  font-size: 13px;
  word-break: break-word;
}
.post-excerpt {
  margin-top: 2px;
  font-size: 12px;
  color: var(--app-text-muted);
  word-break: break-word;
}
</style>
