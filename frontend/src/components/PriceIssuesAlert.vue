<script setup lang="ts">
/**
 * 陈价/缺价提示（#286：仪表盘与统计页共用，同一严重度）：一行摘要，展开看名称清单，附刷新入口。
 */
import { computed, ref } from 'vue'
import {
  collectPriceIssues,
  describeIssueItem,
  missingSummary,
  staleSummary,
  type PriceFreshnessEntry
} from '@/utils/priceIssues'

const props = defineProps<{
  freshness: Record<string, PriceFreshnessEntry> | null | undefined
  refreshing?: boolean
}>()
defineEmits<{ refresh: [] }>()

const issues = computed(() => collectPriceIssues(props.freshness))
const expanded = ref<'stale' | 'missing' | null>(null)
const rows = computed(() => [
  { kind: 'stale' as const, title: staleSummary(issues.value), items: issues.value.stale },
  { kind: 'missing' as const, title: missingSummary(issues.value), items: issues.value.missing }
])

function toggle(kind: 'stale' | 'missing') {
  expanded.value = expanded.value === kind ? null : kind
}
</script>

<template>
  <template v-for="row in rows" :key="row.kind">
    <el-alert
      v-if="row.title"
      type="warning"
      show-icon
      :closable="false"
      class="price-issues-alert"
      :data-testid="`price-issues-${row.kind}`"
    >
      <template #title>
        <span>{{ row.title }}</span>
        <el-button
          link
          type="primary"
          size="small"
          class="price-issues-action"
          @click="toggle(row.kind)"
        >
          {{ expanded === row.kind ? '收起' : '查看明细' }}
        </el-button>
        <el-button
          link
          type="primary"
          size="small"
          class="price-issues-action"
          :loading="refreshing"
          @click="$emit('refresh')"
        >
          刷新价格
        </el-button>
      </template>
      <ul v-if="expanded === row.kind" class="price-issues-list">
        <li v-for="item in row.items" :key="item.key">{{ describeIssueItem(item) }}</li>
      </ul>
    </el-alert>
  </template>
</template>

<style scoped>
.price-issues-alert {
  margin-bottom: 12px;
}

.price-issues-action {
  margin-left: 8px;
}

.price-issues-list {
  margin: 6px 0 0;
  padding-left: 18px;
  max-height: 220px;
  overflow-y: auto;
  font-size: 12px;
  line-height: 1.7;
}
</style>
