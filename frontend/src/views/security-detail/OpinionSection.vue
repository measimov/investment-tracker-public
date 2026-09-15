<script setup lang="ts">
import { onMounted, watch } from 'vue'
import OpinionAuthorFeed from '@/components/OpinionAuthorFeed.vue'
import { useAliveGuard } from '@/composables/useAliveGuard'
import { renderMarkdown } from '@/utils/markdown'
import { opinionTagType } from '../opinions/useOpinions'
import { OPINION_FEED_DAYS, useOpinionFeed } from './useOpinionFeed'
import { useOpinionSummary } from './useOpinionSummary'

const props = defineProps<{ symbol: string; market: string }>()

const { isUnmounted } = useAliveGuard()
const { state, load, generate } = useOpinionSummary({
  symbol: () => props.symbol,
  market: () => props.market,
  isUnmounted
})

const CHANGE_LABELS: Record<string, string> = {
  转多: '转多',
  转空: '转空',
  新增: '新增关注',
  无: '—'
}

// 相关作者动态：折叠区首次展开才拉取（取数/所有权/防重复见 useOpinionFeed）
const feed = useOpinionFeed({
  symbol: () => props.symbol,
  market: () => props.market,
  isUnmounted
})

function resetAndLoad() {
  feed.reset()
  load()
}

onMounted(load)
// 同业跳转复用组件实例：路由参数变了必须重拉（世代守卫在 composable 内），
// 作者动态一并重置为未加载
watch(() => [props.symbol, props.market], resetAndLoad)
</script>

<template>
  <section class="data-section" data-testid="opinion-section">
    <div class="section-header">
      <h3>雪球观点</h3>
      <el-button
        size="small"
        type="primary"
        plain
        data-testid="generate-opinion-button"
        :loading="state.generating"
        @click="generate"
      >
        {{ state.summary ? '重新生成观点摘要' : '生成观点摘要' }}
      </el-button>
    </div>

    <el-alert
      v-if="state.notice"
      type="info"
      :closable="false"
      :title="state.notice"
      class="section-alert"
    />
    <el-alert
      v-if="state.error"
      type="error"
      :closable="false"
      :title="state.error"
      class="section-alert"
    />
    <el-progress
      v-if="state.generating && state.job"
      :percentage="
        Math.round(((Number(state.job.completed) || 0) / (Number(state.job.total) || 1)) * 100)
      "
      :stroke-width="6"
    >
      <span class="stage-label">{{ state.job.stage_label || '进行中' }}</span>
    </el-progress>

    <div v-if="state.summary" v-loading="state.loading">
      <div class="tags-line">
        <el-tag
          v-for="tag in state.summary.tags"
          :key="tag"
          :type="opinionTagType(tag)"
          class="opinion-tag"
        >
          {{ tag }}
        </el-tag>
        <span v-if="state.summary.previous" class="previous-line">
          较上次（{{ state.summary.previous.created_at?.slice(0, 10) }}）：
          {{ state.summary.previous.tags.join('、') || '无标签' }} →
          {{ state.summary.tags.join('、') }}
        </span>
      </div>
      <p class="summary-line">{{ state.summary.summary }}</p>

      <el-table
        v-if="state.summary.author_stances.length"
        :data="state.summary.author_stances"
        size="small"
        class="stance-table"
      >
        <el-table-column prop="author" label="作者" width="140" />
        <el-table-column prop="stance" label="立场" width="80" />
        <el-table-column label="近期变化" width="100">
          <template #default="{ row }">
            {{ CHANGE_LABELS[row.recent_change] || row.recent_change }}
          </template>
        </el-table-column>
        <el-table-column prop="evidence" label="原文引述" min-width="220" show-overflow-tooltip />
      </el-table>

      <!-- LLM Markdown 必须过 renderMarkdown（marked + DOMPurify 消毒）后才可 v-html -->
      <div class="markdown-body" v-html="renderMarkdown(state.summary.content)" />
      <p class="footnote">
        {{ state.summary.model }}
        <template v-if="state.summary.total_tokens">
          · {{ state.summary.total_tokens }} tokens</template
        >
        · 覆盖 {{ state.summary.utterance_count }} 条发言（近 {{ state.summary.recent_days }} 天
        {{ state.summary.recent_utterance_count }} 条） · 生成于
        {{ state.summary.created_at?.slice(0, 16).replace('T', ' ') }}
      </p>
    </div>
    <el-empty
      v-else-if="!state.loading && !state.generating"
      description="暂无观点摘要；点击右上角生成（汇总关注作者的雪球发言，调用 LLM）"
      :image-size="60"
    />

    <!-- 与摘要无关的原始发言流：没生成摘要时也可看（v-if 链之外的独立兄弟） -->
    <el-collapse v-model="feed.openPanels.value" class="feed-collapse" @change="feed.onToggle">
      <el-collapse-item name="feed" data-testid="opinion-related-feed">
        <template #title>相关作者动态（近 {{ OPINION_FEED_DAYS }} 天原始发言）</template>
        <OpinionAuthorFeed
          :authors="feed.authors.value"
          :loading="feed.loading.value"
          empty-text="近 90 天关注作者未提及该标的"
        />
      </el-collapse-item>
    </el-collapse>
  </section>
</template>

<style scoped>
.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.section-alert {
  margin: 8px 0;
}
.tags-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin: 8px 0;
}
.opinion-tag {
  margin-right: 2px;
}
.previous-line {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.summary-line {
  margin: 4px 0 10px;
}
.stance-table {
  margin-bottom: 12px;
}
.stage-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.footnote {
  margin-top: 10px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.feed-collapse {
  margin-top: 12px;
}
</style>
