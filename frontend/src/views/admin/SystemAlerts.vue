<template>
  <div class="system-alerts-page">
    <el-card>
      <template #header>
        <div class="page-header">
          <span>系统告警</span>
          <div class="header-actions">
            <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
            <el-button :loading="checking" @click="runChecks">立即检查</el-button>
            <el-button type="primary" :icon="Bell" :loading="testing" @click="sendTest">
              发送测试通知
            </el-button>
          </div>
        </div>
      </template>

      <div v-if="data" class="status-block">
        <el-alert
          :title="channelLine.text"
          :type="alertType(channelLine.type)"
          :closable="false"
          show-icon
        />
        <el-alert
          v-if="!data.check_enabled"
          title="周期告警检查已关闭（ALERT_CHECK_ENABLED=false）：只有「立即检查」与外部信号会更新本页"
          type="warning"
          :closable="false"
          show-icon
        />
        <el-alert
          v-if="testLine"
          :title="testLine.text"
          :type="alertType(testLine.type)"
          closable
          show-icon
          @close="testLine = null"
        />
        <p class="hint">
          每 {{ data.check_interval_minutes }} 分钟检查一次（雪球采集器 / Cookie / WAF、汇率数据源、
          周期任务、后台任务，以及备份脚本等外部信号）；新告警与升级立即推送，未恢复的每
          {{ data.reminder_hours }} 小时提醒一次，恢复时推送「已恢复」。
        </p>
        <div class="counts">
          <el-tag type="danger" effect="plain">严重 {{ data.counts.critical }}</el-tag>
          <el-tag type="warning" effect="plain">警告 {{ data.counts.warning }}</el-tag>
          <el-tag type="info" effect="plain">提示 {{ data.counts.info }}</el-tag>
          <el-tag type="success" effect="plain">
            近 7 天已恢复 {{ data.counts.recent_resolved }}
          </el-tag>
        </div>
      </div>

      <h3 class="section-title">当前告警</h3>
      <div class="responsive-table">
        <el-table
          :data="data?.active ?? []"
          v-loading="loading"
          stripe
          empty-text="没有未恢复的告警"
        >
          <el-table-column label="级别" width="80">
            <template #default="{ row }">
              <el-tag :type="severityTagType(row.severity)" size="small">
                {{ severityLabel(row.severity) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="告警" min-width="260">
            <template #default="{ row }">
              <div class="alert-title">{{ row.title }}</div>
              <div v-if="row.message" class="alert-message">{{ row.message }}</div>
            </template>
          </el-table-column>
          <el-table-column label="来源" width="110">
            <template #default="{ row }">{{ sourceLabel(row.source) }}</template>
          </el-table-column>
          <el-table-column label="首次发现" min-width="150">
            <template #default="{ row }">
              <div>{{ formatDateTime(row.first_seen_at) }}</div>
              <div class="muted">已持续 {{ durationText(row.first_seen_at, nowIso) || EMPTY }}</div>
            </template>
          </el-table-column>
          <el-table-column label="推送" min-width="140">
            <template #default="{ row }">
              <div>{{ notifyStatusText(row, data?.channels.min_severity ?? 'warning') }}</div>
              <div v-if="row.last_notified_at" class="muted">
                最近 {{ formatDateTime(row.last_notified_at) }}
              </div>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <h3 class="section-title">近 7 天已恢复</h3>
      <div class="responsive-table">
        <el-table
          :data="data?.recent_resolved ?? []"
          v-loading="loading"
          stripe
          empty-text="近 7 天没有恢复的告警"
        >
          <el-table-column label="级别" width="80">
            <template #default="{ row }">
              <el-tag :type="severityTagType(row.severity)" size="small" effect="plain">
                {{ severityLabel(row.severity) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="告警" min-width="260">
            <template #default="{ row }">
              <div class="alert-title">{{ row.title }}</div>
              <div v-if="row.message" class="alert-message">{{ row.message }}</div>
            </template>
          </el-table-column>
          <el-table-column label="来源" width="110">
            <template #default="{ row }">{{ sourceLabel(row.source) }}</template>
          </el-table-column>
          <el-table-column label="恢复时间" min-width="150">
            <template #default="{ row }">
              <div>{{ formatDateTime(row.resolved_at) }}</div>
              <div class="muted">
                持续 {{ durationText(row.first_seen_at, row.resolved_at) || EMPTY }}
              </div>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Bell, Refresh } from '@element-plus/icons-vue'
import api from '../../api'
import type { AlertList } from '../../types'
import { EMPTY, formatDateTime } from '../../utils/helpers'
import { showApiError } from '../../utils/showApiError'
import { useAliveGuard } from '../../composables/useAliveGuard'
import {
  channelStatus,
  durationText,
  notifyStatusText,
  severityLabel,
  severityTagType,
  sourceLabel,
  testResultMessage,
  type StatusLine
} from './systemAlerts'

const data = ref<AlertList | null>(null)
const loading = ref(false)
const checking = ref(false)
const testing = ref(false)
const testLine = ref<StatusLine | null>(null)
const nowIso = ref(new Date().toISOString())
const { isUnmounted: isGone } = useAliveGuard()

const channelLine = computed<StatusLine>(() =>
  data.value ? channelStatus(data.value.channels) : { type: 'info', text: '' }
)

// el-alert 没有 danger 类型
function alertType(type: StatusLine['type']): 'success' | 'warning' | 'info' | 'error' {
  return type === 'danger' ? 'error' : type
}

function apply(result: AlertList) {
  data.value = result
  nowIso.value = new Date().toISOString()
}

async function load() {
  loading.value = true
  try {
    const response = await api.getSystemAlerts()
    if (!isGone()) apply(response.data)
  } catch (error) {
    showApiError(error, '加载系统告警失败')
  } finally {
    loading.value = false
  }
}

async function runChecks() {
  checking.value = true
  try {
    const response = await api.runAlertChecks()
    if (isGone()) return
    apply(response.data)
    ElMessage.success('检查完成')
  } catch (error) {
    showApiError(error, '告警检查失败')
  } finally {
    checking.value = false
  }
}

async function sendTest() {
  testing.value = true
  try {
    const response = await api.sendTestNotification()
    if (!isGone()) testLine.value = testResultMessage(response.data)
  } catch (error) {
    showApiError(error, '发送测试通知失败')
  } finally {
    testing.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}

.header-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.header-actions .el-button + .el-button {
  margin-left: 0;
}

.status-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.hint {
  margin: 4px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.6;
}

.counts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.section-title {
  margin: 20px 0 8px;
  font-size: 15px;
  font-weight: 600;
}

.alert-title {
  font-weight: 500;
}

.alert-message {
  margin-top: 2px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
