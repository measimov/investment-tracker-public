<template>
  <div class="reports-page">
    <header class="page-header">
      <div>
        <h1 class="page-title">AI 复盘</h1>
        <p class="page-subtitle page-description">
          基于账本全量内部数据生成复盘，并可就报告内容追问讨论（口径标注原样呈现）。
        </p>
      </div>
      <div class="header-actions">
        <select
          class="schedule-select"
          aria-label="定期生成频率"
          :value="scheduleCadence ?? ''"
          :disabled="scheduleLoading || scheduleSaving || scheduleCadence === null"
          @change="saveSchedule"
        >
          <option v-if="scheduleCadence === null" value="">
            {{ scheduleLoading ? '定期配置加载中…' : '定期配置尚未确认' }}
          </option>
          <option value="off">定期生成：关闭</option>
          <option value="weekly">定期生成：每周</option>
          <option value="monthly">定期生成：每月</option>
        </select>
        <NButton
          type="primary"
          :loading="generating"
          :disabled="generating"
          @click="generateReport"
          >{{ generating ? '生成中…' : '生成新报告' }}</NButton
        >
      </div>
    </header>
    <NAlert v-if="scheduleError" type="warning" class="status-note" :show-icon="false">
      定期配置未能确认：{{ scheduleError }}
      <NButton text :loading="scheduleLoading" @click="loadSchedule">重新加载定期配置</NButton>
    </NAlert>
    <div class="report-layout">
      <aside class="report-sidebar" aria-label="复盘报告列表">
        <div class="list-heading">
          <h2>报告档案</h2>
          <span>{{ listHasLoaded ? reports.length + ' 份' : '—' }}</span>
        </div>
        <button
          v-if="isCompact"
          ref="reportPicker"
          type="button"
          class="report-picker"
          :aria-expanded="reportListExpanded"
          aria-controls="report-archive-list"
          @click="reportListExpanded = !reportListExpanded"
        >
          <span class="picker-current">
            <span>{{ selectedReport?.title ?? '尚未选择报告' }}</span>
            <span v-if="selectedReport" class="picker-time">{{
              formatDateTime(selectedReport.created_at)
            }}</span>
          </span>
          <span class="picker-action">{{ reportListExpanded ? '收起报告列表' : '展开选择' }}</span>
        </button>
        <p v-if="isCompact && loadingList" class="mobile-list-status" role="status">
          正在加载报告列表…{{ listHasLoaded ? '保留已加载档案，当前列表尚未确认。' : '' }}
        </p>
        <NAlert v-if="listError" type="warning" :show-icon="false" class="status-note">
          {{
            listHasLoaded
              ? isCompact
                ? '列表更新失败，保留旧列表，当前列表尚未确认。'
                : '列表更新失败，以下为上次成功加载的报告。'
              : '报告列表尚未加载成功。'
          }}{{ listError }}
          <NButton text :loading="loadingList" @click="loadReports({ selectFirst: true })"
            >重新加载报告列表</NButton
          >
        </NAlert>
        <NEmpty
          v-if="
            isCompact && !reportListExpanded && listHasLoaded && !listError && reports.length === 0
          "
          description="暂无报告，可生成第一份复盘"
        />
        <NSpin v-show="!isCompact || reportListExpanded" :show="loadingList">
          <div id="report-archive-list" class="report-list" @keydown.esc="closeReportList">
            <div v-if="loadingList && !listHasLoaded" role="status" aria-label="正在加载报告列表">
              <NSkeleton text :repeat="3" />
            </div>
            <NEmpty
              v-else-if="
                listHasLoaded &&
                !listError &&
                reports.length === 0 &&
                (!isCompact || reportListExpanded)
              "
              description="暂无报告，可生成第一份复盘"
            />
            <button
              v-for="report in reports"
              :key="report.id"
              type="button"
              class="report-item"
              :class="{ active: report.id === selectedId }"
              :aria-current="report.id === selectedId ? 'true' : undefined"
              @click="selectFromList(report.id)"
            >
              <span class="report-item-title">{{ report.title }}</span>
              <span class="report-item-meta"
                ><NTag size="small" :bordered="false">{{
                  report.trigger_source === 'scheduled' ? '定期' : '手动'
                }}</NTag
                ><span>{{ formatDateTime(report.created_at) }}</span></span
              >
            </button>
          </div>
        </NSpin>
      </aside>
      <section class="report-content" aria-label="复盘报告正文">
        <NAlert v-if="detailError" type="warning" :show-icon="false" class="status-note">
          所选报告尚未加载成功：{{ detailError }}
          <NButton
            v-if="selectedId !== null"
            text
            :loading="loadingDetail"
            @click="selectReport(selectedId)"
            >重新加载所选报告</NButton
          >
        </NAlert>
        <div
          v-if="loadingDetail"
          role="status"
          aria-label="正在加载所选报告"
          class="detail-placeholder"
        >
          <NSkeleton text :repeat="5" />
        </div>
        <NEmpty
          v-else-if="!currentDetail && !detailError"
          class="detail-placeholder"
          :description="
            listError && !listHasLoaded ? '报告列表未加载，暂不能选择报告' : '选择报告查看详情'
          "
        />
        <article v-else-if="currentDetail" class="report-detail">
          <div class="detail-toolbar">
            <div>
              <h2>{{ currentDetail.title }}</h2>
              <p class="detail-meta">
                生成于 {{ formatDateTime(currentDetail.created_at) }} · {{ currentDetail.model
                }}<template v-if="currentDetail.total_tokens">
                  · {{ currentDetail.total_tokens }} tokens</template
                >
              </p>
            </div>
            <NButton type="error" text @click="removeReport(currentDetail.id)">删除报告</NButton>
          </div>
          <div class="markdown-body" v-html="renderMarkdown(currentDetail.content)" />
          <section class="discussion" aria-label="追问讨论">
            <h3>追问讨论</h3>
            <div class="chat-messages">
              <div
                v-for="message in currentDetail.messages"
                :key="message.id"
                class="chat-message"
                :class="message.role"
              >
                <div class="chat-role">{{ message.role === 'user' ? '我' : 'AI' }}</div>
                <div class="chat-body">
                  <div
                    v-if="message.role === 'assistant'"
                    class="chat-bubble markdown-body"
                    v-html="renderMarkdown(message.content)"
                  />
                  <div v-else class="chat-bubble">{{ message.content }}</div>
                  <div class="chat-time">{{ formatDateTime(message.created_at) }}</div>
                </div>
              </div>
            </div>
            <div class="chat-input">
              <NInput
                v-model:value="question"
                type="textarea"
                :autosize="{ minRows: 2, maxRows: 8 }"
                :maxlength="2000"
                show-count
                :input-props="{ 'aria-label': '就所选报告追问' }"
                placeholder="就本报告内容提问，例如：为什么胜率是实验口径？"
                :disabled="asking"
              /><NButton
                type="primary"
                :loading="asking"
                :disabled="asking || !question.trim()"
                @click="ask"
                >{{ asking ? '思考中…' : '追问' }}</NButton
              >
            </div>
            <p class="disclaimer">
              报告与回答由 AI 基于家庭账本数据自动生成，仅供复盘讨论参考，不构成投资建议。
            </p>
          </section>
        </article>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { showApiError } from '@/utils/showApiError'
import { computed, onMounted, ref } from 'vue'
import { NAlert, NButton, NEmpty, NInput, NSkeleton, NSpin, NTag } from 'naive-ui'
import { useLatestRequest } from '@/composables/useLatestRequest'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { ElMessage } from 'element-plus'
import { confirmAction } from '@/composables/useConfirmAction'
import api from '@/api'
import { formatDateTime } from '@/utils/helpers'
import { renderMarkdown } from '@/utils/markdown'
import { pollJobUntilDone } from '@/utils/polling'
import { useAliveGuard } from '@/composables/useAliveGuard'
import type { LlmReportDetail, LlmReportListItem, LlmReportMessage } from '@/types'

// 后端 LLM 报告 schema 为准（生成类型，PR #172 复审）
type ReportListItem = LlmReportListItem
type ReportMessage = LlmReportMessage
type ReportDetail = LlmReportDetail

const reports = ref<ReportListItem[]>([])
const detail = ref<ReportDetail | null>(null)
const selectedId = ref<number | null>(null)
const loadingList = ref(false)
const loadingDetail = ref(false)
const { isUnmounted } = useAliveGuard()
const generating = ref(false)
const asking = ref(false)
const question = ref('')
const scheduleCadence = ref<string | null>(null)
const listHasLoaded = ref(false)
const listError = ref('')
const detailError = ref('')
const scheduleLoading = ref(false)
const scheduleError = ref('')
const scheduleSaving = ref(false)
const listRequest = useLatestRequest()
const detailRequest = useLatestRequest()
const scheduleRequest = useLatestRequest()
const currentDetail = computed(() => (detail.value?.id === selectedId.value ? detail.value : null))
const isCompact = useMediaQuery('(max-width: 900px)')
const reportListExpanded = ref(false)
const reportPicker = ref<HTMLButtonElement | null>(null)
const selectedReport = computed(
  () => reports.value.find((report) => report.id === selectedId.value) ?? currentDetail.value
)

function closeReportList(event?: KeyboardEvent) {
  if (!isCompact.value) return
  event?.preventDefault()
  reportListExpanded.value = false
  reportPicker.value?.focus()
}
function selectFromList(id: number) {
  closeReportList()
  return selectReport(id)
}

async function loadReports({ selectFirst = false } = {}) {
  const request = listRequest.begin()
  loadingList.value = true
  listError.value = ''
  // 仅在本次列表请求成功时才允许自动选首项：失败回退旧列表再自动选中，
  // 会在删除后刷新失败的场景里重新请求刚删掉的 id
  let firstId: number | null = null
  try {
    const response = await api.getLlmReports()
    if (!listRequest.isCurrent(request)) return
    reports.value = response.data
    listHasLoaded.value = true
    firstId = reports.value[0]?.id ?? null
  } catch (error) {
    if (!listRequest.isCurrent(request)) return
    listError.value = getApiErrorMessage(error, '报告列表加载失败')
    showApiError(error, '报告列表加载失败')
  } finally {
    // 列表到手即解除蒙层：详情加载（可能较慢）不应挡住列表点击
    if (listRequest.isCurrent(request)) loadingList.value = false
  }
  if (listRequest.isCurrent(request) && selectFirst && firstId !== null && !selectedId.value) {
    await selectReport(firstId)
  }
}

async function selectReport(id: number) {
  const request = detailRequest.begin()
  selectedId.value = id
  detail.value = null
  detailError.value = ''
  loadingDetail.value = true
  try {
    const response = await api.getLlmReport(id)
    if (detailRequest.isCurrent(request)) detail.value = response.data
  } catch (error) {
    if (detailRequest.isCurrent(request)) {
      detailError.value = getApiErrorMessage(error, '报告加载失败')
      showApiError(error, '报告加载失败')
    }
  } finally {
    if (detailRequest.isCurrent(request)) loadingDetail.value = false
  }
}

async function generateReport() {
  generating.value = true
  try {
    const response = await api.generateLlmReport()
    const job = await pollJobUntilDone(() => api.getLlmReportJob(response.data.id), {
      intervalMs: 3000,
      maxAttempts: 120,
      isCancelled: isUnmounted,
      timeoutMessage: '报告仍在生成中，请稍后刷新列表查看',
      failureMessage: '报告生成失败'
    })
    const reportId = job?.report_id
    if (typeof reportId === 'number') {
      ElMessage.success('报告已生成')
      selectedId.value = null
      await loadReports()
      await selectReport(reportId)
    }
  } catch (error) {
    if (!isUnmounted()) showApiError(error, '报告生成失败')
  } finally {
    if (!isUnmounted()) generating.value = false
  }
}

async function ask() {
  const content = question.value.trim()
  if (!content || !currentDetail.value || loadingDetail.value) return
  // 竞态防护：等待期间用户可能切换报告——只有仍选中同一报告时才追加，
  // 否则丢弃（服务器已落库，切回该报告重新加载即可见）
  const reportId = currentDetail.value.id
  asking.value = true
  try {
    const response = await api.askLlmReport(reportId, content)
    // 等待期间输入框全局禁用，成功后无条件清空已提交文本——
    // 否则切换报告后 A 的问题会残留在 B 的输入框里被误再次提交
    question.value = ''
    if (detail.value?.id === reportId) {
      detail.value.messages.push(response.data.question, response.data.answer)
    }
  } catch (error) {
    if (detail.value?.id !== reportId) {
      // 已切到其他报告：清掉 A 的草稿，避免残留在 B 的输入框被误提交
      question.value = ''
    } else {
      showApiError(error, '追问失败')
    }
  } finally {
    asking.value = false
  }
}

async function removeReport(id: number) {
  if (currentDetail.value?.id !== id || loadingDetail.value) return
  if (
    !(await confirmAction({
      title: '删除报告',
      message: '删除后报告与全部追问记录不可恢复，确认删除？',
      confirmText: '删除'
    }))
  )
    return
  try {
    await api.deleteLlmReport(id)
    ElMessage.success('报告已删除')
    detailRequest.invalidate()
    detail.value = null
    selectedId.value = null
    loadingDetail.value = false
    detailError.value = ''
    await loadReports({ selectFirst: true })
  } catch (error) {
    showApiError(error, '删除失败')
  }
}

async function loadSchedule() {
  const request = scheduleRequest.begin()
  scheduleLoading.value = true
  scheduleError.value = ''
  try {
    const response = await api.getLlmReportSchedule()
    if (scheduleRequest.isCurrent(request)) scheduleCadence.value = response.data.cadence
  } catch (error) {
    if (scheduleRequest.isCurrent(request))
      scheduleError.value = getApiErrorMessage(error, '定期配置加载失败')
  } finally {
    if (scheduleRequest.isCurrent(request)) scheduleLoading.value = false
  }
}

async function saveSchedule(event: Event) {
  const cadence = (event.target as HTMLSelectElement).value
  scheduleCadence.value = cadence
  scheduleSaving.value = true
  try {
    await api.updateLlmReportSchedule(cadence)
    if (isUnmounted()) return
    scheduleCadence.value = cadence
    ElMessage.success(
      cadence === 'off'
        ? '已关闭定期生成'
        : `已设置${cadence === 'weekly' ? '每周' : '每月'}自动生成`
    )
  } catch (error) {
    if (!isUnmounted()) {
      showApiError(error, '调度设置失败')
      scheduleCadence.value = null
      await loadSchedule()
    }
  } finally {
    if (!isUnmounted()) scheduleSaving.value = false
  }
}

onMounted(async () => {
  await Promise.all([loadReports({ selectFirst: true }), loadSchedule()])
})
</script>

<style scoped>
.reports-page {
  max-width: 1440px;
  margin: 0 auto;
}
.page-header {
  display: flex;
  justify-content: space-between;
  gap: 24px;
  align-items: flex-start;
  padding: 14px 0 28px;
  border-bottom: 1px solid var(--app-border-soft);
}
.page-subtitle {
  max-width: 600px;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  flex-shrink: 0;
}
.schedule-select {
  width: 180px;
  min-height: 36px;
  padding: 0 10px;
  color: var(--app-text);
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-sm);
  font: inherit;
  font-size: 13px;
}
.schedule-select:disabled {
  color: var(--app-text-muted);
}
.status-note {
  margin: 16px 0;
}
.status-note :deep(.n-alert-body__content) {
  overflow-wrap: anywhere;
}
.status-note .n-button {
  margin-left: 8px;
}
.report-layout {
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr);
  gap: 32px;
  margin-top: 26px;
}
.report-sidebar {
  min-width: 0;
}
.list-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 14px;
}
.list-heading h2 {
  font-size: 14px;
  font-weight: 600;
  margin: 0;
}
.list-heading span {
  font-size: 12px;
  color: var(--app-text-muted);
}
.report-list {
  min-height: 120px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.report-item {
  width: 100%;
  text-align: left;
  padding: 14px 12px;
  color: var(--app-text);
  border: 1px solid transparent;
  border-bottom-color: var(--app-border-soft);
  border-radius: var(--app-radius-sm);
  background: transparent;
  cursor: pointer;
  font: inherit;
}
.report-item:hover {
  background: var(--app-surface-secondary);
}
.report-item.active {
  border-color: var(--app-border-soft);
  background: var(--app-surface-secondary);
}
.report-item.active .report-item-title {
  color: var(--app-primary-strong);
}
.report-item:focus-visible,
.report-picker:focus-visible,
.schedule-select:focus-visible {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: 2px;
}
.report-picker {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
  min-height: 44px;
  padding: 12px;
  text-align: left;
  font: inherit;
  font-size: 14px;
  color: var(--app-text);
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-sm);
  cursor: pointer;
}
.picker-current {
  display: grid;
  gap: 4px;
  min-width: 0;
  overflow-wrap: anywhere;
}
.picker-time,
.picker-action,
.mobile-list-status {
  font-size: 13px;
  line-height: 1.7;
}
.picker-action {
  color: var(--app-primary-strong);
  flex-shrink: 0;
}
.mobile-list-status {
  margin: 12px 0;
  color: var(--app-text);
}
.report-item-title {
  display: block;
  font-weight: 600;
  font-size: 14px;
  margin-bottom: 9px;
  overflow-wrap: anywhere;
}
.report-item-meta {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
  color: var(--app-text-muted);
  font-size: 12px;
}
.report-content {
  min-width: 0;
  padding: 24px 28px;
  background: var(--app-surface);
  border: 1px solid var(--app-border-soft);
  border-radius: var(--app-radius);
}
.report-detail {
  min-width: 0;
}
.detail-placeholder {
  padding: 32px 0;
}
.detail-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 24px;
}
.detail-toolbar h2 {
  font-family: var(--app-font-serif);
  font-weight: 500;
  font-size: 24px;
  line-height: 1.5;
  margin: 0 0 8px;
  overflow-wrap: anywhere;
}
.detail-toolbar .n-button {
  flex-shrink: 0;
}
.detail-meta {
  color: var(--app-text-muted);
  font-size: 12px;
  line-height: 1.7;
  margin: 0;
  overflow-wrap: anywhere;
}
.markdown-body :deep(th),
.markdown-body :deep(td) {
  min-width: 96px;
}
.markdown-body :deep(table):focus-visible,
.markdown-body :deep(pre):focus-visible {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: 2px;
}
.discussion {
  margin-top: 32px;
  padding-top: 22px;
  border-top: 1px solid var(--app-border-soft);
}
.discussion h3 {
  font-size: 16px;
  margin: 0 0 18px;
}
.chat-messages {
  display: flex;
  flex-direction: column;
  gap: 14px;
  margin-bottom: 18px;
}
.chat-message {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}
.chat-message.user {
  flex-direction: row-reverse;
}
.chat-role {
  flex-shrink: 0;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  background: var(--app-surface-secondary);
  color: var(--app-text-muted);
}
.chat-body {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  max-width: calc(100% - 42px);
  min-width: 0;
}
.chat-message.user .chat-body {
  align-items: flex-end;
}
.chat-time {
  margin-top: 4px;
  font-size: 12px;
  color: var(--app-text-muted);
  font-variant-numeric: tabular-nums;
}
.chat-bubble {
  max-width: 100%;
  padding: 10px 14px;
  border-radius: var(--app-radius-sm);
  background: var(--app-surface-secondary);
  line-height: 1.8;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.chat-message.user .chat-bubble {
  font-size: 16px;
}
.chat-bubble.markdown-body {
  max-width: 38em;
  white-space: normal;
}
.chat-input {
  display: flex;
  gap: 10px;
  align-items: flex-end;
}
.chat-input .n-button {
  flex-shrink: 0;
}
.disclaimer {
  margin: 14px 0 0;
  color: var(--app-text-muted);
  font-size: 12px;
  line-height: 1.7;
}
@media (max-width: 1100px) {
  .header-actions {
    flex-shrink: 1;
  }
  .report-layout {
    grid-template-columns: 220px minmax(0, 1fr);
    gap: 20px;
  }
  .report-content {
    padding: 22px;
  }
}
@media (max-width: 900px) {
  .page-header {
    flex-direction: column;
    gap: 18px;
  }
  .report-layout {
    grid-template-columns: minmax(0, 1fr);
    margin-top: 22px;
  }
  .report-list {
    min-height: 0;
  }
  .report-content {
    padding: 20px 16px;
  }
  .detail-toolbar {
    flex-wrap: wrap;
    gap: 8px;
  }
}
@media (max-width: 767px) {
  .schedule-select,
  .reports-page :deep(.n-button) {
    min-height: 44px;
  }
  .chat-input {
    flex-direction: column;
    align-items: stretch;
  }
  .chat-input .n-button {
    align-self: flex-end;
  }
}

@media (min-width: 1025px) {
  .page-header {
    padding: 4px 0 16px;
  }
  .report-layout {
    gap: 20px;
    margin-top: 16px;
  }
  .report-item {
    padding: 10px 12px;
  }
  .report-content {
    padding: 16px 20px;
  }
}
</style>
