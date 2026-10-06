<template>
  <div class="watchlist-page">
    <header class="watchlist-header">
      <div>
        <h1 class="page-title">观察清单</h1>
        <p class="page-scope page-description">
          {{
            loading
              ? '正在加载观察标的'
              : !hasLoaded
                ? '观察标的尚未确认'
                : `${items.length} 个观察标的${loadError ? ' · 上次成功加载' : ''}`
          }}
        </p>
      </div>
      <div class="header-actions">
        <NButton
          aria-label="重新加载"
          :loading="loading"
          :disabled="loading"
          @click="loadWatchlist()"
          >重新加载</NButton
        >
        <NButton type="primary" data-testid="add-watchlist-button" @click="openAddDialog"
          >添加观察</NButton
        >
      </div>
    </header>
    <p class="watchlist-note">跟踪未持仓标的的价格、观察理由与研究进展。</p>
    <NAlert
      v-if="loadError"
      type="warning"
      :bordered="false"
      class="load-warning"
      title="观察清单加载失败"
    >
      {{
        hasLoaded ? '显示上次成功加载的观察标的，尚未确认最新结果。' : '尚未确认观察标的，请重试。'
      }}
      <NButton text type="primary" :disabled="loading" @click="loadWatchlist()">重试加载</NButton>
    </NAlert>
    <WatchlistTable
      :items="items"
      :loading="loading"
      :empty-description="emptyDescription"
      :badge-for="announcements.badgeFor"
      @edit="openEditDialog"
      @remove="removeItem"
    />
    <el-dialog
      v-model="dialogVisible"
      :title="editingId === null ? '添加观察标的' : '编辑观察标的'"
      width="480px"
      class="watchlist-dialog"
      :style="{ '--el-text-color-placeholder': 'var(--app-text-soft)' }"
      :close-on-click-modal="false"
    >
      <el-form :model="form" :rules="rules" ref="formRef" label-width="90px">
        <el-form-item label="标的代码" prop="symbol">
          <SecuritySelect
            v-model="form.symbol"
            :market="form.market"
            :disabled="editingId !== null"
            placeholder="如: 600036, AAPL, 00700；支持名称/拼音检索"
            @select="onSymbolSelected"
            @free-text="onSymbolFreeText"
            @resolved="onSymbolResolved"
          />
        </el-form-item>
        <el-form-item label="市场" prop="market">
          <el-select v-model="form.market" placeholder="选择市场" :disabled="editingId !== null">
            <el-option v-for="m in MARKETS" :key="m" :label="m" :value="m" />
          </el-select>
        </el-form-item>
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="可选，便于列表辨认" />
        </el-form-item>
        <el-form-item label="观察理由">
          <el-input
            v-model="form.note"
            type="textarea"
            :rows="3"
            maxlength="500"
            show-word-limit
            placeholder="为什么观察：一句话理由/买入条件"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="mobile-dialog-footer">
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button
            type="primary"
            :loading="submitting"
            data-testid="watchlist-submit"
            @click="handleSubmit"
          >
            确定
          </el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { makeConfirmedAction } from '@/composables/useConfirmAction'
import { showApiError } from '@/utils/showApiError'
import { ref, reactive, computed, onMounted } from 'vue'
import { NAlert, NButton } from 'naive-ui'
import WatchlistTable from './watchlist/WatchlistTable.vue'
import { useLatestRequest } from '@/composables/useLatestRequest'
import { ElMessage, type FormInstance } from 'element-plus'
import api from '../api'
import SecuritySelect from '../components/SecuritySelect.vue'
import { useAutoReload } from '../composables/useAutoReload'
import { useRecentAnnouncements } from '../composables/useRecentAnnouncements'
import {
  MARKETS,
  freeTextFormPatch,
  resolvedFormPatch,
  securityFormPatch
} from '../utils/securities'
import type { SecurityResolveResponse, SecuritySearchItem, WatchlistItem } from '../types'

const loading = ref(false)
const hasLoaded = ref(false)
const loadError = ref(false)
const latestRequest = useLatestRequest()
const emptyDescription = computed(() =>
  loading.value
    ? '正在加载观察标的'
    : loadError.value
      ? '观察清单暂不可用，请重试'
      : !hasLoaded.value
        ? '观察标的尚未加载'
        : '暂无观察标的；点击添加观察'
)
const items = ref<WatchlistItem[]>([])
const dialogVisible = ref(false)
const submitting = ref(false)
const editingId = ref<number | null>(null)
const formRef = ref<FormInstance | null>(null)

const form = reactive({ symbol: '', market: '', name: '', note: '' })
// 近 7 天重要公告徽标（与持仓页同一数据；失败静默）
const announcements = useRecentAnnouncements()

const rules = {
  symbol: [{ required: true, message: '请输入标的代码', trigger: 'blur' }],
  market: [{ required: true, message: '请选择市场', trigger: 'change' }]
}

async function loadWatchlist(options: { silent?: boolean } = {}) {
  const request = latestRequest.begin()
  // 自动重读保留静默约定，不转圈、不弹错；迟到结果不能覆盖之后的手动重载。
  if (!options.silent) loading.value = true
  try {
    const response = await api.getWatchlist({ skipGlobalErrorNotification: options.silent })
    if (!latestRequest.isCurrent(request)) return
    items.value = response.data
    hasLoaded.value = true
    loadError.value = false
  } catch (error) {
    if (!latestRequest.isCurrent(request)) return
    if (options.silent) throw error
    loadError.value = true
    showApiError(error, '加载观察清单失败')
  } finally {
    // 最新静默读取可能接管尚未完成的手动读取；它结束时同样需要解除加载状态。
    if (latestRequest.isCurrent(request)) loading.value = false
  }
}

// 报价由后端交易时段每 15 分钟刷新；页面可见时每 5 分钟静默重读一次（对话框打开时不打断）
useAutoReload(() => loadWatchlist({ silent: true }), { paused: () => dialogVisible.value })

// 自选表单没有币种：只取 symbol/market/name；解析结果只补空名称
function onSymbolSelected(item: SecuritySearchItem) {
  const { symbol, market, name } = securityFormPatch(item)
  Object.assign(form, { symbol, market, name })
}

function onSymbolFreeText(payload: { symbol: string; lastPicked: SecuritySearchItem | null }) {
  const { symbol, name } = freeTextFormPatch(form, payload)
  form.symbol = symbol ?? form.symbol
  if (name !== undefined) form.name = name
}

function onSymbolResolved(result: SecurityResolveResponse) {
  const { name } = resolvedFormPatch(form, result)
  if (name !== undefined) form.name = name
}

function openAddDialog() {
  editingId.value = null
  Object.assign(form, { symbol: '', market: '', name: '', note: '' })
  formRef.value?.clearValidate()
  dialogVisible.value = true
}

function openEditDialog(row: WatchlistItem) {
  editingId.value = row.id
  Object.assign(form, {
    symbol: row.symbol,
    market: row.market,
    name: row.name || '',
    note: row.note || ''
  })
  formRef.value?.clearValidate()
  dialogVisible.value = true
}

async function handleSubmit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  submitting.value = true
  try {
    if (editingId.value === null) {
      await api.addWatchlistItem({
        symbol: form.symbol,
        market: form.market,
        name: form.name || null,
        note: form.note || null
      })
      ElMessage.success('已加入观察清单')
    } else {
      await api.updateWatchlistItem(editingId.value, {
        name: form.name || null,
        note: form.note || null
      })
      ElMessage.success('观察标的已更新')
    }
    dialogVisible.value = false
    await loadWatchlist()
  } catch (error) {
    showApiError(error, editingId.value === null ? '添加失败' : '更新失败')
  } finally {
    submitting.value = false
  }
}

const removeItem = makeConfirmedAction<WatchlistItem>({
  title: '移出观察',
  confirmText: '移出观察',
  message: (row) => `确定把 ${row.symbol} 移出观察清单吗？标的档案与已生成的分析不受影响。`,
  request: (row) => api.removeWatchlistItem(row.id),
  successMessage: '已移出观察清单',
  failureMessage: '移出观察失败',
  reload: () => loadWatchlist()
})

onMounted(() => {
  loadWatchlist()
  announcements.load()
})
</script>

<style scoped>
.watchlist-page {
  width: 100%;
}
.watchlist-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 24px;
  margin-bottom: 24px;
}
.header-actions {
  display: flex;
  gap: 10px;
}
.watchlist-note {
  font-size: 13px;
  line-height: 1.75;
  color: var(--app-text-muted);
  max-width: 80ch;
  margin: 0 0 24px;
}
.load-warning {
  margin-bottom: 20px;
}
.load-warning :deep(.n-button) {
  margin-left: 12px;
}
@media (min-width: 1025px) {
  .watchlist-header {
    margin-bottom: var(--app-space-md);
  }
  .watchlist-note {
    margin-bottom: var(--app-space-sm);
  }
}
@media (max-width: 640px) {
  .watchlist-header {
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 18px;
  }
  .header-actions {
    width: 100%;
  }
  .header-actions :deep(.n-button) {
    min-height: 44px;
  }
  .watchlist-note {
    margin-bottom: 20px;
  }
  :deep(.watchlist-dialog .el-input__wrapper),
  :deep(.watchlist-dialog .el-select__wrapper) {
    min-height: 44px;
  }
  :deep(.watchlist-dialog .el-input__inner) {
    height: 40px;
  }
}
</style>
