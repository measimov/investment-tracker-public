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
            <div class="mobile-card-title">
              <span class="mobile-card-symbol">{{ row.symbol }}</span>
              <span class="mobile-card-name">{{ row.name }}</span>
            </div>
            <div class="mobile-card-tags">
              <el-tag size="small" effect="plain">{{ row.market }}</el-tag>
              <el-tag size="small" type="info" effect="plain">
                {{ accountLabel(row.broker_account_id) }}
              </el-tag>
              <el-tag v-if="upcomingEvent(row)" type="warning" size="small" effect="plain">
                {{ upcomingEvent(row)!.label }}·{{ upcomingEvent(row)!.daysText }}
              </el-tag>
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
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { QuestionFilled, Refresh } from '@element-plus/icons-vue'
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
  await Promise.all([loadExchangeRates(), loadHoldings(), loadBrokerAccounts()])
  loadSecurityEvents()
  loadSecurityAnalyses()
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
