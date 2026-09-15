<script setup lang="ts">
import { useRouter } from 'vue-router'
import type { OpinionSummaryRow } from '@/types'
import { opinionTagType } from './useOpinions'

defineProps<{
  items: OpinionSummaryRow[]
  loading: boolean
  sourceAvailable: boolean
}>()

const ORIGIN_LABELS: Record<string, string> = {
  holding: '持仓',
  watchlist: '自选',
  both: '持仓+自选'
}

const router = useRouter()

function openDetail(row: OpinionSummaryRow) {
  router.push(`/securities/${encodeURIComponent(row.market)}/${encodeURIComponent(row.symbol)}`)
}
</script>

<template>
  <el-table
    :data="items"
    v-loading="loading"
    size="small"
    data-testid="opinion-symbols-table"
    :default-sort="{ prop: 'new_utterance_count', order: 'descending' }"
  >
    <el-table-column label="标的" min-width="150">
      <template #default="{ row }">
        <el-link type="primary" @click="openDetail(row)">
          {{ row.name || row.symbol }}
        </el-link>
        <span class="symbol-sub">{{ row.symbol }} · {{ row.market }}</span>
      </template>
    </el-table-column>
    <el-table-column label="来源" width="90">
      <template #default="{ row }">
        <el-tag size="small" type="info" effect="plain">
          {{ ORIGIN_LABELS[row.origin] || row.origin }}
        </el-tag>
      </template>
    </el-table-column>
    <el-table-column label="观点标签" min-width="180">
      <template #default="{ row }">
        <template v-if="row.tags.length">
          <el-tag
            v-for="tag in row.tags"
            :key="tag"
            size="small"
            :type="opinionTagType(tag)"
            class="opinion-tag"
          >
            {{ tag }}
          </el-tag>
        </template>
        <span v-else class="muted">未生成</span>
      </template>
    </el-table-column>
    <el-table-column label="摘要" min-width="220" show-overflow-tooltip>
      <template #default="{ row }">
        <span v-if="row.summary">{{ row.summary }}</span>
        <span v-else class="muted">—</span>
      </template>
    </el-table-column>
    <el-table-column label="发言" width="90" align="right">
      <template #default="{ row }">
        <span v-if="row.matched_count !== null">{{ row.matched_count }}</span>
        <span v-else class="muted">—</span>
      </template>
    </el-table-column>
    <el-table-column label="新发言" width="90" align="right" prop="new_utterance_count">
      <template #default="{ row }">
        <el-badge
          v-if="row.new_utterance_count"
          :value="row.new_utterance_count"
          type="warning"
          :max="99"
        />
        <span v-else class="muted">0</span>
      </template>
    </el-table-column>
    <el-table-column label="生成时间" width="120">
      <template #default="{ row }">
        <span v-if="row.created_at">{{ row.created_at.slice(0, 10) }}</span>
        <span v-else class="muted">—</span>
      </template>
    </el-table-column>
    <template #empty>
      <el-empty
        :description="
          sourceAvailable ? '近期关注作者未提及任何持仓/自选标的' : '雪球观点数据源未接入'
        "
        :image-size="60"
      />
    </template>
  </el-table>
</template>

<style scoped>
.symbol-sub {
  margin-left: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.opinion-tag {
  margin-right: 4px;
}
.muted {
  color: var(--el-text-color-secondary);
}
</style>
