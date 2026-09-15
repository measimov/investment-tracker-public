<script setup lang="ts">
import { ref, reactive, watch } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import { getApiErrorMessage } from '@/utils/apiErrors'
import type { BrokerAccount, DividendSuggestion } from '@/types'
import { formatNumber, formatDate, toNumber } from '@/utils/helpers'
import { pollJobUntilDone } from '@/utils/polling'
import { useAliveGuard } from '@/composables/useAliveGuard'
import {
  brokerAccountLabel,
  brokerAccountLabelById as labelById,
  getActionTypeName,
  getActionTypeTag
} from './shared'

// 后端 schema 为准（此前手写副本把 status 枚举放宽为 string）
type SuggestionRow = DividendSuggestion

const props = defineProps<{ brokerAccounts: BrokerAccount[]; active: boolean }>()

// counts-changed：待处理徽标挂在壳层 tab 标签上，由壳层重取；
// accepted：接受入账产生了正式公司行动记录，壳层要刷新记录 tab
const emit = defineEmits<{ 'counts-changed': []; accepted: [] }>()

const suggestions = ref<SuggestionRow[]>([])
const suggestionsLoading = ref(false)
const suggestionStatusFilter = ref('')
const { isUnmounted } = useAliveGuard()
const syncing = ref(false)
const accepting = ref(false)

const acceptDialog = reactive<{
  visible: boolean
  row: SuggestionRow | null
  brokerAccountId: number | null
  totalDividend: number | null
  taxWithheld: number | null
}>({ visible: false, row: null, brokerAccountId: null, totalDividend: null, taxWithheld: null })

function brokerAccountLabelById(accountId: number | null | undefined) {
  return labelById(props.brokerAccounts, accountId)
}

function suggestionStatusLabel(status: string) {
  return (
    (
      { NEW: '新建议', MATCHED: '已匹配', ACCEPTED: '已入账', IGNORED: '已忽略' } as Record<
        string,
        string
      >
    )[status] || status
  )
}

function suggestionStatusTag(status: string) {
  if (status === 'NEW') return 'primary'
  if (status === 'ACCEPTED') return 'success'
  if (status === 'IGNORED') return 'info'
  return 'info'
}

async function loadSuggestions() {
  suggestionsLoading.value = true
  try {
    const params: Record<string, unknown> = { limit: 200 }
    if (suggestionStatusFilter.value) params.status = suggestionStatusFilter.value
    const response = await api.listDividendSuggestions(params)
    suggestions.value = response.data
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '加载分红建议失败'))
  } finally {
    suggestionsLoading.value = false
  }
}

async function syncDividends() {
  syncing.value = true
  try {
    const startResponse = await api.startDividendSyncJob()
    const job = await pollJobUntilDone(() => api.getDividendSyncJob(startResponse.data.id), {
      intervalMs: 2000,
      maxAttempts: 150,
      isCancelled: isUnmounted,
      failureMessage: '分红公告同步失败'
    })
    // 取消/卸载即收手：pollJobUntilDone 被 isCancelled 中止时返回 null，
    // 若继续落到下面的列表刷新，会产生卸载后的请求与状态写入，失败时还会
    // 在别的页面弹迟到错误（PR #171 复审）。
    if (!job || isUnmounted()) return
    const result = (job.result || {}) as Record<string, number | unknown[]>
    const failed = Array.isArray(result.failed) ? result.failed.length : 0
    ElMessage.success(
      `同步完成：扫描 ${result.symbols_scanned ?? 0} 只标的，新建议 ${result.new ?? 0} 条、` +
        `已匹配 ${result.matched ?? 0} 条、事件 ${result.events_upserted ?? 0} 条` +
        (failed ? `；${failed} 只标的失败` : '')
    )
    await loadSuggestions()
    emit('counts-changed')
  } catch (error) {
    if (!isUnmounted()) ElMessage.error(getApiErrorMessage(error, '分红公告同步失败'))
  } finally {
    if (!isUnmounted()) syncing.value = false
  }
}

function openAcceptDialog(row: SuggestionRow) {
  acceptDialog.row = row
  // 默认取建议行自身的账户归属（每账户一条建议）；NULL=未指定/合并口径
  acceptDialog.brokerAccountId = row.broker_account_id ?? null
  acceptDialog.totalDividend =
    row.estimated_total_dividend != null ? toNumber(row.estimated_total_dividend) : null
  acceptDialog.taxWithheld = 0
  acceptDialog.visible = true
}

async function submitAccept() {
  if (!acceptDialog.row) return
  accepting.value = true
  try {
    // 始终显式发送 broker_account_id（含 null）：省略该键时后端会沿用建议
    // 原账户，用户"清空账户"的意图会静默丢失。el-select 清空把 model 置为
    // undefined，而 undefined 在 JSON 序列化时被丢键，必须归一化为 null。
    const payload: Record<string, unknown> = {
      broker_account_id:
        typeof acceptDialog.brokerAccountId === 'number' ? acceptDialog.brokerAccountId : null
    }
    if (acceptDialog.row.action_type === 'CASH_DIVIDEND') {
      if (acceptDialog.totalDividend != null) payload.total_dividend = acceptDialog.totalDividend
      if (acceptDialog.taxWithheld != null) payload.tax_withheld = acceptDialog.taxWithheld
    }
    await api.acceptDividendSuggestion(acceptDialog.row.id, payload)
    ElMessage.success('已入账为正式公司行动记录')
    acceptDialog.visible = false
    await loadSuggestions()
    emit('counts-changed')
    emit('accepted')
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '接受建议失败'))
    // 后端可能已在拒绝时把建议转为 MATCHED（迟到入账重判重）：刷新列表
    // 反映真实状态；若该行已不再是 NEW，关闭弹窗防止对旧状态重试。
    const failedId = acceptDialog.row?.id
    await loadSuggestions()
    emit('counts-changed')
    const fresh = suggestions.value.find((row) => row.id === failedId)
    if (!fresh || fresh.status !== 'NEW') acceptDialog.visible = false
  } finally {
    accepting.value = false
  }
}

async function ignoreSuggestion(row: SuggestionRow) {
  try {
    await api.ignoreDividendSuggestion(row.id)
    await loadSuggestions()
    emit('counts-changed')
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '忽略建议失败'))
  }
}

async function restoreSuggestion(row: SuggestionRow) {
  try {
    await api.restoreDividendSuggestion(row.id)
    await loadSuggestions()
    emit('counts-changed')
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '恢复建议失败'))
  }
}

// 首次切到本 tab 才加载（v-show 下组件常驻，卸载即整页离开）
watch(
  () => props.active,
  (active) => {
    if (active && !suggestions.value.length) loadSuggestions()
  }
)
</script>

<template>
  <el-card>
    <template #header>
      <div class="page-header">
        <span>分红公告建议</span>
        <div class="header-actions">
          <el-select
            v-model="suggestionStatusFilter"
            class="suggestion-status-filter"
            @change="loadSuggestions"
          >
            <el-option label="待处理（新建议+已匹配）" value="" />
            <el-option label="仅新建议" value="NEW" />
            <el-option label="仅已匹配" value="MATCHED" />
            <el-option label="已接受" value="ACCEPTED" />
            <el-option label="已忽略" value="IGNORED" />
          </el-select>
          <el-button
            type="primary"
            :loading="syncing"
            data-testid="dividend-sync-button"
            @click="syncDividends"
          >
            同步 A 股分红公告
          </el-button>
        </div>
      </div>
    </template>

    <el-alert
      title="仅同步 A/B 股公告（Tushare）；港股/美股分红仍以券商对账单导入为准。建议不会自动入账——点“接受”才会创建正式公司行动记录。"
      type="info"
      :closable="false"
      show-icon
      class="suggestions-note"
    />

    <el-table v-loading="suggestionsLoading" :data="suggestions" stripe>
      <el-table-column label="代码/名称" min-width="130">
        <template #default="{ row }">
          <span class="suggestion-symbol">{{ row.symbol }}</span>
          <span v-if="row.name" class="suggestion-name">{{ row.name }}</span>
        </template>
      </el-table-column>
      <el-table-column label="类型" width="100">
        <template #default="{ row }">
          <el-tag :type="getActionTypeTag(row.action_type)" size="small">
            {{ getActionTypeName(row.action_type) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="账户" min-width="110" show-overflow-tooltip>
        <template #default="{ row }">
          <span :class="{ 'account-unassigned': !row.broker_account_id }">
            {{
              row.broker_account_id
                ? brokerAccountLabelById(row.broker_account_id)
                : row.action_type === 'STOCK_DIVIDEND'
                  ? '全部账户'
                  : '未指定'
            }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="除权日" width="110">
        <template #default="{ row }">{{ formatDate(row.ex_date) }}</template>
      </el-table-column>
      <el-table-column label="派息日" width="110">
        <template #default="{ row }">{{ row.pay_date ? formatDate(row.pay_date) : '—' }}</template>
      </el-table-column>
      <el-table-column label="每股税前(税后)" min-width="130" align="right">
        <template #default="{ row }">
          <template v-if="row.action_type === 'CASH_DIVIDEND'">
            {{ formatNumber(toNumber(row.cash_div_pre_tax), 4) }}
            <span v-if="row.cash_div_after_tax" class="after-tax">
              ({{ formatNumber(toNumber(row.cash_div_after_tax), 4) }})
            </span>
          </template>
          <template v-else
            >每股送转 {{ formatNumber(toNumber(row.stk_div_per_share), 4) }}</template
          >
        </template>
      </el-table-column>
      <el-table-column label="登记日持仓" min-width="110" align="right">
        <template #default="{ row }">
          <span>{{ formatNumber(toNumber(row.record_date_quantity), 0) }}</span>
          <el-tooltip
            v-if="row.quantity_basis === 'merged'"
            content="账户归属存在矛盾，按合并口径推算（数量总和可信）"
          >
            <el-tag type="warning" size="small" effect="plain">合并</el-tag>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="推算总额(税前)" min-width="120" align="right">
        <template #default="{ row }">
          {{
            row.estimated_total_dividend != null
              ? formatNumber(toNumber(row.estimated_total_dividend), 2)
              : '—'
          }}
        </template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tooltip
            v-if="row.match_detail && row.match_detail.amount_diff != null"
            :content="`已按日期匹配到账本记录，但金额差 ${formatNumber(row.match_detail.amount_diff, 2)}，请核对`"
          >
            <el-tag type="warning" size="small">已匹配·金额差</el-tag>
          </el-tooltip>
          <el-tag v-else :type="suggestionStatusTag(row.status)" size="small">
            {{ suggestionStatusLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="180" fixed="right">
        <template #default="{ row }">
          <!-- 仅 NEW 可接受：MATCHED 已有账本记录，再入账即双计（后端同样拒绝） -->
          <template v-if="row.status === 'NEW'">
            <el-button type="primary" size="small" text @click="openAcceptDialog(row)">
              接受
            </el-button>
            <el-button type="info" size="small" text @click="ignoreSuggestion(row)">
              忽略
            </el-button>
          </template>
          <template v-else-if="row.status === 'MATCHED'">
            <el-tooltip content="账本已有匹配记录，无需入账；如有出入请先核对既有记录">
              <span class="accepted-hint">已在账</span>
            </el-tooltip>
            <el-button type="info" size="small" text @click="ignoreSuggestion(row)">
              忽略
            </el-button>
          </template>
          <el-button
            v-else-if="row.status === 'IGNORED'"
            type="primary"
            size="small"
            text
            @click="restoreSuggestion(row)"
          >
            恢复
          </el-button>
          <span v-else class="accepted-hint">已入账</span>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无分红建议；点击右上角同步公告" :image-size="88" />
      </template>
    </el-table>

    <!-- 接受建议：账户归属与税额可改 -->
    <el-dialog v-model="acceptDialog.visible" title="接受分红建议" width="480px">
      <el-form label-width="110px">
        <el-form-item label="标的">
          <span>
            {{ acceptDialog.row?.symbol }} {{ acceptDialog.row?.name || '' }} （{{
              getActionTypeName(acceptDialog.row?.action_type || '')
            }}）
          </span>
        </el-form-item>
        <el-form-item label="券商账户">
          <el-select
            v-model="acceptDialog.brokerAccountId"
            placeholder="可选；按实际到账账户归属"
            clearable
          >
            <el-option
              v-for="account in brokerAccounts"
              :key="account.id"
              :label="brokerAccountLabel(account)"
              :value="account.id"
            />
          </el-select>
        </el-form-item>
        <template v-if="acceptDialog.row?.action_type === 'CASH_DIVIDEND'">
          <el-form-item label="股息总额">
            <el-input-number
              v-model="acceptDialog.totalDividend"
              :min="0"
              :precision="2"
              :controls="false"
              class="amount-input"
            />
          </el-form-item>
          <el-form-item label="预扣税额">
            <el-input-number
              v-model="acceptDialog.taxWithheld"
              :min="0"
              :precision="2"
              :controls="false"
              class="amount-input"
            />
            <div class="field-hint">A 股券商到账通常为税前全额，税额保持 0 即可</div>
          </el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="acceptDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="accepting" @click="submitAccept">确认入账</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.suggestion-status-filter {
  width: 200px;
  margin-right: 10px;
}

.suggestions-note {
  margin-bottom: 14px;
}

.suggestion-symbol {
  font-weight: 600;
  margin-right: 6px;
}

.suggestion-name {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.after-tax {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.amount-input {
  width: 100%;
}

.field-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.4;
}

.accepted-hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.account-unassigned {
  color: var(--app-warning);
}

@media (max-width: 900px) {
  .header-actions {
    width: 100%;
  }
}
</style>
