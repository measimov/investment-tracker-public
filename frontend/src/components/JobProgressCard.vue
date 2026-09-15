<script setup lang="ts">
import type { BatchJobBase } from '../composables/useBatchJobProgress'

/**
 * 批量 job 进度卡（issue #139）：状态行 + 进度条 + 明细行 + 终止/停止查看。
 *
 * 骨架共享，job 特有内容走插槽：`detail`（计数明细行）、`extra`（如批量分析
 * 的最近结果条）、`header-suffix`（如当前阶段）。testid 由调用方传入——E2E
 * 按 testid 定位两张卡，不能因组件化改名。
 */
defineProps<{
  job: BatchJobBase
  statusText: string
  percent: number
  progressStatus?: 'success' | 'exception' | 'warning'
  etaText: string
  active: boolean
  hint: string
  testid: string
  cancelTestid: string
  stopTestid?: string
}>()

defineEmits<{ cancel: []; stop: [] }>()
</script>

<template>
  <div class="job-progress" :data-testid="testid">
    <div class="job-progress-header">
      <span>{{ statusText }}</span>
      <span>
        {{ job.completed || 0 }}/{{ job.total || 0 }}
        <template v-if="job.current_symbol">
          · {{ job.current_symbol }} {{ job.current_market }}
          <slot name="header-suffix" />
        </template>
      </span>
    </div>
    <el-progress :percentage="percent" :status="progressStatus" :stroke-width="10" />
    <div class="job-progress-detail">
      <slot name="detail" />
      <template v-if="etaText">· 预计剩余 {{ etaText }}</template>
    </div>
    <slot name="extra" />
    <div v-if="job.abort_reason" class="job-progress-error">
      {{ job.abort_reason }}
    </div>
    <div v-if="active" class="job-progress-actions">
      <el-button
        size="small"
        type="danger"
        text
        :data-testid="cancelTestid"
        @click="$emit('cancel')"
      >
        终止任务
      </el-button>
      <el-button size="small" text :data-testid="stopTestid" @click="$emit('stop')">
        停止查看进度
      </el-button>
      <span class="batch-hint">{{ hint }}</span>
    </div>
  </div>
</template>

<style scoped>
/* 进度块通用外观走全局 styles.css 的 .job-progress 套件；这里只有动作区 */
.job-progress-actions {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.batch-hint {
  font-size: 12px;
  color: var(--app-text-muted);
}
</style>
