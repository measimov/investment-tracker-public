<script setup lang="ts">
import { showApiError } from '@/utils/showApiError'
import { ref, reactive, watch } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import type { BrokerAccount, DividendSuggestion } from '@/types'
import { formatNumber, formatDate, formatQuantity, toNumber } from '@/utils/helpers'
import { pollJobUntilDone } from '@/utils/polling'
import {
  accountLabel,
  accountOptionLabel,
  actionTypeLabel,
  actionTypeTag,
  UNASSIGNED_ACCOUNT_LABEL
} from '@/utils/labels'
import { useAliveGuard } from '@/composables/useAliveGuard'
import { hkDividendNotes, suggestionSourceLabel, type HkAnnouncementDetail } from './shared'

// 后端 schema 为准（此前手写副本把 status 枚举放宽为 string）
type SuggestionRow = DividendSuggestion

const props = defineProps<{ brokerAccounts: BrokerAccount[]; active: boolean }>()

// counts-changed：待处理徽标挂在壳层 tab 标签上，由壳层重取；
// accepted：接受入账产生了正式公司行动记录，壳层要刷新记录 tab
const emit = defineEmits<{ 'counts-changed': []; accepted: [] }>()

const suggestions = ref<SuggestionRow[]>([])
const suggestionsLoading = ref(false)
// 默认只看「新建议」：与 tab 徽标（NEW 计数）同一口径——已匹配的建议账本里已有记录，
// 不需要处理；需要核对时切到「新建议+已匹配」或「仅已匹配」
const suggestionStatusFilter = ref('NEW')
const SUGGESTION_LIMIT = 200
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
  return accountLabel(props.brokerAccounts, accountId)
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

function hkNotes(row: SuggestionRow): string[] {
  return hkDividendNotes(row.announcement_detail as HkAnnouncementDetail | null | undefined)
}

function isHkRow(row: SuggestionRow | null | undefined): boolean {
  return row?.source === 'hkexnews-dividend'
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
    const params: Record<string, unknown> = { limit: SUGGESTION_LIMIT }
    if (suggestionStatusFilter.value) params.status = suggestionStatusFilter.value
    const response = await api.listDividendSuggestions(params)
    suggestions.value = response.data
  } catch (error) {
    showApiError(error, '加载分红建议失败')
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
    const result = (job.result || {}) as Record<string, unknown>
    const count = (value: unknown) => (Array.isArray(value) ? value.length : 0)
    const failed = count(result.failed)
    const unparsed = count(result.hk_unparsed_forms)
    const pending = count(result.hk_pending)
    const blocked = count(result.hk_blocked)
    const skippedNoTushare = Number(result.skipped_no_tushare || 0)
    const warnings = [
      failed ? `${failed} 只标的失败` : '',
      unparsed ? `${unparsed} 份港股公告未能识别` : '',
      blocked ? `${blocked} 处港股股息因最新公告无法识别而暂停更新（沿用现有建议，未改写）` : '',
      skippedNoTushare ? `未配置 TUSHARE_TOKEN，跳过 ${skippedNoTushare} 只 A/B 股` : ''
    ].filter(Boolean)
    const notes = [...warnings, pending ? `${pending} 笔港股股息金额/除净日有待公布` : ''].filter(
      Boolean
    )
    const message =
      `同步完成：扫描 ${result.symbols_scanned ?? 0} 只标的，新建议 ${result.new ?? 0} 条、` +
      `已匹配 ${result.matched ?? 0} 条、事件 ${result.events_upserted ?? 0} 条` +
      (notes.length ? `；${notes.join('；')}` : '')
    if (warnings.length) ElMessage.warning(message)
    else ElMessage.success(message)
    await loadSuggestions()
    emit('counts-changed')
  } catch (error) {
    if (!isUnmounted()) showApiError(error, '分红公告同步失败')
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
    showApiError(error, '接受建议失败')
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
    showApiError(error, '忽略建议失败')
  }
}

async function restoreSuggestion(row: SuggestionRow) {
  try {
    await api.restoreDividendSuggestion(row.id)
    await loadSuggestions()
    emit('counts-changed')
  } catch (error) {
    showApiError(error, '恢复建议失败')
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
            <el-option label="待处理（新建议）" value="NEW" />
            <el-option label="新建议+已匹配" value="" />
            <el-option label="仅已匹配" value="MATCHED" />
            <el-option label="已入账" value="ACCEPTED" />
            <el-option label="已忽略" value="IGNORED" />
          </el-select>
          <el-button
            type="primary"
            :loading="syncing"
            data-testid="dividend-sync-button"
            @click="syncDividends"
          >
            同步分红公告
          </el-button>
        </div>
      </div>
    </template>

    <el-alert
      title="A/B 股同步 Tushare 分红公告（需配置 TUSHARE_TOKEN），港股同步披露易「现金股息公告」表格；美股分红仍以券商对账单导入为准。建议不会自动入账——点“接受”才会创建正式公司行动记录。"
      type="info"
      :closable="false"
      show-icon
      class="suggestions-note"
    />
    <el-alert
      v-if="suggestions.length >= SUGGESTION_LIMIT"
      type="info"
      :closable="false"
      show-icon
      class="suggestions-note"
      :title="`仅显示最近 ${SUGGESTION_LIMIT} 条建议（按除权日倒序）`"
    />

    <div class="responsive-table">
      <el-table v-loading="suggestionsLoading" :data="suggestions" stripe>
        <el-table-column label="代码/名称" min-width="130">
          <template #default="{ row }">
            <span class="suggestion-symbol">{{ row.symbol }}</span>
            <span v-if="row.name" class="suggestion-name">{{ row.name }}</span>
            <el-tag
              v-if="isHkRow(row)"
              size="small"
              effect="plain"
              class="suggestion-source"
              data-testid="suggestion-source-hkex"
            >
              {{ suggestionSourceLabel(row.source) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="100">
          <template #default="{ row }">
            <el-tag :type="actionTypeTag(row.action_type)" size="small">
              {{ actionTypeLabel(row.action_type) }}
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
                    : UNASSIGNED_ACCOUNT_LABEL
              }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="除权日" width="110">
          <template #default="{ row }">{{ formatDate(row.ex_date) }}</template>
        </el-table-column>
        <el-table-column label="派息日" width="110">
          <template #default="{ row }">{{
            row.pay_date ? formatDate(row.pay_date) : '—'
          }}</template>
        </el-table-column>
        <el-table-column label="每股税前(税后)" min-width="130" align="right">
          <template #default="{ row }">
            <template v-if="row.action_type === 'CASH_DIVIDEND'">
              {{ formatNumber(toNumber(row.cash_div_pre_tax), 4) }}
              <span v-if="row.currency !== 'CNY'" class="suggestion-currency">{{
                row.currency
              }}</span>
              <span v-if="row.cash_div_after_tax" class="after-tax">
                ({{ formatNumber(toNumber(row.cash_div_after_tax), 4) }})
              </span>
              <el-tooltip v-if="hkNotes(row).length" placement="top">
                <template #content>
                  <div v-for="(line, index) in hkNotes(row)" :key="index">{{ line }}</div>
                </template>
                <el-tag size="small" type="info" effect="plain" class="suggestion-detail-tag">
                  公告
                </el-tag>
              </el-tooltip>
            </template>
            <template v-else>每股送转 {{ formatQuantity(row.stk_div_per_share) }}</template>
          </template>
        </el-table-column>
        <el-table-column label="登记日持仓" min-width="110" align="right">
          <template #default="{ row }">
            <span>{{ formatQuantity(row.record_date_quantity) }}</span>
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
            <span
              v-if="row.estimated_total_dividend != null && row.currency !== 'CNY'"
              class="suggestion-currency"
              >{{ row.currency }}</span
            >
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
            <el-tooltip
              v-else-if="row.match_detail && row.match_detail.currency_mismatch"
              :content="`已按日期匹配到账本记录（账本以 ${row.match_detail.currency_mismatch.recorded_currency || '其他币种'} 入账，与公告 ${row.currency} 币种不同，未比较金额）`"
            >
              <el-tag type="info" size="small">已匹配·币种不同</el-tag>
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
    </div>

    <!-- 接受建议：账户归属与税额可改 -->
    <el-dialog v-model="acceptDialog.visible" title="接受分红建议" width="min(480px, 94vw)">
      <el-form label-width="110px">
        <el-form-item label="标的">
          <span>
            {{ acceptDialog.row?.symbol }} {{ acceptDialog.row?.name || '' }} （{{
              actionTypeLabel(acceptDialog.row?.action_type || '')
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
              :label="accountOptionLabel(account)"
              :value="account.id"
            />
          </el-select>
        </el-form-item>
        <template v-if="acceptDialog.row?.action_type === 'CASH_DIVIDEND'">
          <el-form-item v-if="acceptDialog.row?.currency !== 'CNY'" label="币种">
            <span>{{ acceptDialog.row?.currency }}</span>
          </el-form-item>
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
            <div v-if="isHkRow(acceptDialog.row)" class="field-hint">
              港股按公告派发币种与除净日前一天持仓推算税前总额；预扣税视持有渠道而定（H 股/红筹经
              HKSCC 代理人常按 10%、港股通个人 20%），请按券商实际到账填写
            </div>
            <div v-else class="field-hint">A 股券商到账通常为税前全额，税额保持 0 即可</div>
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

.suggestion-source {
  margin-left: 6px;
}

.suggestion-currency {
  margin-left: 3px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.suggestion-detail-tag {
  margin-left: 4px;
  cursor: help;
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
    flex-wrap: wrap;
  }
}

/* 移动端：筛选下拉与同步按钮各占一行，表格在容器内横向滚动，不撑破视口 */
@media (max-width: 640px) {
  .suggestion-status-filter {
    width: 100%;
    margin-right: 0;
    margin-bottom: 8px;
  }

  .header-actions > .el-button {
    width: 100%;
  }
}
</style>
