<script setup lang="ts">
/**
 * 「更新雪球 Cookie」对话框（仅管理员，入口在采集器卡片）。
 *
 * 粘贴 JSON/请求头或选择插件导出的 .json 文件 → 后端校验并原子替换 XUEQIU_COOKIE_FILE；
 * backend 与采集器都会自动读到新文件，无需重启。提交后输入立即清空，页面上任何时候都
 * 不显示 Cookie 值（后端也只回名称与到期天数）。
 */
import { ref } from 'vue'
import type { UploadInstance } from 'element-plus'
import { formatDateTime } from '@/utils/helpers'
import { cookieTagType } from './collectorStatus'
import {
  cookieFormatLabel,
  cookieKeysText,
  cookieLevelLabel,
  cookieSourceLabel,
  primaryExpirySummary
} from './cookieUpdate'
import { useCookieUpdate } from './useCookieUpdate'

const emit = defineEmits<{ updated: [] }>()

const { state, formatHint, canSubmit, open, close, selectFile, removeFile, submit } =
  useCookieUpdate(() => emit('updated'))

const uploadRef = ref<UploadInstance>()

async function onFileChange(file: { raw?: File }) {
  await selectFile(file.raw)
  // 文本已由 composable 读走；不让 el-upload 的内部列表继续持有文件对象，也允许重选
  uploadRef.value?.clearFiles()
}

defineExpose({ open })
</script>

<template>
  <el-dialog
    v-model="state.visible"
    title="更新雪球 Cookie"
    width="min(640px, 94vw)"
    :close-on-click-modal="false"
    data-testid="xueqiu-cookie-dialog"
    @closed="close"
  >
    <el-alert
      v-if="state.loadError"
      type="error"
      :closable="false"
      :title="state.loadError"
      class="block"
    />
    <el-descriptions
      v-if="state.status"
      :column="1"
      size="small"
      border
      class="block"
      data-testid="xueqiu-cookie-status"
    >
      <el-descriptions-item label="来源">
        {{ cookieSourceLabel(state.status.source) }}
        <span v-if="state.status.file_path" class="muted"> {{ state.status.file_path }}</span>
      </el-descriptions-item>
      <el-descriptions-item label="状态">
        <el-tag :type="cookieTagType(state.status.level)" size="small">
          {{ cookieLevelLabel(state.status.level) }}
        </el-tag>
        <span class="muted"> {{ state.status.message }}</span>
      </el-descriptions-item>
      <el-descriptions-item label="主凭证">
        {{ primaryExpirySummary(state.status.primary) }}
      </el-descriptions-item>
      <el-descriptions-item label="Cookie 名">
        {{ cookieKeysText(state.status.keys) }}
      </el-descriptions-item>
      <el-descriptions-item v-if="state.status.source === 'file'" label="文件时间">
        {{ state.status.file_exists ? formatDateTime(state.status.file_mtime) : '文件不存在' }}
        <template v-if="state.status.backup_exists">
          · 备份 {{ formatDateTime(state.status.backup_mtime) }}
        </template>
      </el-descriptions-item>
    </el-descriptions>
    <el-alert
      v-if="state.status?.read_error"
      type="warning"
      :closable="false"
      :title="state.status.read_error"
      class="block"
    />
    <el-alert
      v-if="state.status && !state.status.writable"
      type="warning"
      :closable="false"
      title="当前部署不能在界面更新 Cookie"
      :description="state.status.writable_reason ?? ''"
      class="block"
      data-testid="xueqiu-cookie-not-writable"
    />

    <template v-if="state.status?.writable">
      <el-input
        v-model="state.text"
        type="textarea"
        :rows="6"
        :disabled="Boolean(state.fileName) || state.submitting"
        autocomplete="off"
        spellcheck="false"
        placeholder="粘贴浏览器插件（如 J2Team Cookies）导出的 JSON，或开发者工具里复制的 Cookie 请求头（xq_a_token=…; xqat=…）"
        data-testid="xueqiu-cookie-input"
      />
      <p v-if="formatHint" class="hint">{{ formatHint }}</p>
      <div class="file-row">
        <el-upload
          ref="uploadRef"
          :auto-upload="false"
          :show-file-list="false"
          accept=".json,.txt"
          :on-change="onFileChange"
        >
          <el-button size="small" :disabled="state.submitting">或选择导出的 JSON 文件</el-button>
        </el-upload>
        <span v-if="state.fileName" class="muted" data-testid="xueqiu-cookie-file">
          已选择 {{ state.fileName }}
          <el-button link type="primary" size="small" @click="removeFile">移除</el-button>
        </span>
      </div>
      <el-checkbox v-model="state.probe" :disabled="state.submitting">
        更新后探活（发一次真实请求确认登录态，约 2–4 秒）
      </el-checkbox>
      <p class="hint">
        必须包含登录凭证 xq_a_token 与 xqat；推荐插件导出的完整 JSON（带到期时间，可提前告警）。
        旧文件会保留为同目录的 .bak，行情与采集器都会自动改用新 Cookie，无需重启。
      </p>
    </template>

    <el-alert
      v-if="state.submitError"
      type="error"
      :closable="false"
      :title="state.submitError"
      class="block"
      data-testid="xueqiu-cookie-error"
    />
    <div v-if="state.result" class="block" data-testid="xueqiu-cookie-result">
      <el-alert type="success" :closable="false" title="Cookie 已更新" />
      <el-descriptions :column="1" size="small" border class="result">
        <el-descriptions-item label="格式">
          {{ cookieFormatLabel(state.result.source_format) }}
        </el-descriptions-item>
        <el-descriptions-item label="主凭证">
          {{ primaryExpirySummary(state.result.status.primary) }}
        </el-descriptions-item>
        <el-descriptions-item label="Cookie 名">
          {{ cookieKeysText(state.result.status.keys) }}
        </el-descriptions-item>
        <el-descriptions-item label="备份">
          {{ state.result.backup_created ? '旧文件已保留为 .bak' : '无旧文件，未生成备份' }}
        </el-descriptions-item>
        <el-descriptions-item v-if="state.result.probe" label="探活">
          <el-tag :type="state.result.probe.ok ? 'success' : 'danger'" size="small">
            {{ state.result.probe.ok ? '成功' : '失败' }}
          </el-tag>
          <span class="muted"> {{ state.result.probe.detail }}</span>
        </el-descriptions-item>
        <el-descriptions-item v-for="(note, index) in state.result.notes" :key="index" label="提示">
          {{ note }}
        </el-descriptions-item>
      </el-descriptions>
    </div>

    <template #footer>
      <el-button @click="state.visible = false">关闭</el-button>
      <el-button
        v-if="state.status?.writable"
        type="primary"
        :disabled="!canSubmit"
        :loading="state.submitting"
        data-testid="xueqiu-cookie-submit"
        @click="submit"
      >
        更新 Cookie
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.block {
  margin-bottom: 12px;
}
.result {
  margin-top: 8px;
}
.hint {
  margin: 6px 0;
  font-size: 12px;
  color: var(--app-text-muted);
}
.muted {
  font-size: 12px;
  color: var(--app-text-muted);
}
.file-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px 0;
  flex-wrap: wrap;
}
</style>
