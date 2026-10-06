<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { NAlert, NButton, NTag } from 'naive-ui'
import api from '@/api'
import type { AlertList, NotificationEventList } from '@/types'
import { formatNumber } from '@/utils/helpers'
import { showApiError } from '@/utils/showApiError'
import { useAliveGuard } from '@/composables/useAliveGuard'
import { useLatestRequest } from '@/composables/useLatestRequest'
import AlertRowsTable from './alerts/AlertRowsTable.vue'
import NotificationEventsTable from './alerts/NotificationEventsTable.vue'
import {
  channelStatus,
  eventSettingsText,
  testResultMessage,
  type StatusLine
} from './systemAlerts'
const data = ref<AlertList | null>(null)
const events = ref<NotificationEventList | null>(null)
const alertsLoading = ref(false),
  eventsLoading = ref(false)
const alertsError = ref(false),
  eventsError = ref(false)
const checking = ref(false),
  testing = ref(false)
const testLine = ref<StatusLine | null>(null)
const nowIso = ref(new Date().toISOString())
const { isUnmounted: isGone } = useAliveGuard()
const alertsRequest = useLatestRequest(),
  eventsRequest = useLatestRequest()
const loading = computed(() => alertsLoading.value || eventsLoading.value)
const channelLine = computed<StatusLine>(() =>
  data.value ? channelStatus(data.value.channels) : { type: 'info', text: '' }
)
function alertType(type: StatusLine['type']) {
  return type === 'danger' ? 'error' : type === 'info' ? 'default' : type
}
function apply(result: AlertList) {
  data.value = result
  alertsError.value = false
  nowIso.value = new Date().toISOString()
}
function alertEmpty(resolved: boolean) {
  if (alertsLoading.value && !data.value) return '正在读取告警记录'
  if (!data.value) return '告警记录尚未加载成功'
  return resolved ? '近 7 天没有恢复的告警' : '没有未恢复的告警'
}
const eventEmpty = computed(() =>
  eventsLoading.value && !events.value
    ? '正在读取事件提醒'
    : !events.value
      ? '事件提醒尚未加载成功'
      : '还没有事件提醒'
)
async function loadEvents() {
  const token = eventsRequest.begin()
  eventsLoading.value = true
  eventsError.value = false
  try {
    const response = await api.getNotificationEvents()
    if (eventsRequest.isCurrent(token)) events.value = response.data
  } catch (error) {
    if (!eventsRequest.isCurrent(token)) return
    eventsError.value = true
    showApiError(error, '加载事件提醒失败')
  } finally {
    if (eventsRequest.isCurrent(token)) eventsLoading.value = false
  }
}
async function loadAlerts() {
  const token = alertsRequest.begin()
  alertsLoading.value = true
  alertsError.value = false
  try {
    const response = await api.getSystemAlerts()
    if (alertsRequest.isCurrent(token)) apply(response.data)
  } catch (error) {
    if (!alertsRequest.isCurrent(token)) return
    alertsError.value = true
    showApiError(error, '加载系统告警失败')
  } finally {
    if (alertsRequest.isCurrent(token)) alertsLoading.value = false
  }
}
async function load() {
  await Promise.all([loadAlerts(), loadEvents()])
}
async function runChecks() {
  checking.value = true
  try {
    const response = await api.runAlertChecks()
    if (isGone()) return
    apply(response.data)
    ElMessage.success('检查完成')
  } catch (error) {
    if (!isGone()) showApiError(error, '告警检查失败')
  } finally {
    if (!isGone()) checking.value = false
  }
}
async function sendTest() {
  testing.value = true
  try {
    const response = await api.sendTestNotification()
    if (!isGone()) testLine.value = testResultMessage(response.data)
  } catch (error) {
    if (!isGone()) showApiError(error, '发送测试通知失败')
  } finally {
    if (!isGone()) testing.value = false
  }
}
onMounted(load)
</script>
<template>
  <div class="system-alerts-page">
    <header class="page-heading">
      <div>
        <h1 class="page-title">系统告警</h1>
        <p class="page-intro page-description">核对当前告警、恢复记录与事件提醒。</p>
      </div>
      <div class="header-actions">
        <NButton
          :loading="loading"
          aria-label="重新加载系统告警与事件提醒"
          :aria-busy="loading"
          :aria-disabled="loading"
          @click="load"
          >重新加载</NButton
        >
        <NButton
          :loading="checking"
          aria-label="立即检查"
          :aria-busy="checking"
          :aria-disabled="checking"
          @click="runChecks"
          >立即检查</NButton
        >
        <NButton
          :loading="testing"
          aria-label="发送测试通知"
          :aria-busy="testing"
          :aria-disabled="testing"
          type="primary"
          @click="sendTest"
          >发送测试通知</NButton
        >
      </div>
    </header>
    <NAlert
      v-if="alertsError"
      type="warning"
      :show-icon="false"
      title="系统告警读取失败"
      class="read-alert"
    >
      {{
        data
          ? '下方保留上次成功的告警与渠道状态，尚未确认最新结果。'
          : '尚未确认告警与通知渠道状态，读取失败不表示系统无告警。'
      }}
      <NButton text type="primary" aria-label="重试告警读取" @click="loadAlerts">重试加载</NButton>
    </NAlert>
    <p v-else-if="!data" class="read-note" role="status">
      {{ alertsLoading ? '正在读取告警与通知渠道状态。' : '告警与通知渠道状态尚未加载成功。' }}
    </p>
    <p v-else-if="alertsLoading" class="read-note" role="status">
      正在重新加载，下方为上次成功的告警与渠道状态。
    </p>
    <section class="status-block" aria-label="告警与通知状态">
      <div class="counts">
        <NTag type="error" :bordered="false"
          >严重 {{ formatNumber(data?.counts.critical, 0) }}</NTag
        >
        <NTag type="warning" :bordered="false"
          >警告 {{ formatNumber(data?.counts.warning, 0) }}</NTag
        >
        <NTag :bordered="false">提示 {{ formatNumber(data?.counts.info, 0) }}</NTag>
        <NTag
          type="success"
          :bordered="false"
          :theme-overrides="{ colorSuccess: 'var(--app-success-soft)' }"
          >近 7 天已恢复 {{ formatNumber(data?.counts.recent_resolved, 0) }}</NTag
        >
      </div>
      <template v-if="data">
        <NAlert
          :title="channelLine.text"
          :type="alertType(channelLine.type)"
          :show-icon="false"
          class="read-alert"
        />
        <NAlert
          v-if="!data.check_enabled"
          title="周期告警检查已关闭（ALERT_CHECK_ENABLED=false）：只有「立即检查」与外部信号会更新本页"
          type="warning"
          :show-icon="false"
          class="read-alert"
        />
        <p class="read-note">
          每 {{ data.check_interval_minutes }} 分钟检查一次（雪球采集器 / Cookie /
          WAF、汇率数据源、周期任务、后台任务，以及备份脚本等外部信号）；新告警与升级立即推送，未恢复的每
          {{ data.reminder_hours }} 小时提醒一次，恢复时推送「已恢复」。
        </p>
      </template>
      <NAlert
        v-if="testLine"
        :title="testLine.text"
        :type="alertType(testLine.type)"
        :show-icon="false"
        class="read-alert"
        ><NButton text aria-label="关闭测试通知结果" @click="testLine = null">关闭</NButton></NAlert
      >
    </section>
    <section class="alerts-section" aria-label="当前告警">
      <div class="section-heading">
        <h2>当前告警</h2>
        <span>{{ data ? `${data.active.length} 条记录` : '尚未确认' }}</span>
      </div>
      <AlertRowsTable
        :rows="data?.active ?? []"
        :loading="alertsLoading"
        :empty-description="alertEmpty(false)"
        :min-severity="data?.channels.min_severity ?? 'warning'"
        :now-iso="nowIso"
      />
    </section>
    <section class="alerts-section" aria-label="近7天已恢复告警">
      <div class="section-heading">
        <h2>近 7 天已恢复</h2>
        <span>{{ data ? `${data.recent_resolved.length} 条记录` : '尚未确认' }}</span>
      </div>
      <AlertRowsTable
        :rows="data?.recent_resolved ?? []"
        :loading="alertsLoading"
        :empty-description="alertEmpty(true)"
        :min-severity="data?.channels.min_severity ?? 'warning'"
        :now-iso="nowIso"
        resolved
      />
    </section>
    <section class="alerts-section" aria-label="最近提醒">
      <div class="section-heading">
        <h2>最近提醒</h2>
        <span>{{ events ? `${events.items.length} 条记录` : '尚未确认' }}</span>
      </div>
      <NAlert
        v-if="eventsError"
        type="warning"
        :show-icon="false"
        title="事件提醒读取失败"
        class="read-alert"
        >{{
          events
            ? '保留上次成功的提醒与设置，尚未确认最新结果。'
            : '尚未确认事件提醒与设置，读取失败不表示无提醒。'
        }}<NButton text type="primary" aria-label="重试事件提醒读取" @click="loadEvents"
          >重试加载</NButton
        ></NAlert
      >
      <p v-else-if="eventsLoading && events" class="read-note" role="status">
        正在重新加载，下方为上次成功的事件提醒。
      </p>
      <p v-if="events" class="read-note">{{ eventSettingsText(events) }}</p>
      <p v-if="events && events.items.length >= 50" class="read-note">
        当前显示最近最多 50 条事件提醒，不代表全部历史。
      </p>
      <NotificationEventsTable
        :rows="events?.items ?? []"
        :loading="eventsLoading"
        :empty-description="eventEmpty"
      />
    </section>
  </div>
</template>
<style scoped>
.system-alerts-page {
  min-width: 0;
  width: 100%;
}
.page-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 28px;
}
.header-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.status-block {
  margin-bottom: 30px;
}
.counts {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  padding: 18px 0;
  border-block: 1px solid var(--app-border);
  margin-bottom: 18px;
}
.read-alert {
  margin: 12px 0;
  overflow-wrap: anywhere;
}
.read-note {
  font-size: 13px;
  line-height: 1.7;
  color: var(--app-text-muted);
  margin: 12px 0;
  overflow-wrap: anywhere;
}
.alerts-section {
  margin-top: 30px;
  min-width: 0;
}
.section-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 18px;
}
h2 {
  font-size: 20px;
  font-weight: 600;
  margin: 0;
}
.section-heading > span {
  font-size: 13px;
  color: var(--app-text-muted);
}
@media (max-width: 1100px) {
  .page-heading {
    align-items: flex-start;
    flex-wrap: wrap;
  }
}
@media (max-width: 640px) {
  .page-heading {
    gap: 16px;
  }
  .header-actions :deep(.n-button),
  .read-alert :deep(.n-button) {
    min-height: 44px;
  }
  .counts {
    gap: 8px;
  }
  .status-block {
    margin-bottom: 24px;
  }
  .alerts-section {
    margin-top: 24px;
  }
}

@media (min-width: 1025px) {
  .page-heading {
    margin-bottom: 16px;
  }
  .status-block {
    margin-bottom: 20px;
  }
  .counts {
    padding: 12px 0;
  }
  .alerts-section {
    margin-top: 20px;
  }
  .section-heading {
    margin-bottom: 12px;
  }
  h2 {
    font-size: 18px;
  }
}
</style>
