<script setup lang="ts">
/**
 * 观点页「今日热帖」小卡：采集器每日一轮抓的雪球市场热帖（statuses/hots, scope=day）
 * 最新一次快照。只读展示；默认露出前几条，折叠看全部。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import api from '@/api'
import type { XueqiuHotPost } from '@/types'
import { useAliveGuard } from '@/composables/useAliveGuard'
import { formatDateTime } from '@/utils/helpers'
import XueqiuPostList from './XueqiuPostList.vue'

const PREVIEW_COUNT = 3
const LIMIT = 20

const { isUnmounted } = useAliveGuard()
const state = reactive({
  loading: false,
  failed: false,
  snapshotAt: null as string | null,
  items: [] as XueqiuHotPost[]
})
const expanded = ref(false)

const visible = computed(() => (expanded.value ? state.items : state.items.slice(0, PREVIEW_COUNT)))

async function load() {
  state.loading = true
  state.failed = false
  try {
    const response = await api.getXueqiuHots({ scope: 'day', limit: LIMIT })
    if (isUnmounted()) return
    state.snapshotAt = response.data.snapshot_at ?? null
    state.items = response.data.items ?? []
  } catch {
    if (!isUnmounted()) state.failed = true
  } finally {
    if (!isUnmounted()) state.loading = false
  }
}

onMounted(load)
</script>

<template>
  <el-card shadow="never" class="hots-card" data-testid="xueqiu-hots-card">
    <div class="hots-header">
      <span class="title-text">今日热帖</span>
      <span class="muted">
        <template v-if="state.snapshotAt">快照于 {{ formatDateTime(state.snapshotAt) }}</template>
        <template v-else-if="!state.loading">采集器每日一轮抓取，尚无快照</template>
      </span>
      <el-button
        v-if="state.items.length > PREVIEW_COUNT"
        link
        size="small"
        type="primary"
        @click="expanded = !expanded"
      >
        {{ expanded ? '收起' : `展开全部 ${state.items.length} 条` }}
      </el-button>
    </div>
    <div v-loading="state.loading">
      <p v-if="state.failed" class="muted">热帖加载失败</p>
      <XueqiuPostList
        v-else-if="state.items.length"
        :posts="visible"
        show-rank
        testid="xueqiu-hots-list"
      />
    </div>
  </el-card>
</template>

<style scoped>
.hots-card {
  margin-bottom: 12px;
}
.hots-header {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.title-text {
  font-weight: 600;
}
.muted {
  font-size: 12px;
  color: var(--app-text-muted);
}
</style>
