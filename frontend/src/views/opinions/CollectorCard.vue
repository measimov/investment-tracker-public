<script setup lang="ts">
/**
 * 雪球发言采集器卡片（观点页数据源状态区）：启用/心跳/上一轮/Cookie/WAF 状态，
 * 关注作者名单与最近 10 次运行；每日按标的采集的状态与组合跟踪名单。
 * 增删改、「立即运行」与「更新 Cookie」仅管理员可见。
 */
import { computed, onMounted, ref } from 'vue'
import { NAlert, NButton, NInput, NTag } from 'naive-ui'
import { useMediaQuery } from '@/composables/useMediaQuery'
import CollectorRecords from './CollectorRecords.vue'
import { useAuthStore } from '@/stores/auth'
import { useXueqiuCapabilitiesStore } from '@/stores/xueqiuCapabilities'
import { formatDateTime } from '@/utils/helpers'
import CookieUpdateDialog from './CookieUpdateDialog.vue'
import { useCollector } from './useCollector'
import {
  cookieLabel,
  cookieTagType,
  runStatusLabel,
  runStatusType,
  symbolsCycleSummary
} from './collectorStatus'

const isMobile = useMediaQuery('(max-width:640px)')
const auth = useAuthStore()
const capabilities = useXueqiuCapabilitiesStore()
function cookieUpdated() {
  void load()
  void capabilities.load(true)
}
const isAdmin = computed(() => auth.isAdmin)
const {
  state,
  health,
  formValid,
  cubeFormValid,
  load,
  addAuthor,
  toggleAuthor,
  removeAuthor,
  runNow,
  addCube,
  toggleCube,
  removeCube
} = useCollector()
const symbolsSummary = computed(() => symbolsCycleSummary(state.status?.symbols))
// 只决定说明的展示层级；健康与运行状态仍复用既有判定。
const compact = computed(
  () =>
    !!state.status &&
    !state.loading &&
    !state.loadError &&
    !state.requesting &&
    health.value.type === 'success' &&
    cookieTagType(state.status.cookie.level) === 'success' &&
    !state.status.symbols.run_pending &&
    !state.status.symbols.retry_pending &&
    (!state.status.symbols.enabled || runStatusType(state.status.symbols.last_status) === 'success')
)
const expanded = ref<string[]>([])
const cookieDialog = ref<InstanceType<typeof CookieUpdateDialog>>()

const enabledAuthors = computed(
  () => (state.status?.authors ?? []).filter((author) => author.enabled).length
)

function tagType(type: string) {
  return type === 'danger'
    ? 'error'
    : type === 'info'
      ? 'default'
      : (type as 'success' | 'warning' | 'primary')
}
onMounted(load)
</script>

<template>
  <section
    aria-label="采集器状态"
    class="collector-card"
    :class="{ 'collector-compact': compact }"
    data-testid="xueqiu-collector-card"
  >
    <div v-if="!compact" class="collector-header">
      <div class="collector-title">
        <h2 class="title-text">采集器</h2>
        <NTag :type="tagType(health.type)" size="small" data-testid="collector-health">
          {{ health.label }}
        </NTag>
        <NTag
          v-if="
            state.status &&
            runStatusType(state.status.last_cycle_status) === 'primary' &&
            health.type !== 'primary'
          "
          type="primary"
          size="small"
          >{{ runStatusLabel(state.status.last_cycle_status) }}</NTag
        >
        <span v-if="state.status" class="summary">
          上一轮 {{ formatDateTime(state.status.last_cycle_finished_at) }} · 关注
          {{ enabledAuthors }}/{{ state.status.authors.length }} 位作者 · Cookie
          <NTag :type="tagType(cookieTagType(state.status.cookie.level))" size="small">
            {{ cookieLabel(state.status.cookie) }}
          </NTag>
        </span>
      </div>
      <div class="collector-actions">
        <NButton aria-label="重新加载采集器状态" :loading="state.loading" @click="load"
          >重新加载</NButton
        >
      </div>
    </div>
    <p v-if="!compact && health.hint" class="hint">{{ health.hint }}</p>
    <p
      v-if="
        state.status?.cookie.message &&
        cookieTagType(state.status.cookie.level) !== 'success' &&
        health.hint !== state.status.cookie.message
      "
      class="hint"
    >
      {{ state.status.cookie.message }}
    </p>
    <p v-if="state.status?.run_pending && health.type !== 'primary'" class="hint">
      已请求立即运行，等待采集器开始。
    </p>
    <p
      v-if="
        state.status?.last_cycle_message &&
        runStatusType(state.status.last_cycle_status) === 'primary' &&
        health.hint !== state.status.last_cycle_message
      "
      class="hint"
    >
      {{ state.status.last_cycle_message }}
    </p>
    <p v-if="!compact && symbolsSummary" class="hint" data-testid="collector-symbols-summary">
      {{ symbolsSummary }}
      <template v-if="state.status?.symbols.last_finished_at">
        （{{ formatDateTime(state.status.symbols.last_finished_at) }}）
      </template>
      <template v-if="state.status?.symbols.run_pending">· 已请求立即运行</template>
    </p>
    <p
      v-if="
        !compact &&
        state.status?.symbols.last_message &&
        runStatusType(state.status.symbols.last_status) !== 'success'
      "
      class="hint"
    >
      {{ state.status.symbols.last_message }}
    </p>
    <NAlert v-if="state.loadError" type="error" :title="state.loadError" class="hint">{{
      state.status
        ? '保留上次成功的采集器状态，未确认最新结果。'
        : '尚未确认采集器状态，不能据此判断运行正常或来源可用。'
    }}</NAlert>
    <p v-else-if="state.loading && state.status" class="hint">
      正在重新加载，当前保留上次成功的采集器状态。
    </p>

    <el-collapse v-model="expanded" class="collector-detail">
      <el-collapse-item name="detail" title="运行详情、关注作者、跟踪组合与最近运行">
        <template #title>
          <div v-if="compact && state.status" class="collector-title compact-title">
            <h2 class="title-text">采集器</h2>
            <NTag :type="tagType(health.type)" size="small" data-testid="collector-health">
              {{ health.label }}
            </NTag>
            <span class="summary"
              >上一轮 {{ formatDateTime(state.status.last_cycle_finished_at) }}</span
            >
            <span v-if="!state.status.symbols.enabled" class="summary">按标的采集未启用</span>
            <span class="maintenance-label">维护与运行详情</span>
          </div>
          <span v-else>运行详情、关注作者、跟踪组合与最近运行</span>
        </template>
        <div class="collector-actions maintenance-actions">
          <NButton
            v-if="compact"
            aria-label="重新加载采集器状态"
            :loading="state.loading"
            @click="load"
          >
            重新加载
          </NButton>
          <NButton
            v-if="isAdmin"
            size="medium"
            data-testid="collector-update-cookie"
            @click="cookieDialog?.open()"
          >
            更新 Cookie
          </NButton>
          <NButton
            v-if="isAdmin"
            size="medium"
            type="primary"
            data-testid="collector-run-now"
            :disabled="!state.status?.enabled || state.status?.run_pending"
            :loading="state.requesting"
            @click="runNow('authors')"
            >立即运行</NButton
          >
          <NButton
            v-if="isAdmin"
            size="medium"
            data-testid="collector-run-symbols"
            :disabled="
              !state.status?.enabled ||
              !state.status?.symbols.enabled ||
              state.status?.symbols.run_pending
            "
            :loading="state.requesting"
            @click="runNow('symbols')"
            >立即跑按标的</NButton
          >
        </div>
        <template v-if="compact && state.status">
          <p class="hint">
            关注 {{ enabledAuthors }}/{{ state.status.authors.length }} 位作者 · Cookie
            {{ cookieLabel(state.status.cookie) }}
          </p>
          <p v-if="symbolsSummary" class="hint" data-testid="collector-symbols-summary">
            {{ symbolsSummary }}
            <template v-if="state.status.symbols.last_finished_at">
              （{{ formatDateTime(state.status.symbols.last_finished_at) }}）
            </template>
          </p>
        </template>
        <el-descriptions
          v-if="state.status"
          :column="isMobile ? 1 : 2"
          :label-width="isMobile ? 80 : undefined"
          size="small"
          border
        >
          <el-descriptions-item label="启用">
            {{ state.status.enabled ? '是' : '否（需在部署配置中开启）' }}
          </el-descriptions-item>
          <el-descriptions-item label="进程心跳">
            {{ formatDateTime(state.status.heartbeat_at) }}
            <NTag v-if="!state.status.alive" type="error" size="small">超时</NTag>
          </el-descriptions-item>
          <el-descriptions-item label="上一轮">
            {{ formatDateTime(state.status.last_cycle_started_at) }} →
            {{ formatDateTime(state.status.last_cycle_finished_at) }}
            <NTag :type="tagType(runStatusType(state.status.last_cycle_status))" size="small">
              {{ runStatusLabel(state.status.last_cycle_status) }}
            </NTag>
          </el-descriptions-item>
          <el-descriptions-item label="节奏">
            每 {{ state.status.cycle_minutes }} 分钟一轮
          </el-descriptions-item>
          <el-descriptions-item label="WAF">
            <template v-if="state.status.waf_cooldown_until">
              冷却至 {{ formatDateTime(state.status.waf_cooldown_until) }}
            </template>
            <template v-else-if="state.status.last_waf_at">
              上次 {{ formatDateTime(state.status.last_waf_at) }}
            </template>
            <template v-else>未触发</template>
          </el-descriptions-item>
          <el-descriptions-item label="Cookie">
            {{ state.status.cookie.message }}
          </el-descriptions-item>
          <el-descriptions-item
            v-if="state.status.last_cycle_message"
            label="上一轮详情"
            :span="isMobile ? 1 : 2"
          >
            {{ state.status.last_cycle_message }}
          </el-descriptions-item>
          <el-descriptions-item label="按标的采集" :span="isMobile ? 1 : 2">
            每天 {{ state.status.symbols.run_after }} 后一轮（持仓∪自选的公告/讨论、组合调仓） ·
            上一轮 {{ formatDateTime(state.status.symbols.last_started_at) }} →
            {{ formatDateTime(state.status.symbols.last_finished_at) }}
            <NTag
              v-if="state.status.symbols.last_status"
              :type="tagType(runStatusType(state.status.symbols.last_status))"
              size="small"
            >
              {{ runStatusLabel(state.status.symbols.last_status) }}
            </NTag>
          </el-descriptions-item>
          <el-descriptions-item
            v-if="state.status.symbols.last_message"
            label="按标的详情"
            :span="isMobile ? 1 : 2"
          >
            {{ state.status.symbols.last_message }}
          </el-descriptions-item>
        </el-descriptions>

        <CollectorRecords
          v-if="state.status"
          :status="state.status"
          :is-admin="isAdmin"
          :saving="state.saving"
          @toggle-author="toggleAuthor"
          @remove-author="removeAuthor"
          @toggle-cube="toggleCube"
          @remove-cube="removeCube"
        >
          <template #add-author>
            <div v-if="isAdmin" class="add-form" data-testid="collector-add-author">
              <NInput
                v-model:value="state.form.userId"
                :input-props="{ 'aria-label': '关注作者雪球用户 ID' }"
                size="small"
                placeholder="雪球用户 ID（数字）"
                class="id-input"
              />
              <NInput
                v-model:value="state.form.displayName"
                :input-props="{ 'aria-label': '关注作者展示名' }"
                size="small"
                placeholder="展示名（可选）"
                class="name-input"
              />
              <NButton
                size="medium"
                type="primary"
                :disabled="!formValid"
                :loading="state.saving"
                @click="addAuthor"
              >
                加入关注
              </NButton>
            </div> </template
          ><template #add-cube>
            <div v-if="isAdmin" class="add-form" data-testid="collector-add-cube">
              <NInput
                v-model:value="state.cubeForm.cubeId"
                :input-props="{ 'aria-label': '跟踪组合代号' }"
                size="small"
                placeholder="组合代号（如 ZH000001）"
                class="id-input"
              />
              <NInput
                v-model:value="state.cubeForm.displayName"
                :input-props="{ 'aria-label': '跟踪组合展示名' }"
                size="small"
                placeholder="展示名（可选）"
                class="name-input"
              />
              <NButton
                size="medium"
                type="primary"
                :disabled="!cubeFormValid"
                :loading="state.saving"
                @click="addCube"
              >
                跟踪组合
              </NButton>
            </div>
          </template>
        </CollectorRecords>
      </el-collapse-item>
    </el-collapse>
    <CookieUpdateDialog v-if="isAdmin" ref="cookieDialog" @updated="cookieUpdated" />
  </section>
</template>

<style scoped>
.collector-card {
  padding: 20px;
  border: 1px solid var(--app-border);
  border-radius: 12px;
  background: var(--app-surface-muted);
  min-width: 0;
}
.collector-compact {
  padding-block: 8px;
}
.collector-compact .collector-detail {
  margin-top: 0;
}
.compact-title .summary {
  margin: 0;
}
.maintenance-label {
  color: var(--app-primary-strong);
  font-size: 13px;
}
.maintenance-actions {
  margin-bottom: 12px;
}
.collector-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.collector-title {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}
.title-text {
  font-size: 18px;
  font-weight: 600;
  margin: 0;
}
.collector-actions {
  align-items: center;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.collector-actions :deep(.n-button) {
  min-height: 36px;
}
.summary,
.hint {
  font-size: 13px;
  line-height: 1.75;
  color: var(--app-text-muted);
  margin: 12px 0 0;
  overflow-wrap: anywhere;
}
.collector-detail {
  margin-top: 12px;
  --el-collapse-header-bg-color: var(--app-surface-muted);
  --el-collapse-content-bg-color: var(--app-surface-muted);
  --el-collapse-border-color: var(--app-border);
  --el-collapse-header-text-color: var(--app-text);
  --el-collapse-content-text-color: var(--app-text);
}
.collector-detail :deep(.el-descriptions__body) {
  background: var(--app-surface-muted);
  color: var(--app-text);
}
.collector-detail :deep(.el-descriptions__label.is-bordered-label) {
  background: var(--app-surface-secondary);
  color: var(--app-text);
}
.collector-detail :deep(.el-descriptions__cell.is-bordered-content) {
  background: var(--app-surface-muted);
}
.collector-detail :deep(.el-collapse-item__content) {
  padding-bottom: 0;
}
.collector-detail :deep(.el-collapse-item__header) {
  height: auto;
  min-height: 44px;
  line-height: 1.7;
  padding: 12px 0;
}
.collector-detail :deep(.el-descriptions__cell) {
  overflow-wrap: anywhere;
}
.add-form {
  display: flex;
  gap: 8px;
  margin-top: 12px;
  flex-wrap: wrap;
}
.id-input {
  width: 220px;
  max-width: 100%;
}
.name-input {
  width: 180px;
  max-width: 100%;
}
@media (max-width: 640px) {
  .collector-card {
    padding: 16px;
  }
  .collector-compact {
    padding-block: 8px;
  }
  .collector-actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    width: 100%;
  }
  .collector-actions :deep(.n-button__content) {
    white-space: normal;
  }
  .collector-detail :deep(.el-descriptions__label.is-bordered-label) {
    min-width: 80px;
    white-space: nowrap;
  }
  .collector-actions :deep(.n-button),
  .add-form :deep(.n-button) {
    min-height: 44px;
  }
  .add-form :deep(.n-input-wrapper) {
    min-height: 44px;
    align-items: center;
  }
  .id-input,
  .name-input {
    width: 100%;
  }
}
</style>
