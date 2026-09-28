<script setup lang="ts">
/**
 * 雪球发言采集器卡片（观点页数据源状态区）：启用/心跳/上一轮/Cookie/WAF 状态，
 * 关注作者名单与最近 10 次运行；每日按标的采集的状态与组合跟踪名单。
 * 增删改、「立即运行」与「更新 Cookie」仅管理员可见。
 */
import { computed, onMounted, ref } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { formatDateTime } from '@/utils/helpers'
import CookieUpdateDialog from './CookieUpdateDialog.vue'
import { useCollector } from './useCollector'
import {
  cookieLabel,
  cookieTagType,
  runStatusLabel,
  runStatusType,
  symbolsCycleSummary,
  xueqiuCubeUrl,
  xueqiuProfileUrl
} from './collectorStatus'

const auth = useAuthStore()
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
const expanded = ref<string[]>([])
const cookieDialog = ref<InstanceType<typeof CookieUpdateDialog>>()

const enabledAuthors = computed(
  () => (state.status?.authors ?? []).filter((author) => author.enabled).length
)

onMounted(load)
</script>

<template>
  <el-card shadow="never" class="collector-card" data-testid="xueqiu-collector-card">
    <div class="collector-header">
      <div class="collector-title">
        <span class="title-text">采集器</span>
        <el-tag :type="health.type" size="small" data-testid="collector-health">
          {{ health.label }}
        </el-tag>
        <span v-if="state.status" class="summary">
          上一轮 {{ formatDateTime(state.status.last_cycle_finished_at) }} · 关注
          {{ enabledAuthors }}/{{ state.status.authors.length }} 位作者 · Cookie
          <el-tag :type="cookieTagType(state.status.cookie.level)" size="small">
            {{ cookieLabel(state.status.cookie) }}
          </el-tag>
        </span>
      </div>
      <div class="collector-actions">
        <el-button size="small" :loading="state.loading" @click="load">刷新</el-button>
        <el-button
          v-if="isAdmin"
          size="small"
          data-testid="collector-update-cookie"
          @click="cookieDialog?.open()"
        >
          更新 Cookie
        </el-button>
        <el-button
          v-if="isAdmin"
          size="small"
          type="primary"
          data-testid="collector-run-now"
          :disabled="!state.status?.enabled || state.status?.run_pending"
          :loading="state.requesting"
          @click="runNow('authors')"
        >
          立即运行
        </el-button>
        <el-button
          v-if="isAdmin"
          size="small"
          data-testid="collector-run-symbols"
          :disabled="
            !state.status?.enabled ||
            !state.status?.symbols.enabled ||
            state.status?.symbols.run_pending
          "
          :loading="state.requesting"
          @click="runNow('symbols')"
        >
          立即跑按标的
        </el-button>
      </div>
    </div>
    <p v-if="health.hint" class="hint">{{ health.hint }}</p>
    <p v-if="symbolsSummary" class="hint" data-testid="collector-symbols-summary">
      {{ symbolsSummary }}
      <template v-if="state.status?.symbols.last_finished_at">
        （{{ formatDateTime(state.status.symbols.last_finished_at) }}）
      </template>
      <template v-if="state.status?.symbols.run_pending">· 已请求立即运行</template>
    </p>
    <el-alert
      v-if="state.loadError"
      type="error"
      :closable="false"
      :title="state.loadError"
      class="hint"
    />

    <el-collapse v-if="state.status" v-model="expanded" class="collector-detail">
      <el-collapse-item name="detail" title="运行详情、关注作者、跟踪组合与最近运行">
        <el-descriptions :column="2" size="small" border>
          <el-descriptions-item label="启用">
            {{ state.status.enabled ? '是' : '否（XUEQIU_COLLECTOR_ENABLED）' }}
          </el-descriptions-item>
          <el-descriptions-item label="进程心跳">
            {{ formatDateTime(state.status.heartbeat_at) }}
            <el-tag v-if="!state.status.alive" type="danger" size="small">超时</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="上一轮">
            {{ formatDateTime(state.status.last_cycle_started_at) }} →
            {{ formatDateTime(state.status.last_cycle_finished_at) }}
            <el-tag :type="runStatusType(state.status.last_cycle_status)" size="small">
              {{ runStatusLabel(state.status.last_cycle_status) }}
            </el-tag>
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
          <el-descriptions-item v-if="state.status.last_cycle_message" label="上一轮详情" :span="2">
            {{ state.status.last_cycle_message }}
          </el-descriptions-item>
          <el-descriptions-item label="按标的采集" :span="2">
            每天 {{ state.status.symbols.run_after }} 后一轮（持仓∪自选的公告/讨论、组合调仓） ·
            上一轮 {{ formatDateTime(state.status.symbols.last_started_at) }} →
            {{ formatDateTime(state.status.symbols.last_finished_at) }}
            <el-tag
              v-if="state.status.symbols.last_status"
              :type="runStatusType(state.status.symbols.last_status)"
              size="small"
            >
              {{ runStatusLabel(state.status.symbols.last_status) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item
            v-if="state.status.symbols.last_message"
            label="按标的详情"
            :span="2"
          >
            {{ state.status.symbols.last_message }}
          </el-descriptions-item>
        </el-descriptions>

        <h4 class="section-title">关注作者</h4>
        <el-table
          :data="state.status.authors"
          size="small"
          data-testid="collector-authors-table"
          empty-text="关注名单为空"
        >
          <el-table-column label="作者" min-width="160">
            <template #default="{ row }">
              <el-link :href="xueqiuProfileUrl(row.xueqiu_user_id)" target="_blank" type="primary">
                {{ row.display_name || row.xueqiu_user_id }}
              </el-link>
              <span v-if="row.display_name" class="muted"> {{ row.xueqiu_user_id }}</span>
            </template>
          </el-table-column>
          <el-table-column label="启用" width="80">
            <template #default="{ row }">
              <el-switch
                :model-value="row.enabled"
                :disabled="!isAdmin || state.saving"
                size="small"
                @change="(value: string | number | boolean) => toggleAuthor(row, Boolean(value))"
              />
            </template>
          </el-table-column>
          <el-table-column label="上次采集" min-width="150">
            <template #default="{ row }">{{ formatDateTime(row.last_run_at) }}</template>
          </el-table-column>
          <el-table-column label="结果" min-width="200">
            <template #default="{ row }">
              <el-tag v-if="row.last_status" :type="runStatusType(row.last_status)" size="small">
                {{ runStatusLabel(row.last_status) }}
              </el-tag>
              <span class="muted"> {{ row.last_message }}</span>
            </template>
          </el-table-column>
          <el-table-column v-if="isAdmin" label="" width="70">
            <template #default="{ row }">
              <el-button link type="danger" size="small" @click="removeAuthor(row)">
                移出
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="isAdmin" class="add-form" data-testid="collector-add-author">
          <el-input
            v-model="state.form.userId"
            size="small"
            placeholder="雪球用户 ID（数字）"
            class="id-input"
          />
          <el-input
            v-model="state.form.displayName"
            size="small"
            placeholder="展示名（可选）"
            class="name-input"
          />
          <el-button
            size="small"
            type="primary"
            :disabled="!formValid"
            :loading="state.saving"
            @click="addAuthor"
          >
            加入关注
          </el-button>
        </div>

        <h4 class="section-title">跟踪组合（调仓记录，随按标的采集每日一轮）</h4>
        <el-table
          :data="state.status.cubes"
          size="small"
          data-testid="collector-cubes-table"
          empty-text="没有跟踪的组合"
        >
          <el-table-column label="组合" min-width="160">
            <template #default="{ row }">
              <el-link :href="xueqiuCubeUrl(row.cube_id)" target="_blank" type="primary">
                {{ row.display_name || row.cube_id }}
              </el-link>
              <span v-if="row.display_name" class="muted"> {{ row.cube_id }}</span>
            </template>
          </el-table-column>
          <el-table-column label="启用" width="80">
            <template #default="{ row }">
              <el-switch
                :model-value="row.enabled"
                :disabled="!isAdmin || state.saving"
                size="small"
                @change="(value: string | number | boolean) => toggleCube(row, Boolean(value))"
              />
            </template>
          </el-table-column>
          <el-table-column label="上次采集" min-width="150">
            <template #default="{ row }">{{ formatDateTime(row.last_run_at) }}</template>
          </el-table-column>
          <el-table-column label="结果" min-width="200">
            <template #default="{ row }">
              <el-tag v-if="row.last_status" :type="runStatusType(row.last_status)" size="small">
                {{ runStatusLabel(row.last_status) }}
              </el-tag>
              <span class="muted"> {{ row.last_message }}</span>
            </template>
          </el-table-column>
          <el-table-column v-if="isAdmin" label="" width="70">
            <template #default="{ row }">
              <el-button link type="danger" size="small" @click="removeCube(row)"> 移出 </el-button>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="isAdmin" class="add-form" data-testid="collector-add-cube">
          <el-input
            v-model="state.cubeForm.cubeId"
            size="small"
            placeholder="组合代号（如 ZH000001）"
            class="id-input"
          />
          <el-input
            v-model="state.cubeForm.displayName"
            size="small"
            placeholder="展示名（可选）"
            class="name-input"
          />
          <el-button
            size="small"
            type="primary"
            :disabled="!cubeFormValid"
            :loading="state.saving"
            @click="addCube"
          >
            跟踪组合
          </el-button>
        </div>

        <h4 class="section-title">最近运行（作者采集）</h4>
        <el-table :data="state.status.recent_runs" size="small" empty-text="采集器还没有运行记录">
          <el-table-column label="开始" min-width="140">
            <template #default="{ row }">{{ formatDateTime(row.started_at) }}</template>
          </el-table-column>
          <el-table-column prop="author_user_id" label="作者" min-width="110" />
          <el-table-column label="结果" width="100">
            <template #default="{ row }">
              <el-tag :type="runStatusType(row.status)" size="small">
                {{ runStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="候选/回复/发言" min-width="120">
            <template #default="{ row }">
              {{ row.candidate_count }} / {{ row.reply_count }} / {{ row.utterance_count }}
            </template>
          </el-table-column>
          <el-table-column
            prop="error_message"
            label="错误"
            min-width="180"
            show-overflow-tooltip
          />
        </el-table>
      </el-collapse-item>
    </el-collapse>
    <CookieUpdateDialog v-if="isAdmin" ref="cookieDialog" @updated="load" />
  </el-card>
</template>

<style scoped>
.collector-card {
  margin-bottom: 12px;
}
.collector-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.collector-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.title-text {
  font-weight: 600;
}
.summary {
  font-size: 12px;
  color: var(--app-text-muted);
}
.hint {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--app-text-muted);
}
.collector-detail {
  margin-top: 8px;
}
.section-title {
  margin: 12px 0 6px;
  font-size: 13px;
}
.muted {
  font-size: 12px;
  color: var(--app-text-muted);
}
.add-form {
  display: flex;
  gap: 8px;
  margin-top: 8px;
  flex-wrap: wrap;
}
.id-input {
  width: 200px;
}
.name-input {
  width: 160px;
}
</style>
