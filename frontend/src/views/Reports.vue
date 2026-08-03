<template>
  <div class="reports-page">
    <el-card class="page-card" shadow="never">
      <template #header>
        <div class="page-header">
          <div>
            <h2>AI 复盘报告</h2>
            <p class="page-subtitle">
              基于账本全量内部数据生成复盘，并可就报告内容追问讨论（口径标注原样呈现）。
            </p>
          </div>
          <div class="header-actions">
            <el-select
              v-model="scheduleCadence"
              class="schedule-select"
              size="default"
              @change="saveSchedule"
            >
              <el-option label="定期生成：关闭" value="off" />
              <el-option label="定期生成：每周" value="weekly" />
              <el-option label="定期生成：每月" value="monthly" />
            </el-select>
            <el-button
              type="primary"
              :icon="MagicStick"
              :loading="generating"
              @click="generateReport"
            >
              {{ generating ? '生成中…' : '生成新报告' }}
            </el-button>
          </div>
        </div>
      </template>

      <el-row :gutter="16">
        <el-col :xs="24" :md="7">
          <div class="report-list" v-loading="loadingList">
            <el-empty
              v-if="!loadingList && reports.length === 0"
              description="暂无报告，点击右上角生成第一份复盘"
              :image-size="88"
            />
            <div
              v-for="report in reports"
              :key="report.id"
              class="report-item"
              :class="{ active: report.id === selectedId }"
              @click="selectReport(report.id)"
            >
              <div class="report-item-title">{{ report.title }}</div>
              <div class="report-item-meta">
                <el-tag size="small" effect="plain">
                  {{ report.trigger_source === 'scheduled' ? '定期' : '手动' }}
                </el-tag>
                <span>{{ formatDateTime(report.created_at) }}</span>
              </div>
            </div>
          </div>
        </el-col>

        <el-col :xs="24" :md="17">
          <div v-if="!detail && !loadingDetail" class="detail-placeholder">
            <el-empty description="选择左侧报告查看详情" :image-size="88" />
          </div>
          <div v-else v-loading="loadingDetail" class="report-detail">
            <template v-if="detail">
              <div class="detail-toolbar">
                <span class="detail-meta">
                  {{ detail.model }}
                  <template v-if="detail.total_tokens">
                    · {{ detail.total_tokens }} tokens
                  </template>
                </span>
                <el-button type="danger" text size="small" @click="removeReport(detail.id)">
                  删除报告
                </el-button>
              </div>
              <div class="markdown-body" v-html="renderMarkdown(detail.content)" />

              <el-divider content-position="left">追问讨论</el-divider>
              <div class="chat-messages">
                <div
                  v-for="message in detail.messages"
                  :key="message.id"
                  class="chat-message"
                  :class="message.role"
                >
                  <div class="chat-role">{{ message.role === 'user' ? '我' : 'AI' }}</div>
                  <div
                    v-if="message.role === 'assistant'"
                    class="chat-bubble markdown-body"
                    v-html="renderMarkdown(message.content)"
                  />
                  <div v-else class="chat-bubble">{{ message.content }}</div>
                </div>
              </div>
              <div class="chat-input">
                <el-input
                  v-model="question"
                  type="textarea"
                  :rows="2"
                  maxlength="2000"
                  show-word-limit
                  placeholder="就本报告内容提问，例如：为什么胜率是实验口径？"
                  :disabled="asking"
                />
                <el-button
                  type="primary"
                  :loading="asking"
                  :disabled="!question.trim()"
                  @click="ask"
                >
                  {{ asking ? '思考中…' : '追问' }}
                </el-button>
              </div>
              <p class="disclaimer">
                报告与回答由 AI 基于家庭账本数据自动生成，仅供复盘讨论参考，不构成投资建议。
              </p>
            </template>
          </div>
        </el-col>
      </el-row>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { MagicStick } from '@element-plus/icons-vue'
import api from '@/api'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { formatDateTime } from '@/utils/helpers'
import { renderMarkdown } from '@/utils/markdown'
import { pollJobUntilDone } from '@/utils/polling'

interface ReportListItem {
  id: number
  title: string
  trigger_source?: string
  created_at?: string
  [key: string]: unknown
}

interface ReportMessage {
  id: number
  role: string
  content: string
  [key: string]: unknown
}

interface ReportDetail extends ReportListItem {
  model?: string
  total_tokens?: number | null
  content: string
  messages: ReportMessage[]
}

const reports = ref<ReportListItem[]>([])
const detail = ref<ReportDetail | null>(null)
const selectedId = ref<number | null>(null)
const loadingList = ref(false)
const loadingDetail = ref(false)
const generating = ref(false)
const asking = ref(false)
const question = ref('')
const scheduleCadence = ref('off')

async function loadReports({ selectFirst = false } = {}) {
  loadingList.value = true
  // 仅在本次列表请求成功时才允许自动选首项：失败回退旧列表再自动选中，
  // 会在删除后刷新失败的场景里重新请求刚删掉的 id
  let firstId: number | null = null
  try {
    const response = await api.getLlmReports()
    reports.value = response.data
    firstId = reports.value[0]?.id ?? null
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '报告列表加载失败'))
  } finally {
    // 列表到手即解除蒙层：详情加载（可能较慢）不应挡住列表点击
    loadingList.value = false
  }
  if (selectFirst && firstId !== null && !selectedId.value) {
    await selectReport(firstId)
  }
}

async function selectReport(id: number) {
  selectedId.value = id
  loadingDetail.value = true
  try {
    const response = await api.getLlmReport(id)
    // 竞态防护：快速 A→B 切换时，慢返回的 A 不得覆盖当前选中的 B
    if (selectedId.value === id) {
      detail.value = response.data
    }
  } catch (error) {
    if (selectedId.value === id) {
      ElMessage.error(getApiErrorMessage(error, '报告加载失败'))
    }
  } finally {
    if (selectedId.value === id) {
      loadingDetail.value = false
    }
  }
}

async function generateReport() {
  generating.value = true
  try {
    const response = await api.generateLlmReport()
    const job = await pollJobUntilDone(() => api.getLlmReportJob(response.data.id), {
      intervalMs: 3000,
      maxAttempts: 120,
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
    ElMessage.error(getApiErrorMessage(error, '报告生成失败'))
  } finally {
    generating.value = false
  }
}

async function ask() {
  const content = question.value.trim()
  if (!content || !detail.value) return
  // 竞态防护：等待期间用户可能切换报告——只有仍选中同一报告时才追加，
  // 否则丢弃（服务器已落库，切回该报告重新加载即可见）
  const reportId = detail.value.id
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
      ElMessage.error(getApiErrorMessage(error, '追问失败'))
    }
  } finally {
    asking.value = false
  }
}

async function removeReport(id: number) {
  try {
    await ElMessageBox.confirm('删除后报告与全部追问记录不可恢复，确认删除？', '删除报告', {
      type: 'warning'
    })
  } catch {
    return
  }
  try {
    await api.deleteLlmReport(id)
    ElMessage.success('已删除')
    detail.value = null
    selectedId.value = null
    await loadReports({ selectFirst: true })
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '删除失败'))
  }
}

async function loadSchedule() {
  try {
    const response = await api.getLlmReportSchedule()
    scheduleCadence.value = response.data.cadence
  } catch {
    // 静默：调度配置读取失败不阻塞页面
  }
}

async function saveSchedule(cadence: string) {
  try {
    await api.updateLlmReportSchedule(cadence)
    ElMessage.success(
      cadence === 'off'
        ? '已关闭定期生成'
        : `已设置${cadence === 'weekly' ? '每周' : '每月'}自动生成`
    )
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '调度设置失败'))
    await loadSchedule()
  }
}

onMounted(async () => {
  await Promise.all([loadReports({ selectFirst: true }), loadSchedule()])
})
</script>

<style scoped>
.reports-page {
  max-width: 1280px;
  margin: 0 auto;
}

.page-header h2 {
  margin: 0 0 4px;
}

.page-subtitle {
  margin: 0;
  color: var(--app-text-soft);
  font-size: 13px;
}

.schedule-select {
  width: 160px;
}

.report-list {
  min-height: 200px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.report-item {
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 8px;
  cursor: pointer;
  transition: border-color 0.2s;
}

.report-item:hover {
  border-color: var(--el-color-primary-light-5);
}

.report-item.active {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}

.report-item-title {
  font-weight: 600;
  margin-bottom: 4px;
}

.report-item-meta {
  display: flex;
  gap: 8px;
  align-items: center;
  color: var(--app-text-soft);
  font-size: 12px;
}

.detail-placeholder {
  padding: 40px 0;
}

.detail-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.detail-meta {
  color: var(--app-text-soft);
  font-size: 12px;
}

.markdown-body :deep(h2) {
  margin: 18px 0 10px;
  font-size: 18px;
  border-bottom: 1px solid var(--el-border-color-light);
  padding-bottom: 6px;
}

.markdown-body :deep(h3) {
  margin: 14px 0 8px;
  font-size: 15px;
}

.markdown-body :deep(p),
.markdown-body :deep(li) {
  line-height: 1.7;
  font-size: 14px;
}

.markdown-body :deep(table) {
  border-collapse: collapse;
  margin: 10px 0;
  max-width: 100%;
  display: block;
  overflow-x: auto;
}

.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid var(--el-border-color-light);
  padding: 6px 10px;
  font-size: 13px;
}

.chat-messages {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-bottom: 12px;
}

.chat-message {
  display: flex;
  gap: 8px;
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
  background: var(--el-color-info-light-8);
}

.chat-message.user .chat-role {
  background: var(--el-color-primary-light-8);
}

.chat-bubble {
  max-width: 85%;
  padding: 8px 12px;
  border-radius: 10px;
  background: var(--el-fill-color-light);
  font-size: 14px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.chat-message.user .chat-bubble {
  background: var(--el-color-primary-light-9);
  white-space: pre-wrap;
}

.chat-bubble.markdown-body {
  white-space: normal;
}

.chat-input {
  display: flex;
  gap: 10px;
  align-items: flex-end;
}

.chat-input .el-button {
  flex-shrink: 0;
}

.disclaimer {
  margin-top: 14px;
  color: var(--app-text-soft);
  font-size: 12px;
}

@media (max-width: 900px) {
  .report-list {
    margin-bottom: 16px;
  }
}
</style>
