<template>
  <div class="holdings-page">
    <el-card class="holdings-card">
      <template #header>
        <div class="page-header">
          <span>当前持仓</span>
          <div class="header-actions">
            <el-button type="success" :icon="Refresh" @click="refreshPrices" :loading="refreshing">
              一键刷新股价
            </el-button>
            <el-tooltip :disabled="analyzableCount > 0" content="当前持仓没有 A股/美股/港股 标的">
              <span>
                <el-button
                  type="primary"
                  :icon="MagicStick"
                  :loading="batchStarting"
                  :disabled="analyzableCount === 0 || isBatchActive || isDigestActive"
                  data-testid="analyze-all-button"
                  @click="analyzeAllHoldings"
                >
                  一键分析所有持仓
                </el-button>
              </span>
            </el-tooltip>
            <el-tooltip :disabled="analyzableCount > 0" content="当前持仓没有 A股/美股/港股 标的">
              <span>
                <el-button
                  :icon="Notebook"
                  :loading="digestStarting"
                  :disabled="analyzableCount === 0 || isBatchActive || isDigestActive"
                  data-testid="digest-backfill-button"
                  @click="backfillAllDigests"
                >
                  补齐财报摘要
                </el-button>
              </span>
            </el-tooltip>
            <el-select
              v-model="selectedAccount"
              placeholder="选择账户"
              clearable
              class="market-select"
            >
              <el-option label="全部账户" :value="''" />
              <el-option label="未指定账户" value="unassigned" />
              <el-option
                v-for="account in brokerAccounts"
                :key="account.id"
                :label="account.account_name"
                :value="account.id"
              />
            </el-select>
            <el-select
              v-model="selectedMarket"
              placeholder="选择市场"
              clearable
              @change="loadHoldings"
              class="market-select"
            >
              <el-option label="全部市场" value="" />
              <el-option label="A股" value="A股" />
              <el-option label="B股" value="B股" />
              <el-option label="港股" value="港股" />
              <el-option label="美股" value="美股" />
              <el-option label="新加坡股" value="新加坡股" />
              <el-option label="加密货币" value="加密货币" />
            </el-select>
          </div>
        </div>
      </template>

      <div v-if="batchJob" class="job-progress" data-testid="batch-analysis-progress">
        <div class="job-progress-header">
          <span>{{ batchStatusText }}</span>
          <span>
            {{ batchJob.completed || 0 }}/{{ batchJob.total || 0 }}
            <template v-if="batchJob.current_symbol">
              · {{ batchJob.current_symbol }} {{ batchJob.current_market }}
              <template v-if="batchJob.current_stage">（{{ batchJob.current_stage }}）</template>
            </template>
          </span>
        </div>
        <el-progress :percentage="batchPercent" :status="batchProgressStatus" :stroke-width="10" />
        <div class="job-progress-detail">
          成功 {{ batchJob.success_count || 0 }} · 跳过 {{ batchJob.skipped_count || 0 }} · 失败
          {{ batchJob.failed_count || 0 }}
          <template v-if="batchEtaText">· 预计剩余 {{ batchEtaText }}</template>
        </div>
        <div v-if="recentBatchResults.length" class="batch-recent">
          <span
            v-for="item in recentBatchResults"
            :key="`${item.symbol}:${item.market}`"
            :class="{ 'batch-recent-failed': item.status === 'failed' }"
          >
            {{ item.symbol }} {{ batchResultLabel(item) }}
          </span>
        </div>
        <div v-if="batchJob.abort_reason" class="job-progress-error">
          {{ batchJob.abort_reason }}
        </div>
        <div v-if="isBatchActive" class="job-progress-actions">
          <el-button
            size="small"
            type="danger"
            text
            data-testid="cancel-batch-button"
            @click="cancelBatchAnalysis"
          >
            终止任务
          </el-button>
          <el-button size="small" text data-testid="stop-watching-batch" @click="stopWatchingBatch">
            停止查看进度
          </el-button>
          <span class="batch-hint">
            「停止查看」只停止本页轮询，后台任务会继续运行并继续消耗
            token；重新进入持仓页可继续查看。
          </span>
        </div>
      </div>

      <div v-if="digestJob" class="job-progress" data-testid="digest-backfill-progress">
        <div class="job-progress-header">
          <span>{{ digestStatusText }}</span>
          <span>
            {{ digestJob.completed || 0 }}/{{ digestJob.total || 0 }}
            <template v-if="digestJob.current_symbol">
              · {{ digestJob.current_symbol }} {{ digestJob.current_market }}
            </template>
          </span>
        </div>
        <el-progress
          :percentage="digestPercent"
          :status="digestProgressStatus"
          :stroke-width="10"
        />
        <div class="job-progress-detail">
          已生成摘要 {{ digestJob.digests_generated || 0 }} 份 · 标的成功
          {{ digestJob.success_count || 0 }} · 失败 {{ digestJob.failed_count || 0 }}
          <template v-if="digestEtaText">· 预计剩余 {{ digestEtaText }}</template>
        </div>
        <div v-if="digestJob.abort_reason" class="job-progress-error">
          {{ digestJob.abort_reason }}
        </div>
        <div v-if="isDigestActive" class="job-progress-actions">
          <el-button
            size="small"
            type="danger"
            text
            data-testid="cancel-digest-backfill"
            @click="cancelDigestBackfill"
          >
            终止任务
          </el-button>
          <el-button size="small" text @click="stopWatchingDigest">停止查看进度</el-button>
          <span class="batch-hint">
            「停止查看」只停止本页轮询，后台任务会继续运行；重新进入持仓页可继续查看。
          </span>
        </div>
      </div>

      <div v-if="!isMobileView" class="responsive-table desktop-data-table">
        <el-table :data="visibleHoldings" v-loading="loading" stripe>
          <template #empty>
            <el-empty description="暂无持仓数据" :image-size="88" />
          </template>
          <el-table-column prop="symbol" label="代码" min-width="90" />
          <el-table-column label="名称" min-width="150">
            <template #default="{ row }">
              <el-link
                type="primary"
                :underline="false"
                class="holding-name"
                @click="openSecurityDetail(row)"
              >
                {{ row.name }}
              </el-link>
              <el-tooltip v-if="upcomingEvent(row)" :content="eventTooltip(row)" placement="top">
                <el-tag
                  type="warning"
                  size="small"
                  effect="plain"
                  class="event-badge"
                  data-testid="security-event-badge"
                >
                  {{ upcomingEvent(row)!.label }}·{{ upcomingEvent(row)!.daysText }}
                </el-tag>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column prop="market" label="市场" width="80" />
          <el-table-column label="账户" min-width="110" show-overflow-tooltip>
            <template #default="{ row }">
              <span :class="{ 'account-unassigned': !row.broker_account_id }">
                {{ accountLabel(row.broker_account_id) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="quantity" label="持仓数量" min-width="120" align="right">
            <template #default="{ row }">
              {{ formatNumber(row.quantity, 4) }}
            </template>
          </el-table-column>
          <el-table-column prop="avg_cost" label="平均成本" min-width="105" align="right">
            <template #default="{ row }">
              {{ formatNumber(row.avg_cost, 4) }}
            </template>
          </el-table-column>
          <el-table-column prop="total_cost" label="总成本" min-width="135" align="right">
            <template #default="{ row }">
              <span class="accent-strong">
                {{ formatCurrency(row.total_cost, row.currency) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="当前价格" width="150" align="right">
            <template #default="{ row }">
              <!-- 表格行内不放 +/- 步进钮：132px 下会把输入框压碎（截图实锤） -->
              <el-input-number
                v-model="currentPrices[priceKey(row)]"
                :min="0"
                :precision="4"
                size="small"
                :controls="false"
                @change="savePriceToDatabase(row)"
              />
            </template>
          </el-table-column>
          <el-table-column label="当前市值" min-width="130" align="right">
            <template #default="{ row }">
              <span v-if="currentPrices[priceKey(row)]" style="font-weight: bold">
                {{
                  formatCurrency(
                    (currentPrices[priceKey(row)] ?? 0) * toNumber(row.quantity),
                    row.currency
                  )
                }}
              </span>
              <span v-else style="color: var(--app-text-soft)">-</span>
            </template>
          </el-table-column>
          <el-table-column label="浮动盈亏" min-width="115" align="right">
            <template #default="{ row }">
              <span
                v-if="currentPrices[priceKey(row)]"
                :style="{ fontWeight: 'bold', color: getProfitColor(row) }"
              >
                {{ formatCurrency(calculateProfitAmount(row), row.currency) }}
              </span>
              <span v-else style="color: var(--app-text-soft)">-</span>
            </template>
          </el-table-column>
          <el-table-column label="收益率" min-width="90" align="right">
            <template #default="{ row }">
              <span
                v-if="currentPrices[priceKey(row)]"
                :style="{ fontWeight: 'bold', color: getProfitColor(row) }"
              >
                {{ formatPercent(calculateProfitRate(row)) }}
              </span>
              <span v-else style="color: var(--app-text-soft)">-</span>
            </template>
          </el-table-column>
          <el-table-column label="AI 标签" min-width="130">
            <template #default="{ row }">
              <template v-if="analysisFor(row)">
                <el-tooltip :content="analysisFor(row)!.summary">
                  <span class="ai-tags" data-testid="ai-tags">
                    <el-tag
                      :type="riskTagType(analysisFor(row)!.risk_level)"
                      size="small"
                      effect="plain"
                    >
                      {{
                        RISK_LABELS[analysisFor(row)!.risk_level] || analysisFor(row)!.risk_level
                      }}
                    </el-tag>
                    <el-tag
                      v-for="tag in analysisFor(row)!.tags.slice(0, 2)"
                      :key="tag"
                      size="small"
                      effect="plain"
                      type="warning"
                    >
                      {{ tag }}
                    </el-tag>
                  </span>
                </el-tooltip>
              </template>
              <span v-else class="ai-untagged">未分析</span>
            </template>
          </el-table-column>
          <el-table-column prop="currency" label="币种" width="70" />
          <el-table-column prop="updated_at" label="更新时间" min-width="150">
            <template #default="{ row }">
              {{ formatDateTime(row.updated_at) }}
            </template>
          </el-table-column>
          <el-table-column label="操作" width="90" fixed="right">
            <template #default="{ row }">
              <el-button type="primary" size="small" text @click="openTransferDialog(row)">
                转仓
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div v-else v-loading="loading" class="mobile-card-list">
        <article
          v-for="row in visibleHoldings"
          :key="`${row.market}-${row.symbol}-${row.broker_account_id ?? 'null'}`"
          class="mobile-card"
          data-testid="holding-card"
        >
          <div class="mobile-card-head">
            <!-- 移动端此前是纯 span：桌面端标题是 el-link 可进标的档案，手机上
                 整页没有任何入口，AI 分析/财报摘要在手机上根本打不开 -->
            <button
              type="button"
              class="mobile-card-title mobile-card-title-link"
              data-testid="holding-card-title"
              :aria-label="`查看 ${row.name || row.symbol} 的标的档案`"
              @click="openSecurityDetail(row)"
            >
              <span class="mobile-card-symbol" data-testid="holding-card-symbol">
                {{ row.symbol }}
                <el-icon class="mobile-card-title-chevron"><ArrowRight /></el-icon>
              </span>
              <span v-if="row.name" class="mobile-card-name">{{ row.name }}</span>
            </button>
            <div class="mobile-card-tags">
              <el-tag size="small" effect="plain">{{ row.market }}</el-tag>
              <el-tag size="small" type="info" effect="plain">
                {{ accountLabel(row.broker_account_id) }}
              </el-tag>
              <el-tag v-if="upcomingEvent(row)" type="warning" size="small" effect="plain">
                {{ upcomingEvent(row)!.label }}·{{ upcomingEvent(row)!.daysText }}
              </el-tag>
              <!-- AI 标签：移动端只取 1 个（标签行已有市场/账户/事件三个 tag）。
                   没有这块，手机上发起批量分析后回到本页看不到任何结果 -->
              <template v-if="analysisFor(row)">
                <el-tag
                  :type="riskTagType(analysisFor(row)!.risk_level)"
                  size="small"
                  effect="plain"
                  data-testid="ai-tags"
                >
                  {{ RISK_LABELS[analysisFor(row)!.risk_level] || analysisFor(row)!.risk_level }}
                </el-tag>
                <el-tag v-if="analysisFor(row)!.tags[0]" size="small" effect="plain" type="warning">
                  {{ analysisFor(row)!.tags[0] }}
                </el-tag>
              </template>
            </div>
          </div>

          <div class="mobile-card-metrics">
            <div>
              <span class="mobile-metric-label">总成本</span>
              <strong>{{ formatCurrency(row.total_cost, row.currency) }}</strong>
            </div>
            <div>
              <span class="mobile-metric-label">当前市值</span>
              <strong v-if="currentPrices[priceKey(row)]">
                {{
                  formatCurrency(
                    (currentPrices[priceKey(row)] ?? 0) * toNumber(row.quantity),
                    row.currency
                  )
                }}
              </strong>
              <strong v-else>-</strong>
            </div>
            <div>
              <span class="mobile-metric-label">浮动盈亏</span>
              <strong v-if="currentPrices[priceKey(row)]" :style="{ color: getProfitColor(row) }">
                {{ formatCurrency(calculateProfitAmount(row), row.currency) }}
              </strong>
              <strong v-else>-</strong>
            </div>
            <div>
              <span class="mobile-metric-label">收益率</span>
              <strong v-if="currentPrices[priceKey(row)]" :style="{ color: getProfitColor(row) }">
                {{ formatPercent(calculateProfitRate(row)) }}
              </strong>
              <strong v-else>-</strong>
            </div>
          </div>

          <div class="mobile-card-meta">
            <span>数量 {{ formatNumber(row.quantity, 4) }}</span>
            <span>成本 {{ formatNumber(row.avg_cost, 4) }}</span>
            <span>{{ row.currency }}</span>
          </div>

          <div class="mobile-price-row">
            <span>当前价格</span>
            <el-input-number
              v-model="currentPrices[priceKey(row)]"
              :min="0"
              :precision="4"
              size="small"
              @change="savePriceToDatabase(row)"
            />
          </div>

          <div class="mobile-card-actions">
            <el-button type="primary" size="small" text @click="openTransferDialog(row)">
              转仓到其他账户
            </el-button>
          </div>
        </article>
        <el-empty
          v-if="!loading && visibleHoldings.length === 0"
          description="暂无持仓数据"
          :image-size="88"
        />
      </div>

      <!-- Summary -->
      <el-divider />
      <div class="summary-section">
        <el-alert
          v-if="unpricedCount > 0"
          type="info"
          :closable="false"
          show-icon
          class="summary-alert"
          :title="`${unpricedCount} 只持仓暂无价格，已同时从总成本与总市值中剔除（口径自洽）`"
        />
        <el-row :gutter="20">
          <el-col :xs="24" :sm="12" :lg="6">
            <div class="summary-item summary-cost">
              <div class="summary-label">总成本</div>
              <div class="summary-value">{{ formatCurrency(totalCostCNY) }}</div>
              <div class="summary-sub-value">{{ formatCurrency(totalCostUSD, 'USD') }}</div>
            </div>
          </el-col>
          <el-col :xs="24" :sm="12" :lg="6">
            <div class="summary-item summary-market">
              <div class="summary-label">总市值</div>
              <div class="summary-value">{{ formatCurrency(totalMarketValueCNY) }}</div>
              <div class="summary-sub-value">{{ formatCurrency(totalMarketValueUSD, 'USD') }}</div>
            </div>
          </el-col>
          <el-col :xs="24" :sm="12" :lg="6">
            <div class="summary-item summary-profit">
              <div class="summary-label">
                总盈亏（摊薄成本口径）
                <el-tooltip
                  placement="top"
                  content="按持仓摊薄平均成本计算：卖出不改变剩余持仓均价。统计分析页的“当前持仓表现”按 FIFO 剩余批次成本计算，部分卖出过的证券两者会有差异；两种口径下“已实现+未实现”的总收益一致，只是拆分归属不同。"
                >
                  <el-icon class="label-help"><QuestionFilled /></el-icon>
                </el-tooltip>
              </div>
              <div class="summary-value" :style="{ color: profitColor(totalProfit) }">
                {{ formatCurrency(totalProfit) }}
              </div>
              <div class="summary-sub-value" :style="{ color: profitColor(totalProfit) }">
                {{ formatCurrency(convertToUSD(totalProfit), 'USD') }}
              </div>
            </div>
          </el-col>
          <el-col :xs="24" :sm="12" :lg="6">
            <div class="summary-item summary-rate">
              <div class="summary-label">总收益率</div>
              <div class="summary-value" :style="{ color: profitColor(totalProfitRate) }">
                {{ formatPercent(totalProfitRate) }}
              </div>
            </div>
          </el-col>
        </el-row>
      </div>
    </el-card>

    <el-dialog v-model="transferDialogVisible" title="账户间转仓" width="420px">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="转仓把持仓从一个账户移到另一个，成本基础跟随迁移，不产生盈亏或现金流。"
        style="margin-bottom: 16px"
      />
      <el-form v-if="transferForm" label-position="top">
        <el-form-item label="证券">
          <el-input :model-value="`${transferForm.symbol}（${transferForm.market}）`" disabled />
        </el-form-item>
        <el-form-item label="转出账户">
          <el-input :model-value="accountLabel(transferForm.from_broker_account_id)" disabled />
        </el-form-item>
        <el-form-item label="转入账户" required>
          <el-select v-model="transferForm.to_broker_account_id" placeholder="请选择转入账户">
            <el-option
              v-if="transferForm.from_broker_account_id !== null"
              label="未指定账户"
              value="unassigned"
            />
            <el-option
              v-for="account in transferTargetAccounts"
              :key="account.id"
              :label="account.account_name"
              :value="account.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="`数量（可转 ${transferForm.max_quantity}）`" required>
          <el-input-number
            v-model="transferForm.quantity"
            :min="0.00000001"
            :max="transferForm.max_quantity"
            :precision="8"
          />
        </el-form-item>
        <el-form-item label="转仓日期" required>
          <el-date-picker
            v-model="transferForm.transfer_date"
            type="date"
            value-format="YYYY-MM-DD"
          />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="transferForm.notes" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="transferDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="transferring" @click="submitTransfer">
          确认转仓
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowRight, MagicStick, Notebook, QuestionFilled, Refresh } from '@element-plus/icons-vue'
import api from '../api'
import { useHoldingsStore, type Holding } from '../stores/holdings'
import { useTransactionsStore } from '../stores/transactions'
import { useExchangeRates } from '../composables/useExchangeRates'
import { useMediaQuery } from '../composables/useMediaQuery'
import { getApiErrorMessage } from '../utils/apiErrors'
import {
  formatNumber,
  formatCurrency,
  formatDateTime,
  todayLocalISODate,
  profitColor,
  formatPercent,
  toNumber
} from '../utils/helpers'
import { pollJobUntilDone } from '../utils/polling'
import { isApiError } from '../utils/apiErrors'

interface BrokerAccount {
  id: number
  account_name: string
  [key: string]: unknown
}

interface TransferForm {
  symbol: string
  market: string
  from_broker_account_id: number | null | undefined
  to_broker_account_id: number | 'unassigned' | null | undefined
  quantity: number
  max_quantity: number
  transfer_date: string
  notes: string
}

interface PriceRefreshResult {
  success_count: number
  skipped_count: number
  failed_count: number
  failed_list?: Array<{ symbol: string; market: string; error?: string }>
  success_list?: Array<{ symbol: string; market: string; price?: number; source?: string }>
}

const loading = ref(false)
const refreshing = ref(false)
const holdingsStore = useHoldingsStore()
const transactionsStore = useTransactionsStore()
const holdings = ref<Holding[]>([])
const selectedMarket = ref('')
const selectedAccount = ref<'' | 'unassigned' | number>('')
const brokerAccounts = ref<BrokerAccount[]>([])
const transferDialogVisible = ref(false)
const transferring = ref(false)
const transferForm = ref<TransferForm | null>(null)
const currentPrices = reactive<Record<string, number | null>>({})
const { loadExchangeRates, convertToCNY, convertToUSD } = useExchangeRates()
const isMobileView = useMediaQuery('(max-width: 640px)')

const visibleHoldings = computed(() => {
  if (selectedAccount.value === '' || selectedAccount.value === undefined) return holdings.value
  if (selectedAccount.value === 'unassigned') {
    return holdings.value.filter((h) => h.broker_account_id === null)
  }
  return holdings.value.filter((h) => h.broker_account_id === selectedAccount.value)
})

const transferTargetAccounts = computed(() => {
  const form = transferForm.value
  if (!form) return brokerAccounts.value
  return brokerAccounts.value.filter((account) => account.id !== form.from_broker_account_id)
})

function accountLabel(accountId: number | null | undefined) {
  if (accountId === null || accountId === undefined) return '未指定'
  const account = brokerAccounts.value.find((item) => item.id === accountId)
  return account ? account.account_name : `账户#${accountId}`
}

async function loadBrokerAccounts() {
  try {
    const response = await api.getBrokerAccounts({ limit: 1000 })
    brokerAccounts.value = response.data
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '加载券商账户失败'))
  }
}

function openTransferDialog(row: Holding) {
  transferForm.value = {
    symbol: row.symbol,
    market: row.market,
    from_broker_account_id: row.broker_account_id,
    // undefined = 尚未选择；null 仅在用户点选"未指定账户"(sentinel) 后映射产生
    to_broker_account_id: undefined,
    quantity: toNumber(row.quantity),
    max_quantity: toNumber(row.quantity),
    transfer_date: todayLocalISODate(),
    notes: ''
  }
  transferDialogVisible.value = true
}

async function submitTransfer() {
  const form = transferForm.value
  if (!form) return
  if (form.to_broker_account_id === undefined) {
    ElMessage.warning('请选择转入账户')
    return
  }
  const targetAccountId =
    form.to_broker_account_id === 'unassigned' ? null : form.to_broker_account_id
  if (targetAccountId === form.from_broker_account_id) {
    ElMessage.warning('请选择不同的转入账户')
    return
  }
  if (!form.quantity || form.quantity <= 0) {
    ElMessage.warning('请输入有效的转仓数量')
    return
  }
  transferring.value = true
  try {
    await transactionsStore.createTransfer({
      symbol: form.symbol,
      market: form.market,
      quantity: form.quantity,
      from_broker_account_id: form.from_broker_account_id,
      to_broker_account_id: targetAccountId,
      transfer_date: form.transfer_date,
      notes: form.notes || null
    })
    ElMessage.success('转仓成功')
    transferDialogVisible.value = false
    await loadHoldings({ force: true })
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '转仓失败'))
  } finally {
    transferring.value = false
  }
}

// 价格键与统计页一致：symbol:market（同代码跨市场不串价）
function priceKey(row: Holding) {
  return `${row.symbol}:${row.market}`
}

// 汇总口径：无可用价格的持仓行在成本与市值两侧同时剔除（口径自洽），
// 单独计数提示，而不是"计成本不计市值"把总盈亏虚减。
const pricedHoldings = computed(() =>
  visibleHoldings.value.filter((h) => (currentPrices[priceKey(h)] ?? 0) > 0)
)

const unpricedCount = computed(() => visibleHoldings.value.length - pricedHoldings.value.length)

const totalCostCNY = computed(() => {
  return pricedHoldings.value.reduce((sum, h) => {
    const costCNY = convertToCNY(toNumber(h.total_cost), h.currency)
    return sum + costCNY
  }, 0)
})

const totalCostUSD = computed(() => {
  return convertToUSD(totalCostCNY.value)
})

const totalCost = computed(() => totalCostCNY.value)

const totalMarketValueCNY = computed(() => {
  return pricedHoldings.value.reduce((sum, h) => {
    const marketValue = (currentPrices[priceKey(h)] ?? 0) * toNumber(h.quantity)
    const marketValueCNY = convertToCNY(marketValue, h.currency)
    return sum + marketValueCNY
  }, 0)
})

const totalMarketValueUSD = computed(() => {
  return convertToUSD(totalMarketValueCNY.value)
})

const totalMarketValue = computed(() => totalMarketValueCNY.value)

const totalProfit = computed(() => {
  return totalMarketValue.value - totalCost.value
})

const totalProfitRate = computed(() => {
  if (totalCost.value === 0) return 0
  return (totalProfit.value / totalCost.value) * 100
})

async function loadHoldings(options: { force?: boolean } = {}) {
  loading.value = true
  try {
    const params: Record<string, unknown> = {}
    if (selectedMarket.value) params.market = selectedMarket.value

    holdings.value = await holdingsStore.fetchHoldings(params, {
      force: options?.force === true
    })

    // Initialize current prices from persisted market prices only.
    holdings.value.forEach((h) => {
      if (h.current_price && toNumber(h.current_price) > 0) {
        currentPrices[priceKey(h)] = toNumber(h.current_price)
      } else {
        currentPrices[priceKey(h)] = null
      }
    })
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '加载持仓数据失败'))
  } finally {
    loading.value = false
  }
}

async function savePriceToDatabase(row: Holding) {
  try {
    const price = currentPrices[priceKey(row)]
    if (!price || price <= 0) return

    await holdingsStore.updateHoldingPrice(row.id, price)
    // Silent success - don't show message for every input
  } catch (error) {
    ElMessage.error(`保存${row.symbol}价格失败: ${getApiErrorMessage(error)}`)
  }
}

async function refreshPrices() {
  refreshing.value = true

  const loadingMsg = ElMessage({
    message: '股价刷新已提交，正在后台处理...',
    type: 'info',
    duration: 0,
    showClose: true
  })

  try {
    const response = await holdingsStore.refreshAllPrices()
    const job = response.data
    const result = await pollPriceRefreshJob(job.id)

    loadingMsg.close()

    await loadHoldings({ force: true })

    let message = `成功更新 ${result.success_count} 只股票`

    if (result.skipped_count > 0) {
      message += `，跳过 ${result.skipped_count} 只（最近已更新）`
    }

    if (result.failed_count > 0) {
      message += `，${result.failed_count} 只更新失败`
    }

    if (result.failed_count === 0) {
      ElMessage.success(message)
    } else if (result.success_count > 0) {
      ElMessage.warning(message)
    } else {
      ElMessage.error('刷新失败，请稍后重试')
    }

    if (import.meta.env.DEV && result.failed_list && result.failed_list.length > 0) {
      console.group('📊 刷新失败详情')
      result.failed_list.forEach((item) => {
        console.error(`${item.symbol} (${item.market}): ${item.error}`)
      })
      console.groupEnd()
    }

    if (import.meta.env.DEV && result.success_list && result.success_list.length > 0) {
      console.group('📈 刷新成功详情')
      result.success_list.forEach((item) => {
        console.log(`${item.symbol} (${item.market}): ${item.price} [${item.source}]`)
      })
      console.groupEnd()
    }
  } catch (error) {
    loadingMsg.close()

    let errorMessage = '刷新股价失败'

    if (isApiError(error) && error.response?.data?.detail) {
      errorMessage = error.response.data.detail
    } else if (error instanceof Error && error.message) {
      errorMessage = error.message
    }

    ElMessage.error(errorMessage)
    console.error('Refresh error:', error)
  } finally {
    refreshing.value = false
  }
}

async function pollPriceRefreshJob(jobId: number | string): Promise<PriceRefreshResult> {
  const job = await pollJobUntilDone(() => api.getPriceRefreshJob(jobId), {
    timeoutMessage: '刷新仍在后台运行，请稍后重新查看持仓价格',
    failureMessage: '后台刷新失败'
  })
  // pollJobUntilDone 仅在显式取消时返回 null，此路径未启用取消
  return (job?.result ?? null) as PriceRefreshResult
}

function calculateProfitAmount(row: Holding) {
  const currentPrice = currentPrices[priceKey(row)]
  if (!currentPrice) return 0
  const marketValue = currentPrice * toNumber(row.quantity)
  return marketValue - toNumber(row.total_cost)
}

function calculateProfitRate(row: Holding) {
  const profitAmount = calculateProfitAmount(row)
  const totalCost = toNumber(row.total_cost)
  if (totalCost === 0) return 0
  return (profitAmount / totalCost) * 100
}

function getProfitColor(row: Holding) {
  return profitColor(calculateProfitAmount(row))
}

onMounted(async () => {
  isUnmounted = false
  await Promise.all([loadExchangeRates(), loadHoldings(), loadBrokerAccounts()])
  loadSecurityEvents()
  loadSecurityAnalyses()
  loadBatchTargetCount()
  attachToActiveBatchJob() // 刷新页面/切走再回来时接上进行中的批量任务
})

onUnmounted(() => {
  isUnmounted = true
})

// ---------------------------------------------------------------------------
// AI 标的分析摘要（持仓列表标签列；点击名称跳转详情页）
// ---------------------------------------------------------------------------

interface AnalysisSummaryRow {
  symbol: string
  market: string
  tags: string[]
  risk_level: string
  summary: string
}

const router = useRouter()
const RISK_LABELS: Record<string, string> = { low: '低', medium: '中', high: '高' }
const securityAnalyses = ref<Map<string, AnalysisSummaryRow>>(new Map())

function riskTagType(level: string) {
  if (level === 'high') return 'danger'
  if (level === 'medium') return 'warning'
  return 'success'
}

function analysisFor(row: { symbol: string; market: string }): AnalysisSummaryRow | null {
  return securityAnalyses.value.get(`${row.symbol}:${row.market}`) || null
}

async function loadSecurityAnalyses() {
  try {
    const response = await api.listSecurityAnalyses()
    const map = new Map<string, AnalysisSummaryRow>()
    for (const row of response.data as AnalysisSummaryRow[]) {
      map.set(`${row.symbol}:${row.market}`, row)
    }
    securityAnalyses.value = map
  } catch {
    // 标签列失败静默：不打断持仓主流程
  }
}

// ---------------------------------------------------------------------------
// 批量分析（一键分析所有持仓）
//
// 后端是串行长任务（数十分钟到数小时），前端只负责启动、轮询展示与终止。
// 三种语义严格区分，文案不得混用：
//   离开页面   → 任务继续，回来自动恢复（无 UI）
//   停止查看   → 只停本页轮询，token 照烧
//   终止任务   → 真正中止（后端在标的边界收尾）
// ---------------------------------------------------------------------------

// 与后端 security_profile_service.SUPPORTED_MARKETS 对齐；其余市场后端 409
const ANALYZABLE_MARKETS = new Set(['A股', '美股', '港股'])
// 纯 UI 量级提示：单标的实测 1~3 分钟（同步基本面 + 已有摘要 + LLM 生成）
const MINUTES_PER_SYMBOL_LOW = 1
const MINUTES_PER_SYMBOL_HIGH = 3
const BATCH_POLL_INTERVAL_MS = 5000
// 先定墙钟上限（6 小时）再反推次数；超时只是停止本页轮询，任务仍在后台
const BATCH_POLL_MAX_ATTEMPTS = 4320

interface BatchResultRow {
  symbol: string
  market: string
  status: string
  error?: string | null
  reason?: string | null
}

interface AnalysisBatchJob {
  id?: string
  status?: string
  total?: number
  completed?: number
  progress_percent?: number | string | null
  success_count?: number
  failed_count?: number
  skipped_count?: number
  current_symbol?: string | null
  current_market?: string | null
  current_stage?: string | null
  results?: BatchResultRow[]
  abort_reason?: string | null
  cancelled?: boolean
  started_at?: string | null
  [key: string]: unknown
}

const batchJob = ref<AnalysisBatchJob | null>(null)
const batchStarting = ref(false)
const batchWatchStopped = ref(false)
let isUnmounted = false

// 目标数以后端预览为准：后端还会排除已清仓、EXCLUDE 与 CASH_MANAGEMENT 标的，
// 只按支持市场在本地算会虚高（持有货币基金时尤其明显），启动后 job.total 又
// 突然变小。
//
// 三态而不是"count 为 null 就回退"：null 既表示"还没取到"也表示"取失败了"，
// 混在一起会让预览**在途期间**也用本地数——用户此时点按钮就会看到错误的目标数。
// 因此确认框前必须等预览定案，只有**确定失败**才回退本地估算。
type BatchTargetsState = 'idle' | 'loading' | 'ready' | 'failed'

const batchTargetCount = ref<number | null>(null)
const batchTargetsState = ref<BatchTargetsState>('idle')
let batchTargetsPromise: Promise<void> | null = null

const localAnalyzableCount = computed(
  () =>
    new Set(
      holdings.value
        .filter((row) => ANALYZABLE_MARKETS.has(row.market))
        .map((row) => `${row.symbol}:${row.market}`)
    ).size
)

// 按钮的启用判据：预览在途/失败时用本地估算保持可点（宁可点开后再告知
// "没有可分析标的"，也不要把按钮错误地禁掉）。确认框里的数字另走 resolve。
const analyzableCount = computed(() =>
  batchTargetsState.value === 'ready' && batchTargetCount.value !== null
    ? batchTargetCount.value
    : localAnalyzableCount.value
)

function loadBatchTargetCount(force = false): Promise<void> {
  if (batchTargetsPromise && !force) return batchTargetsPromise
  batchTargetsState.value = 'loading'
  batchTargetsPromise = (async () => {
    try {
      const response = await api.getSecurityAnalysisBatchTargets()
      if (isUnmounted) return
      batchTargetCount.value = Number(response.data?.total ?? 0)
      batchTargetsState.value = 'ready'
    } catch {
      if (isUnmounted) return
      batchTargetsState.value = 'failed' // 确定失败后才允许回退本地估算
    }
  })()
  return batchTargetsPromise
}

/** 确认框用的目标数：等预览定案；只有确定失败才退回本地估算。 */
async function resolveBatchTargetCount(): Promise<number> {
  if (batchTargetsState.value !== 'ready') await loadBatchTargetCount()
  if (batchTargetsState.value === 'ready' && batchTargetCount.value !== null) {
    return batchTargetCount.value
  }
  return localAnalyzableCount.value
}

const isBatchActive = computed(
  () => batchJob.value?.status === 'queued' || batchJob.value?.status === 'running'
)

const batchPercent = computed(() =>
  Math.max(0, Math.min(100, Math.round(Number(batchJob.value?.progress_percent || 0))))
)

const batchProgressStatus = computed(() => {
  const status = batchJob.value?.status
  if (status === 'failed') return 'exception'
  if (status === 'interrupted') return 'warning'
  if (status === 'succeeded') return 'success'
  return undefined
})

const BATCH_STATUS_LABELS: Record<string, string> = {
  queued: '批量分析排队中',
  running: '批量分析进行中',
  succeeded: '批量分析完成',
  failed: '批量分析失败',
  interrupted: '批量分析已终止'
}

const batchStatusText = computed(
  () => BATCH_STATUS_LABELS[String(batchJob.value?.status || '')] || '批量分析进行中'
)

const recentBatchResults = computed<BatchResultRow[]>(() =>
  [...(batchJob.value?.results || [])].slice(-5).reverse()
)

function batchResultLabel(item: BatchResultRow): string {
  if (item.status === 'succeeded') return '已分析'
  if (item.status === 'skipped') return '已跳过'
  return '失败'
}

// 剩余时间估计：数小时的任务没有 ETA，用户读不出"还要多久"与"是不是卡死了"
const batchEtaText = computed(() => {
  const job = batchJob.value
  if (!job || !isBatchActive.value) return ''
  const completed = Number(job.completed || 0)
  const total = Number(job.total || 0)
  if (completed < 2 || total <= completed || !job.started_at) return ''
  const elapsedMs = Date.now() - Date.parse(job.started_at)
  if (!Number.isFinite(elapsedMs) || elapsedMs <= 0) return ''
  const remainingMinutes = Math.round(((elapsedMs / completed) * (total - completed)) / 60000)
  if (remainingMinutes < 1) return '不到 1 分钟'
  if (remainingMinutes < 90) return `约 ${remainingMinutes} 分钟`
  return `约 ${(remainingMinutes / 60).toFixed(1)} 小时`
})

function batchEstimateText(count: number): string {
  const low = count * MINUTES_PER_SYMBOL_LOW
  const high = count * MINUTES_PER_SYMBOL_HIGH
  if (high <= 90) return `${low}~${high} 分钟`
  return `约 ${(low / 60).toFixed(1)}~${(high / 60).toFixed(1)} 小时`
}

async function analyzeAllHoldings() {
  batchStarting.value = true
  // 先等目标预览定案，确认框里的数量/耗时/token 预期不能用在途的本地估算
  const targetCount = await resolveBatchTargetCount()
  if (isUnmounted) return
  if (targetCount === 0) {
    batchStarting.value = false
    ElMessage.info('当前没有可分析的持仓标的（A股/美股/港股，且不含已排除与现金管理标的）')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将对 ${targetCount} 只持仓标的（A股/美股/港股）逐个同步基本面并调用 LLM 生成分析，` +
        `预计耗时 ${batchEstimateText(targetCount)}，会消耗较多 LLM token。\n` +
        `24 小时内已分析过的标的会自动跳过；任务在后台运行，关闭页面不会中断。`,
      '一键分析所有持仓',
      { type: 'warning', confirmButtonText: '开始分析', cancelButtonText: '取消' }
    )
  } catch {
    batchStarting.value = false
    return // 用户取消
  }

  batchWatchStopped.value = false
  try {
    const response = await api.startSecurityAnalysisBatchJob()
    if (isUnmounted) return
    batchJob.value = response.data as AnalysisBatchJob
    // 启动完成即收起 loading：轮询要跑数十分钟到数小时，让按钮一直转圈既无
    // 信息量（进度块已经在显示了），也与"活跃时只禁用"的设计不符
    batchStarting.value = false
    await watchBatchJob(response.data.id)
  } catch (error) {
    if (isUnmounted) return
    ElMessage.error(getApiErrorMessage(error, '批量分析启动失败'))
  } finally {
    if (!isUnmounted) batchStarting.value = false
  }
}

async function watchBatchJob(jobId: string) {
  try {
    const job = await pollJobUntilDone(() => api.getSecurityAnalysisBatchJob(jobId), {
      intervalMs: BATCH_POLL_INTERVAL_MS,
      maxAttempts: BATCH_POLL_MAX_ATTEMPTS,
      isCancelled: () => isUnmounted || batchWatchStopped.value,
      onUpdate: (job) => {
        if (isUnmounted || batchWatchStopped.value) return
        const previous = Number(batchJob.value?.completed || 0)
        batchJob.value = job as AnalysisBatchJob
        // 每完成一只就刷一次标签列：标签一格一格亮起来是最好的进度反馈
        if (Number(job.completed || 0) > previous) loadSecurityAnalyses()
      },
      timeoutMessage: '批量分析仍在后台运行；重新进入持仓页可继续查看进度',
      failureMessage: '批量分析失败'
    })
    if (!job || isUnmounted) return
    await loadSecurityAnalyses()
    loadBatchTargetCount(true) // 新鲜度窗口变了，下次的目标数随之变化
    const success = Number(job.success_count || 0)
    const skipped = Number(job.skipped_count || 0)
    const failed = Number(job.failed_count || 0)
    const summary =
      `批量分析完成：成功 ${success} 只` +
      (skipped ? `，跳过 ${skipped} 只` : '') +
      (failed ? `，失败 ${failed} 只` : '')
    if (!failed) ElMessage.success(summary)
    else if (success > 0) ElMessage.warning(summary)
    else ElMessage.error('批量分析全部失败，请检查 LLM 配置后重试')
  } catch (error) {
    if (isUnmounted || batchWatchStopped.value) return
    // 终止是用户主动行为，不当作错误弹红
    if (batchJob.value?.cancelled) {
      await loadSecurityAnalyses()
      ElMessage.info('批量分析已终止，已生成的分析已保留')
      return
    }
    ElMessage.error(getApiErrorMessage(error, '批量分析失败'))
  }
}

async function cancelBatchAnalysis() {
  const jobId = batchJob.value?.id
  if (!jobId) return
  try {
    await ElMessageBox.confirm(
      '已生成的分析会保留，未开始的标的不再分析。当前正在分析的标的会先跑完再停止。',
      '终止批量分析',
      { type: 'warning', confirmButtonText: '终止', cancelButtonText: '继续运行' }
    )
  } catch {
    return
  }
  try {
    await api.cancelSecurityAnalysisBatchJob(jobId)
    if (!isUnmounted) ElMessage.info('已请求终止，当前标的完成后停止')
  } catch (error) {
    if (!isUnmounted) ElMessage.error(getApiErrorMessage(error, '终止失败'))
  }
}

function stopWatchingBatch() {
  batchWatchStopped.value = true
  batchJob.value = null
}

// ---------------------------------------------------------------------------
// 批量财报摘要回填（商业画像与"财报要点"的原料；可重复触发续跑加深至十年）
// ---------------------------------------------------------------------------

interface DigestBatchJob {
  id: string
  type?: string
  status: string
  total?: number
  completed?: number
  progress_percent?: number
  success_count?: number
  failed_count?: number
  digests_generated?: number
  digests_blocked?: number
  symbols_with_remaining?: number
  current_symbol?: string | null
  current_market?: string | null
  cancelled?: boolean
  abort_reason?: string | null
  started_at?: string | null
  [key: string]: unknown
}

const digestJob = ref<DigestBatchJob | null>(null)
const digestStarting = ref(false)
const digestWatchStopped = ref(false)

const isDigestActive = computed(
  () => digestJob.value?.status === 'queued' || digestJob.value?.status === 'running'
)

const digestPercent = computed(() =>
  Math.max(0, Math.min(100, Math.round(Number(digestJob.value?.progress_percent || 0))))
)

const digestProgressStatus = computed(() => {
  const status = digestJob.value?.status
  if (status === 'failed') return 'exception'
  if (status === 'interrupted') return 'warning'
  if (status === 'succeeded') return 'success'
  return undefined
})

const DIGEST_STATUS_LABELS: Record<string, string> = {
  queued: '财报摘要回填排队中',
  running: '财报摘要回填进行中',
  succeeded: '财报摘要回填完成',
  failed: '财报摘要回填失败',
  interrupted: '财报摘要回填已终止'
}

const digestStatusText = computed(
  () => DIGEST_STATUS_LABELS[String(digestJob.value?.status || '')] || '财报摘要回填进行中'
)

const digestEtaText = computed(() => {
  const job = digestJob.value
  if (!job || !isDigestActive.value) return ''
  const completed = Number(job.completed || 0)
  const total = Number(job.total || 0)
  if (completed < 2 || total <= completed || !job.started_at) return ''
  const elapsedMs = Date.now() - Date.parse(job.started_at)
  if (!Number.isFinite(elapsedMs) || elapsedMs <= 0) return ''
  const remainingMinutes = Math.round(((elapsedMs / completed) * (total - completed)) / 60000)
  if (remainingMinutes < 1) return '不到 1 分钟'
  if (remainingMinutes < 90) return `约 ${remainingMinutes} 分钟`
  return `约 ${(remainingMinutes / 60).toFixed(1)} 小时`
})

async function backfillAllDigests() {
  digestStarting.value = true
  // 预览是纯 DB 统计（不打外网），失败时如实说取不到，不用本地估算凑数——
  // 本地根本不知道每个标的已有几份摘要
  let preview: { targets_total: number; targets_without_digest: number; per_symbol_budget: number }
  try {
    preview = (await api.getDigestBackfillPreview()).data
  } catch (error) {
    if (!isUnmounted) {
      digestStarting.value = false
      ElMessage.error(getApiErrorMessage(error, '获取回填预览失败'))
    }
    return
  }
  if (isUnmounted) return
  if (!preview.targets_total) {
    digestStarting.value = false
    ElMessage.info('当前没有可回填的持仓标的（A股/美股/港股）')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将为 ${preview.targets_total} 只持仓标的下载财报原文并生成 AI 摘要` +
        `（其中 ${preview.targets_without_digest} 只目前一份摘要都没有）。\n` +
        `每只本轮最多补 ${preview.per_symbol_budget} 份（新→旧）；已有的期数自动跳过，` +
        `再次点击本按钮可继续向更早年份加深，直至十年补满。\n` +
        `预计每只 2-8 分钟（下载与解析 PDF 为主），任务在后台运行，关闭页面不会中断。`,
      '补齐财报摘要',
      { type: 'warning', confirmButtonText: '开始回填', cancelButtonText: '取消' }
    )
  } catch {
    digestStarting.value = false
    return // 用户取消
  }

  digestWatchStopped.value = false
  try {
    const response = await api.startDigestBackfillJob()
    if (isUnmounted) return
    digestJob.value = response.data as DigestBatchJob
    digestStarting.value = false
    await watchDigestJob(response.data.id)
  } catch (error) {
    if (isUnmounted) return
    ElMessage.error(getApiErrorMessage(error, '批量回填启动失败'))
  } finally {
    if (!isUnmounted) digestStarting.value = false
  }
}

async function watchDigestJob(jobId: string) {
  try {
    const job = await pollJobUntilDone(() => api.getDigestBackfillJob(jobId), {
      intervalMs: BATCH_POLL_INTERVAL_MS,
      maxAttempts: BATCH_POLL_MAX_ATTEMPTS,
      isCancelled: () => isUnmounted || digestWatchStopped.value,
      onUpdate: (job) => {
        if (isUnmounted || digestWatchStopped.value) return
        digestJob.value = job as DigestBatchJob
      },
      timeoutMessage: '回填仍在后台运行；重新进入持仓页可继续查看进度',
      failureMessage: '批量回填失败'
    })
    if (!job || isUnmounted) return
    const generated = Number(job.digests_generated || 0)
    const blocked = Number(job.digests_blocked || 0)
    const remaining = Number(job.symbols_with_remaining || 0)
    const failed = Number(job.failed_count || 0)
    let summary = `财报摘要回填完成：新生成 ${generated} 份`
    if (remaining) summary += `；${remaining} 只标的还有更早年份可补，再次点击可继续加深`
    if (blocked) summary += `；${blocked} 份报告已永久失败（多次重试仍无法下载或摘要）`
    if (failed) summary += `；${failed} 只标的失败`
    // 有永久失败也不能弹绿：绿色 + "新生成 0 份" 会让用户以为一切正常
    if (!failed && !blocked) ElMessage.success(summary)
    else ElMessage.warning(summary)
  } catch (error) {
    if (isUnmounted || digestWatchStopped.value) return
    if (digestJob.value?.cancelled) {
      ElMessage.info('回填已终止，已生成的摘要已保留，可再次触发续跑')
      return
    }
    ElMessage.error(getApiErrorMessage(error, '批量回填失败'))
  }
}

async function cancelDigestBackfill() {
  const jobId = digestJob.value?.id
  if (!jobId) return
  try {
    await ElMessageBox.confirm(
      '已生成的摘要会保留，未开始的标的不再处理。当前标的会先跑完再停止。',
      '终止财报摘要回填',
      { type: 'warning', confirmButtonText: '终止', cancelButtonText: '继续运行' }
    )
  } catch {
    return
  }
  try {
    await api.cancelDigestBackfillJob(jobId)
    if (!isUnmounted) ElMessage.info('已请求终止，当前标的完成后停止')
  } catch (error) {
    if (!isUnmounted) ElMessage.error(getApiErrorMessage(error, '终止失败'))
  }
}

function stopWatchingDigest() {
  digestWatchStopped.value = true
  digestJob.value = null
}

async function attachToActiveBatchJob() {
  try {
    const response = await api.listActiveAnalysisJobs()
    const jobs = (response.data || []) as AnalysisBatchJob[]
    const analysis = jobs.find((job) => job.type === 'security_analysis_batch')
    if (analysis?.id && !isUnmounted) {
      batchWatchStopped.value = false
      batchJob.value = analysis
      await watchBatchJob(analysis.id)
      return
    }
    const digest = jobs.find((job) => job.type === 'report_digest_batch')
    if (digest?.id && !isUnmounted) {
      digestWatchStopped.value = false
      digestJob.value = digest as DigestBatchJob
      await watchDigestJob(digest.id)
    }
  } catch {
    // 恢复失败静默：不打断持仓主流程
  }
}

function openSecurityDetail(row: { symbol: string; market: string }) {
  router.push(`/securities/${encodeURIComponent(row.market)}/${encodeURIComponent(row.symbol)}`)
}

// ---------------------------------------------------------------------------
// 标的事件角标（未来 90 天：财报披露 / 分红预案 / 限售解禁）
// ---------------------------------------------------------------------------

interface SecurityEventRow {
  symbol: string
  market: string
  event_type: string
  event_date: string
}

const EVENT_TYPE_LABELS: Record<string, string> = {
  EARNINGS_DISCLOSURE: '财报披露',
  DIVIDEND_PLAN: '分红预案',
  SHARE_UNLOCK: '限售解禁'
}

const securityEvents = ref<Map<string, SecurityEventRow[]>>(new Map())

async function loadSecurityEvents() {
  try {
    const response = await api.getSecurityEvents({ days_ahead: 90 })
    const map = new Map<string, SecurityEventRow[]>()
    for (const event of response.data as SecurityEventRow[]) {
      const key = `${event.symbol}:${event.market}`
      const list = map.get(key) || []
      list.push(event)
      map.set(key, list)
    }
    securityEvents.value = map
  } catch {
    // 事件角标失败静默：不打断持仓主流程
  }
}

function eventsFor(row: { symbol: string; market: string }): SecurityEventRow[] {
  return securityEvents.value.get(`${row.symbol}:${row.market}`) || []
}

function upcomingEvent(row: { symbol: string; market: string }) {
  const today = new Date().toISOString().slice(0, 10)
  const upcoming = eventsFor(row).filter((event) => event.event_date >= today)
  if (!upcoming.length) return null
  const nearest = upcoming[0]
  const days = Math.round(
    (new Date(nearest.event_date).getTime() - new Date(today).getTime()) / 86400000
  )
  return {
    label: EVENT_TYPE_LABELS[nearest.event_type] || nearest.event_type,
    daysText: days === 0 ? '今天' : `${days}天后`,
    date: nearest.event_date
  }
}

function eventTooltip(row: { symbol: string; market: string }): string {
  const today = new Date().toISOString().slice(0, 10)
  return eventsFor(row)
    .filter((event) => event.event_date >= today)
    .map(
      (event) => `${event.event_date} ${EVENT_TYPE_LABELS[event.event_type] || event.event_type}`
    )
    .join('；')
}
</script>

<style scoped>
/* 批量进度块的通用外观走 styles.css 的 .job-progress 套件；这里只留批量特有的 */
.batch-recent {
  margin-top: 6px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  font-size: 12px;
  color: var(--app-text-muted);
}

.batch-recent-failed {
  color: var(--app-danger);
}

.job-progress-actions {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.batch-hint {
  font-size: 12px;
  color: var(--app-text-muted);
}

.summary-alert {
  margin-bottom: 12px;
}

.holding-name {
  margin-right: 6px;
}

.ai-tags {
  display: inline-flex;
  gap: 4px;
  cursor: help;
}

.ai-untagged {
  color: var(--app-text-soft);
  font-size: 12px;
}

.event-badge {
  cursor: help;
}

.label-help {
  margin-left: 4px;
  color: var(--app-text-soft);
  cursor: help;
  vertical-align: -2px;
}

.holdings-page {
  width: 100%;
}

.holdings-card {
  overflow: hidden;
}

.market-select {
  width: 150px;
}

.summary-section {
  margin-top: 24px;
}

.account-unassigned {
  color: var(--app-text-soft);
}

.summary-item {
  min-height: 116px;
  padding: 18px;
  background-color: var(--app-surface-muted);
  border: 1px solid var(--app-border-soft);
  border-left: 3px solid var(--summary-accent);
  border-radius: 8px;
}

.summary-cost {
  --summary-accent: var(--app-primary);
}

.summary-market {
  --summary-accent: var(--app-info);
}

.summary-profit {
  --summary-accent: var(--app-success);
}

.summary-rate {
  --summary-accent: var(--app-warning);
}

.summary-label {
  font-size: 13px;
  color: var(--app-text-muted);
  margin-bottom: 8px;
}

.summary-value {
  font-size: 22px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: var(--app-text);
  line-height: 1.2;
}

.summary-sub-value {
  font-size: 14px;
  color: var(--app-text-muted);
  margin-top: 4px;
}

:deep(.el-input-number) {
  width: 112px;
}

@media (max-width: 900px) {
  .header-actions {
    align-items: stretch;
    width: 100%;
  }

  .market-select {
    width: 100%;
  }

  .summary-section {
    margin-top: 18px;
  }

  .summary-value {
    font-size: 22px;
    overflow-wrap: anywhere;
  }
}

@media (max-width: 640px) {
  .holdings-card :deep(.el-card__header) {
    text-align: center;
  }

  /* 卡片通用外观见 styles.css 的 .mobile-card 套件；这里只留持仓特有的价格输入行 */
  .mobile-price-row {
    display: grid;
    grid-template-columns: 72px minmax(0, 1fr);
    align-items: center;
    gap: 10px;
    margin-top: 12px;
    color: var(--app-text-muted);
    font-size: 13px;
  }

  .mobile-price-row :deep(.el-input-number) {
    width: 100%;
  }
}
</style>
