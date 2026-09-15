<template>
  <div class="watchlist-page">
    <el-card class="watchlist-card">
      <template #header>
        <div class="page-header">
          <span>观察清单</span>
          <div class="header-actions">
            <el-button
              type="primary"
              :icon="Plus"
              data-testid="add-watchlist-button"
              @click="openAddDialog"
            >
              添加观察
            </el-button>
          </div>
        </div>
      </template>

      <el-alert
        title="纳入观察但未持仓的标的：点击代码进入标的档案（AI 分析、财报摘要、格雷厄姆准则对观察标的同样可用）。准则达标为防御型投资者标准的预计算结果，不可判定 = 数据源边界而非不达标。"
        type="info"
        :closable="false"
        show-icon
        class="watchlist-note"
      />

      <div v-if="!isMobileView" class="responsive-table desktop-data-table">
        <el-table :data="items" v-loading="loading" stripe row-key="id">
          <template #empty>
            <el-empty description="暂无观察标的；点击右上角添加" :image-size="88" />
          </template>
          <el-table-column label="代码" width="110">
            <template #default="{ row }">
              <el-link type="primary" :underline="false" @click="openSecurityDetail(row)">
                {{ row.symbol }}
              </el-link>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="名称" min-width="120" show-overflow-tooltip />
          <el-table-column prop="market" label="市场" width="90" />
          <el-table-column prop="note" label="观察理由" min-width="220" show-overflow-tooltip />
          <el-table-column label="当前价" min-width="130" align="right">
            <template #default="{ row }">
              <template v-if="row.current_price != null">
                {{ formatNumber(row.current_price, 4) }}
                <div class="price-updated-at">{{ formatDateTime(row.price_updated_at) }}</div>
              </template>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="格雷厄姆准则" min-width="150">
            <template #default="{ row }">
              <el-tooltip
                v-if="row.graham_summary"
                :content="`达标 ${row.graham_summary.passed} / 不达标 ${row.graham_summary.failed} / 不可判定 ${row.graham_summary.indeterminate}（数据年度 ${row.graham_summary.as_of_year}，详情见标的档案）`"
              >
                <el-tag :type="grahamTagType(row.graham_summary)" size="small" effect="plain">
                  达标 {{ row.graham_summary.passed }}/{{ row.graham_summary.total }}
                </el-tag>
              </el-tooltip>
              <el-tooltip v-else content="生成 AI 分析或同步档案后可见准则判定">
                <span class="muted">未同步档案</span>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column label="加入日期" width="110">
            <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="130" fixed="right">
            <template #default="{ row }">
              <el-button type="primary" size="small" text @click="openEditDialog(row)"
                >编辑</el-button
              >
              <el-button type="danger" size="small" text @click="removeItem(row)">移除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div v-else v-loading="loading" class="mobile-card-list">
        <article
          v-for="row in items"
          :key="row.id"
          class="mobile-card"
          data-testid="watchlist-card"
        >
          <div class="mobile-card-head">
            <button
              type="button"
              class="mobile-card-title mobile-card-title-link"
              @click="openSecurityDetail(row)"
            >
              <span class="mobile-card-symbol">
                {{ row.symbol }}
                <el-icon class="mobile-card-title-chevron"><ArrowRight /></el-icon>
              </span>
              <span v-if="row.name" class="mobile-card-name">{{ row.name }}</span>
            </button>
            <div class="mobile-card-tags">
              <el-tag size="small" effect="plain">{{ row.market }}</el-tag>
              <el-tag
                v-if="row.graham_summary"
                :type="grahamTagType(row.graham_summary)"
                size="small"
                effect="plain"
              >
                达标 {{ row.graham_summary.passed }}/{{ row.graham_summary.total }}
              </el-tag>
            </div>
          </div>
          <div v-if="row.note" class="mobile-card-meta">
            <span>{{ row.note }}</span>
          </div>
          <div class="mobile-card-meta">
            <span v-if="row.current_price != null">
              当前价 {{ formatNumber(row.current_price, 4) }}
            </span>
            <span>加入 {{ formatDate(row.created_at) }}</span>
          </div>
          <div class="mobile-card-actions">
            <el-button type="primary" size="small" text @click="openEditDialog(row)"
              >编辑</el-button
            >
            <el-button type="danger" size="small" text @click="removeItem(row)">移除</el-button>
          </div>
        </article>
        <el-empty
          v-if="!loading && items.length === 0"
          description="暂无观察标的"
          :image-size="88"
        />
      </div>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      :title="editingId === null ? '添加观察标的' : '编辑观察标的'"
      width="480px"
    >
      <el-form :model="form" :rules="rules" ref="formRef" label-width="90px">
        <el-form-item label="股票代码" prop="symbol">
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
            placeholder="为什么观察：一句话理由/买入条件（正式论点可在标的档案页记录）"
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
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, type FormInstance } from 'element-plus'
import { ArrowRight, Plus } from '@element-plus/icons-vue'
import api from '../api'
import SecuritySelect from '../components/SecuritySelect.vue'
import { useMediaQuery } from '../composables/useMediaQuery'
import { getApiErrorMessage } from '../utils/apiErrors'
import { formatDate, formatDateTime, formatNumber } from '../utils/helpers'
import {
  MARKETS,
  freeTextFormPatch,
  resolvedFormPatch,
  securityFormPatch
} from '../utils/securities'
import type { SecurityResolveResponse, SecuritySearchItem, WatchlistItem } from '../types'

const router = useRouter()
const isMobileView = useMediaQuery('(max-width: 640px)')
const loading = ref(false)
const items = ref<WatchlistItem[]>([])
const dialogVisible = ref(false)
const submitting = ref(false)
const editingId = ref<number | null>(null)
const formRef = ref<FormInstance | null>(null)

const form = reactive({ symbol: '', market: '', name: '', note: '' })

const rules = {
  symbol: [{ required: true, message: '请输入股票代码', trigger: 'blur' }],
  market: [{ required: true, message: '请选择市场', trigger: 'change' }]
}

// 准则摘要 tag 配色：不达标为主 → warning；有达标且无不达标 → success；
// 全部不可判定 → info（数据边界，不是负面信号）
function grahamTagType(summary: Record<string, unknown>) {
  const passed = Number(summary.passed || 0)
  const failed = Number(summary.failed || 0)
  if (failed > passed) return 'warning'
  if (passed > 0) return 'success'
  return 'info'
}

function openSecurityDetail(row: WatchlistItem) {
  router.push(`/securities/${encodeURIComponent(row.market)}/${encodeURIComponent(row.symbol)}`)
}

async function loadWatchlist() {
  loading.value = true
  try {
    const response = await api.getWatchlist()
    items.value = response.data
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '加载观察清单失败'))
  } finally {
    loading.value = false
  }
}

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
      ElMessage.success('已更新')
    }
    dialogVisible.value = false
    await loadWatchlist()
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, editingId.value === null ? '添加失败' : '更新失败'))
  } finally {
    submitting.value = false
  }
}

function removeItem(row: WatchlistItem) {
  ElMessageBox.confirm(
    `确定把 ${row.symbol} 移出观察清单吗？标的档案与已生成的分析不受影响。`,
    '移出观察',
    { confirmButtonText: '确定', cancelButtonText: '取消', type: 'warning' }
  ).then(async () => {
    try {
      await api.removeWatchlistItem(row.id)
      ElMessage.success('已移出观察清单')
      await loadWatchlist()
    } catch (error) {
      ElMessage.error(getApiErrorMessage(error, '移除失败'))
    }
  })
}

onMounted(loadWatchlist)
</script>

<style scoped>
.watchlist-page {
  width: 100%;
}

.watchlist-card {
  overflow: hidden;
}

.watchlist-note {
  margin-bottom: 16px;
}

.price-updated-at {
  font-size: 12px;
  color: var(--app-text-soft);
}

.muted {
  color: var(--app-text-soft);
  font-size: 12px;
}

@media (max-width: 900px) {
  .header-actions {
    width: 100%;
  }
}
</style>
