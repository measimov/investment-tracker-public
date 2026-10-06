<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  NAlert,
  NButton,
  NDataTable,
  NEmpty,
  NProgress,
  NSkeleton,
  NSpin,
  NTag,
  type DataTableColumns
} from 'naive-ui'
import type { OpinionAuthorStance } from '@/types'
import OpinionAuthorFeed from '@/components/OpinionAuthorFeed.vue'
import { useAliveGuard } from '@/composables/useAliveGuard'
import { useXueqiuCapabilitiesStore } from '@/stores/xueqiuCapabilities'
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
const capabilities = useXueqiuCapabilitiesStore()

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

// 雪球公告流 / 讨论（采集器每日按标的落库）：同样首次展开才拉取
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
  if (capabilities.showOpinions) load()
}

onMounted(() => {
  if (capabilities.showOpinions) load()
})
watch(
  () => capabilities.showOpinions,
  (visible) => {
    if (visible) load()
  }
)
// 同业跳转复用组件实例：路由参数变了必须重拉（世代守卫在 composable 内），
// 作者动态一并重置为未加载
watch(() => [props.symbol, props.market], resetAndLoad)

const stanceColumns: DataTableColumns<OpinionAuthorStance> = [
  { title: '作者', key: 'author', width: 140 },
  { title: '立场', key: 'stance', width: 80 },
  {
    title: '近期变化',
    key: 'recent_change',
    width: 100,
    render: (row) => CHANGE_LABELS[row.recent_change] || row.recent_change
  },
  { title: '原文引述', key: 'evidence', minWidth: 220 }
]
function tagType(tag: string) {
  const type = opinionTagStyle(tag).type
  return type === 'info' ? 'default' : type
}
function tagColor(tag: string) {
  const style = opinionTagStyle(tag)
  if (style.effect !== 'dark') return undefined
  return {
    color:
      style.type === 'primary'
        ? 'var(--app-primary-strong)'
        : style.type === 'warning'
          ? 'var(--app-warning-text)'
          : 'var(--app-text-muted)',
    textColor: 'var(--app-on-primary)'
  }
}
</script>

<template>
  <section class="sd-block" data-testid="opinion-section">
    <template v-if="capabilities.showOpinions">
      <div class="sd-block-header">
        <h3 class="sd-block-title">雪球观点</h3>
        <NButton
          size="small"
          secondary
          data-testid="generate-opinion-button"
          :loading="state.generating"
          @click="generate"
        >
          {{ state.summary ? '重新生成观点摘要' : '生成观点摘要' }}
        </NButton>
      </div>

      <NAlert v-if="state.notice" type="default" :closable="false" class="section-alert">{{
        state.notice
      }}</NAlert>
      <NAlert v-if="state.error" type="error" :closable="false" class="section-alert"
        >{{ state.error }}
        <div>
          <NButton :disabled="state.loading || state.generating" @click="load"
            >重新加载观点</NButton
          >
        </div></NAlert
      >
      <NProgress
        v-if="state.generating && state.job"
        :percentage="
          Math.round(((Number(state.job.completed) || 0) / (Number(state.job.total) || 1)) * 100)
        "
        type="line"
        :height="6"
      >
        <span class="sd-meta-time">{{ state.job.stage_label || '进行中' }}</span>
      </NProgress>

      <div v-if="state.loading && !state.summary" role="status" aria-label="正在加载观点摘要">
        <NSkeleton text :repeat="4" />
      </div>
      <NSpin v-if="state.summary" :show="state.loading"
        ><div>
          <!-- 与 AI 分析同一元信息条：标签 + 窗口/最新发言 + 生成时间 -->
          <div class="sd-meta-row">
            <NTag
              v-for="tag in state.summary.tags"
              :key="tag"
              size="small"
              :type="tagType(tag)"
              :color="tagColor(tag)"
              :bordered="false"
              :class="{ 'opinion-change': opinionTagStyle(tag).change }"
            >
              {{ opinionTagStyle(tag).label }}
            </NTag>
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

          <div
            v-if="state.summary.author_stances.length"
            class="sd-table-scroll stance-table"
            tabindex="0"
            role="region"
            aria-label="作者立场与原文引述表，可横向滚动"
          >
            <NDataTable
              class="sd-data-table"
              style="min-width: 540px"
              :columns="stanceColumns"
              :data="state.summary.author_stances"
              :bordered="false"
            />
          </div>

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
        </div></NSpin
      >
      <NEmpty
        v-else-if="!state.summary && !state.loading && !state.generating && !state.error"
        description="暂无观点摘要；点击右上角生成（汇总关注作者的雪球发言，调用 LLM）"
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
    </template>

    <!-- 雪球公告流 / 讨论：采集器每日一轮按标的落库，只读展示（不接 LLM） -->
    <el-collapse
      v-if="capabilities.showSymbolFeed"
      v-model="xueqiu.openPanels.value"
      class="feed-collapse"
      @change="xueqiu.onToggle"
    >
      <el-collapse-item name="xueqiu" data-testid="xueqiu-symbol-feed">
        <template #title>雪球公告流 / 讨论（每日采集，各最新 {{ XUEQIU_FEED_LIMIT }} 条）</template>
        <NSpin :show="xueqiu.loading.value"
          ><div>
            <p class="sd-footnote">
              以下为雪球平台的公告流与讨论，属于第三方来源；官方披露请查看「公告」标签。
            </p>
            <NAlert
              v-if="xueqiu.failed.value"
              type="warning"
              :closable="false"
              class="section-alert"
              >雪球公告流/讨论加载失败，收起后再展开可重试</NAlert
            >
            <p v-if="xueqiuHint" class="sd-meta-time">{{ xueqiuHint }}</p>
            <p v-else-if="xueqiu.lastCycleFinishedAt.value" class="sd-meta-time">
              采集于 {{ formatDateTime(xueqiu.lastCycleFinishedAt.value) }}
            </p>
            <el-tabs v-model="xueqiuTab">
              <el-tab-pane
                :label="`雪球公告流（${xueqiu.announcements.value.length}）`"
                name="announcement"
              >
                <XueqiuPostList
                  :posts="xueqiu.announcements.value"
                  empty-text="暂无雪球公告流（采集器尚未采到该标的，或该市场无雪球公告流）"
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
            </el-tabs></div
        ></NSpin>
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
