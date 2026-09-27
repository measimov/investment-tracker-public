<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import OpinionAuthorFeed from '@/components/OpinionAuthorFeed.vue'
import { useAliveGuard } from '@/composables/useAliveGuard'
import { renderMarkdown } from '@/utils/markdown'
import { formatDate, formatDateTime } from '@/utils/helpers'
import { opinionTagStyle } from '../opinions/opinionTags'
import XueqiuPostList from '../opinions/XueqiuPostList.vue'
import { feedFreshnessHint } from '../opinions/xueqiuFeed'
import { daysAgoText } from './format'
import { OPINION_FEED_DAYS, useOpinionFeed } from './useOpinionFeed'
import { useOpinionSummary } from './useOpinionSummary'
import { XUEQIU_FEED_LIMIT, useXueqiuSymbolFeed } from './useXueqiuSymbolFeed'

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

// 雪球公告 / 讨论（采集器每日按标的落库）：同样首次展开才拉取
const xueqiu = useXueqiuSymbolFeed({
  symbol: () => props.symbol,
  market: () => props.market,
  isUnmounted
})
const xueqiuTab = ref('announcement')
const xueqiuHint = computed(() =>
  xueqiu.loaded.value ? feedFreshnessHint(xueqiu.lastCycleFinishedAt.value) : ''
)

function resetAndLoad() {
  feed.reset()
  xueqiu.reset()
  load()
}

onMounted(load)
// 同业跳转复用组件实例：路由参数变了必须重拉（世代守卫在 composable 内），
// 作者动态一并重置为未加载
watch(() => [props.symbol, props.market], resetAndLoad)
</script>

<template>
  <section class="sd-block" data-testid="opinion-section">
    <div class="sd-block-header">
      <h3 class="sd-block-title">雪球观点</h3>
      <el-button
        size="small"
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
      <span class="sd-meta-time">{{ state.job.stage_label || '进行中' }}</span>
    </el-progress>

    <div v-if="state.summary" v-loading="state.loading">
      <!-- 与 AI 分析同一元信息条：标签 + 窗口/最新发言 + 生成时间 -->
      <div class="sd-meta-row">
        <el-tag
          v-for="tag in state.summary.tags"
          :key="tag"
          size="small"
          :type="opinionTagStyle(tag).type"
          :effect="opinionTagStyle(tag).effect"
          :class="{ 'opinion-change': opinionTagStyle(tag).change }"
        >
          {{ opinionTagStyle(tag).label }}
        </el-tag>
        <span class="sd-meta-time" data-testid="opinion-window">
          近 {{ state.summary.lookback_days }} 天 · 最新发言
          {{ formatDateTime(state.summary.latest_utterance_at) }}
        </span>
        <span class="sd-meta-time">
          生成于 {{ formatDateTime(state.summary.created_at) }}
          <template v-if="daysAgoText(state.summary.created_at)"
            >（{{ daysAgoText(state.summary.created_at) }}）</template
          >
        </span>
        <p class="sd-meta-summary">{{ state.summary.summary }}</p>
        <span v-if="state.summary.previous" class="sd-meta-time">
          较上次（{{ formatDate(state.summary.previous.created_at) }}）：
          {{ state.summary.previous.tags.join('、') || '无标签' }} →
          {{ state.summary.tags.join('、') }}
        </span>
      </div>

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
      <p class="sd-footnote">
        {{ state.summary.model }}
        <template v-if="state.summary.total_tokens">
          · {{ state.summary.total_tokens }} tokens</template
        >
        · 覆盖 {{ state.summary.utterance_count }} 条发言（近 {{ state.summary.recent_days }} 天
        {{ state.summary.recent_utterance_count }} 条）
      </p>
    </div>
    <el-empty
      v-else-if="!state.loading && !state.generating"
      description="暂无观点摘要；点击右上角生成（汇总关注作者的雪球发言，调用 LLM）"
      :image-size="56"
    />

    <!-- 与摘要无关的原始发言流：没生成摘要时也可看（v-if 链之外的独立兄弟） -->
    <el-collapse v-model="feed.openPanels.value" class="feed-collapse" @change="feed.onToggle">
      <el-collapse-item name="feed" data-testid="opinion-related-feed">
        <template #title>相关作者动态（近 {{ OPINION_FEED_DAYS }} 天原始发言）</template>
        <OpinionAuthorFeed
          :authors="feed.authors.value"
          :loading="feed.loading.value"
          :empty-text="`近 ${OPINION_FEED_DAYS} 天关注作者未提及该标的`"
        />
      </el-collapse-item>
    </el-collapse>

    <!-- 雪球公告 / 讨论：采集器每日一轮按标的落库，只读展示（不接 LLM） -->
    <el-collapse v-model="xueqiu.openPanels.value" class="feed-collapse" @change="xueqiu.onToggle">
      <el-collapse-item name="xueqiu" data-testid="xueqiu-symbol-feed">
        <template #title>雪球公告 / 讨论（每日采集，各最新 {{ XUEQIU_FEED_LIMIT }} 条）</template>
        <div v-loading="xueqiu.loading.value">
          <el-alert
            v-if="xueqiu.failed.value"
            type="warning"
            :closable="false"
            title="雪球公告/讨论加载失败，收起后再展开可重试"
            class="section-alert"
          />
          <p v-if="xueqiuHint" class="sd-meta-time">{{ xueqiuHint }}</p>
          <p v-else-if="xueqiu.lastCycleFinishedAt.value" class="sd-meta-time">
            采集于 {{ formatDateTime(xueqiu.lastCycleFinishedAt.value) }}
          </p>
          <el-tabs v-model="xueqiuTab">
            <el-tab-pane
              :label="`公告（${xueqiu.announcements.value.length}）`"
              name="announcement"
            >
              <XueqiuPostList
                :posts="xueqiu.announcements.value"
                empty-text="暂无雪球公告（采集器尚未采到该标的，或该市场无公告流）"
                testid="xueqiu-announcements"
              />
            </el-tab-pane>
            <el-tab-pane :label="`讨论（${xueqiu.discussions.value.length}）`" name="discussion">
              <XueqiuPostList
                :posts="xueqiu.discussions.value"
                empty-text="暂无雪球讨论"
                testid="xueqiu-discussions"
              />
            </el-tab-pane>
          </el-tabs>
        </div>
      </el-collapse-item>
    </el-collapse>
  </section>
</template>

<style scoped>
/* 近期变化类标签：加粗 + 实线描边，主题把标签统一做浅色时仍能一眼区分 */
.opinion-change {
  font-weight: 600;
  border-color: currentColor;
}
.section-alert {
  margin: 8px 0;
}
.stance-table {
  margin-bottom: 12px;
}
.feed-collapse {
  margin-top: 12px;
}
</style>
