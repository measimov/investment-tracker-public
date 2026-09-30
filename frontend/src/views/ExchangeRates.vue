<template>
  <div class="exchange-rates-page">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>汇率管理</span>
          <div v-if="canEdit" class="header-actions">
            <el-button :icon="Refresh" @click="refreshFromAPI" :loading="refreshing">
              从API更新汇率
            </el-button>
            <el-button type="primary" :icon="Plus" @click="showAddDialog">手动添加汇率</el-button>
          </div>
          <el-text v-else type="info" size="small">汇率为全局数据，由管理员维护</el-text>
        </div>
      </template>

      <!-- 当前汇率展示 -->
      <div class="current-rates" v-loading="loadingLatest && hasLoaded">
        <h3>当前汇率（基准货币：{{ latestBaseCurrency }}）</h3>
        <el-row v-if="initialLoading" :gutter="20">
          <el-col v-for="index in 4" :key="index" :xs="24" :sm="12" :md="6">
            <el-card shadow="never" class="rate-skeleton-card">
              <el-skeleton animated>
                <template #template>
                  <el-skeleton-item variant="text" class="rate-title-skeleton" />
                  <el-skeleton-item variant="h3" class="rate-value-skeleton" />
                  <div class="rate-info">
                    <el-skeleton-item variant="text" />
                    <el-skeleton-item variant="button" class="rate-tag-skeleton" />
                  </div>
                </template>
              </el-skeleton>
            </el-card>
          </el-col>
        </el-row>
        <el-empty
          v-else-if="displayRates.length === 0"
          description="暂无可用汇率"
          :image-size="88"
        />
        <el-row v-else :gutter="20">
          <el-col v-for="card in displayRates" :key="card.currency" :xs="24" :sm="12" :md="6">
            <el-card
              shadow="hover"
              class="rate-card"
              :class="{ 'rate-card--stale': card.stale }"
              :data-testid="`rate-card-${card.currency}`"
            >
              <div class="rate-card-head">
                <span class="currency-code">{{ card.currency }}</span>
                <span class="rate-caption">1 {{ card.currency }} 兑 {{ latestBaseCurrency }}</span>
              </div>
              <div class="rate-value">
                {{ formatNumber(card.rate, 4) }}
                <span class="rate-unit">{{ latestBaseCurrency }}</span>
              </div>
              <div class="rate-info">
                <el-text size="small" :type="card.stale ? 'warning' : 'info'">
                  生效: {{ formatDate(card.effectiveDate) }}
                  <template v-if="card.ageDays !== null && card.ageDays > 0">
                    （{{ card.ageDays }} 天前）
                  </template>
                </el-text>
                <span class="rate-tags">
                  <el-tooltip
                    v-if="card.stale"
                    :content="`超过 ${RATE_STALE_DAYS} 天未更新，折算与估值仍按这条汇率计算`"
                  >
                    <el-tag size="small" type="warning" effect="dark">过期</el-tag>
                  </el-tooltip>
                  <el-tag v-if="card.source" size="small" :type="sourceTagType(card.source)">
                    {{ sourceLabel(card.source) }}
                  </el-tag>
                </span>
              </div>
            </el-card>
          </el-col>
        </el-row>
      </div>

      <el-divider />

      <!-- 官方中间价与第三方报价比对（#200） -->
      <div class="rate-checks">
        <h3>官方中间价与第三方比对（近 30 天）</h3>
        <el-text size="small" type="info" class="rate-checks-tip">
          折算以中国外汇交易中心人民币汇率中间价为准（工作日 9:15 发布）；第三方报价只用于逐日比对，
          官方中间价持续不可用时才会顶上并在仪表盘告警。
        </el-text>
        <el-empty v-if="sourceChecks.length === 0" description="暂无比对记录" :image-size="64" />
        <div v-else class="responsive-table">
          <el-table :data="sourceChecks" size="small" stripe max-height="320">
            <el-table-column label="比对日" min-width="100">
              <template #default="{ row }">{{ formatDate(row.check_date) }}</template>
            </el-table-column>
            <el-table-column prop="from_currency" label="币种" min-width="70" />
            <el-table-column label="官方中间价" min-width="150" align="right">
              <template #default="{ row }">
                {{ formatNumber(row.official_rate, 4) }}
                <el-text size="small" type="info">（{{ formatDate(row.official_date) }}）</el-text>
              </template>
            </el-table-column>
            <el-table-column label="第三方报价" min-width="160" align="right">
              <template #default="{ row }">
                {{ formatNumber(row.reference_rate, 4) }}
                <el-text size="small" type="info"
                  >（{{ sourceLabel(row.reference_source) }}）</el-text
                >
              </template>
            </el-table-column>
            <el-table-column label="差异" min-width="90" align="right">
              <template #default="{ row }">
                <el-text :type="isDiffAbnormal(row.diff_pct) ? 'danger' : undefined">
                  {{ Number(row.diff_pct) > 0 ? '+' : '' }}{{ formatNumber(row.diff_pct, 2) }}%
                </el-text>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>

      <el-divider />

      <!-- 汇率历史记录 -->
      <div class="rate-history">
        <div class="history-header">
          <h3>汇率历史记录</h3>
          <!-- 停用行保留作审计，默认不列出；管理员可切换查看 -->
          <el-switch
            v-if="canEdit"
            v-model="showInactive"
            active-text="显示已停用"
            @change="loadRateHistory"
          />
        </div>
        <el-alert
          v-if="rateHistory.length >= HISTORY_LIMIT"
          type="info"
          :closable="false"
          show-icon
          class="list-limit-alert"
          :title="`仅显示最近 ${HISTORY_LIMIT} 条汇率记录`"
        />
        <div v-if="initialLoading" class="history-skeleton">
          <el-skeleton animated :rows="7" />
        </div>
        <div v-else class="responsive-table">
          <el-table :data="rateHistory" stripe v-loading="loadingHistory">
            <template #empty>
              <el-empty description="暂无汇率历史记录" :image-size="88" />
            </template>
            <el-table-column prop="from_currency" label="源币种" min-width="90" />
            <el-table-column prop="to_currency" label="目标币种" min-width="90" />
            <el-table-column prop="rate" label="汇率" min-width="110" align="right">
              <template #default="{ row }">
                {{ formatNumber(row.rate, 4) }}
              </template>
            </el-table-column>
            <el-table-column label="生效日期" min-width="110">
              <template #default="{ row }">{{ formatDate(row.effective_date) }}</template>
            </el-table-column>
            <el-table-column prop="source" label="来源" min-width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="sourceTagType(row.source)">
                  {{ sourceLabel(row.source) }}
                </el-tag>
                <el-tag
                  v-if="row.is_active === false"
                  size="small"
                  type="info"
                  class="inactive-tag"
                >
                  已停用
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="created_at" label="创建时间" min-width="150">
              <template #default="{ row }">
                {{ formatDateTime(row.created_at) }}
              </template>
            </el-table-column>
            <el-table-column v-if="canEdit" label="操作" width="150" fixed="right">
              <template #default="{ row }">
                <el-button size="small" type="primary" @click="editRate(row)"> 编辑 </el-button>
                <!-- 只有手工行能停用：官方/第三方行会被下一次刷新重建，改值请编辑 -->
                <el-button
                  v-if="isManualSource(row.source) && row.is_active !== false"
                  size="small"
                  type="danger"
                  plain
                  @click="deactivateRate(row.id)"
                >
                  停用
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>
    </el-card>

    <!-- 添加/编辑汇率对话框 -->
    <el-dialog v-model="dialogVisible" :title="editingRate ? '编辑汇率' : '添加汇率'" width="560px">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        class="global-rate-alert"
        :title="GLOBAL_RATE_NOTICE"
      />
      <el-form :model="rateForm" :rules="rules" ref="rateFormRef" label-width="100px">
        <el-form-item label="源币种" prop="from_currency">
          <el-select
            v-model="rateForm.from_currency"
            placeholder="请选择源币种"
            :disabled="!!editingRate"
          >
            <el-option
              v-for="curr in currencies"
              :key="curr.code"
              :label="`${curr.name} (${curr.code})`"
              :value="curr.code"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="目标币种" prop="to_currency">
          <el-select
            v-model="rateForm.to_currency"
            placeholder="请选择目标币种"
            :disabled="!!editingRate"
          >
            <el-option
              v-for="curr in currencies"
              :key="curr.code"
              :label="`${curr.name} (${curr.code})`"
              :value="curr.code"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="汇率" prop="rate">
          <el-input-number
            v-model="rateForm.rate"
            :precision="4"
            :step="0.0001"
            :min="0"
            placeholder="请输入汇率"
          />
          <div v-if="rateForm.from_currency && rateForm.to_currency" class="form-tip">
            即 1 {{ rateForm.from_currency }} 可兑换多少 {{ rateForm.to_currency }}
          </div>
        </el-form-item>

        <el-form-item label="生效日期" prop="effective_date">
          <el-date-picker
            v-model="rateForm.effective_date"
            type="date"
            placeholder="选择生效日期"
            format="YYYY-MM-DD"
            value-format="YYYY-MM-DD"
            :disabled="!!editingRate"
          />
        </el-form-item>

        <el-form-item v-if="editingRate" label="来源">
          <el-tag size="small" :type="sourceTagType(editingRate.source || '')">
            {{ sourceLabel(editingRate.source) }}
          </el-tag>
        </el-form-item>

        <el-form-item v-if="editingRate">
          <el-text type="info" size="small">修改数值后该行记为「手工」，不再被自动刷新覆盖</el-text>
        </el-form-item>
      </el-form>

      <template #footer>
        <div class="mobile-dialog-footer">
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button type="primary" @click="submitRate" :loading="submitting">确定</el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { showApiError } from '@/utils/showApiError'
import { Refresh, Plus } from '@element-plus/icons-vue'
import { computed, ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormItemRule } from 'element-plus'
import api from '@/api'
import { useAuthStore } from '@/stores/auth'
import type {
  ExchangeRate,
  ExchangeRateCheck,
  ExchangeRateCreate,
  ExchangeRateLatest
} from '@/types'
import { CURRENCIES } from '@/utils/currency'
import { formatDate, formatDateTime, formatNumber, todayLocalISODate } from '@/utils/helpers'
import { RATE_STALE_DAYS, buildRateCards } from './exchange-rates/rateCards'
import { isDiffAbnormal, sourceLabel, sourceTagType } from './exchange-rates/sources'

// 汇率是全局表（不分用户）：任何人的增删改都会改变所有用户的折算与估值
const GLOBAL_RATE_NOTICE = '汇率为全局数据，修改会影响所有用户的金额折算与持仓估值'
const HISTORY_LIMIT = 100

// 写入仅管理员（#277）：非管理员只读，不显示新增/刷新/编辑/停用
const authStore = useAuthStore()
const canEdit = computed(() => authStore.isAdmin)
const showInactive = ref(false)
const isManualSource = (source: string | null | undefined) => !source || source === 'manual'

// 后端 ExchangeRate schema 为准（此前手写副本把 source/is_active 写成非空，已漂移）
type RateRow = ExchangeRate

// 后端 ExchangeRateLatest schema 为准（此前手写副本把全部必填字段放宽为 optional）
type LatestRates = ExchangeRateLatest

const latestRates = ref<LatestRates | null>(null)
const rateHistory = ref<RateRow[]>([])
const sourceChecks = ref<ExchangeRateCheck[]>([])
const loadingLatest = ref(false)
const loadingHistory = ref(false)
const hasLoaded = ref(false)
const dialogVisible = ref(false)
const refreshing = ref(false)
const submitting = ref(false)
const editingRate = ref<RateRow | null>(null)

const rateFormRef = ref<FormInstance | null>(null)
const rateForm = ref<{
  from_currency: string
  to_currency: string
  rate: number | null
  effective_date: string
}>({
  from_currency: '',
  to_currency: 'CNY',
  rate: null,
  effective_date: todayLocalISODate()
})

const currencies = CURRENCIES

const displayRates = computed(() =>
  latestRates.value ? buildRateCards(latestRates.value, todayLocalISODate()) : []
)

const latestBaseCurrency = computed(() => latestRates.value?.base_currency || 'CNY')
const initialLoading = computed(
  () => !hasLoaded.value && (loadingLatest.value || loadingHistory.value)
)

const validateCurrencyPair: FormItemRule['validator'] = (_rule, _value, callback) => {
  const { from_currency: from, to_currency: to } = rateForm.value
  if (from && to && from === to) callback(new Error('源币种与目标币种不能相同'))
  else callback()
}

const validatePositiveRate: FormItemRule['validator'] = (_rule, value, callback) => {
  if (value === null || value === undefined || value === '') callback(new Error('请输入汇率'))
  else if (!(Number(value) > 0)) callback(new Error('汇率必须大于 0'))
  else callback()
}

const rules: Record<string, FormItemRule[]> = {
  from_currency: [
    { required: true, message: '请选择源币种', trigger: 'change' },
    { validator: validateCurrencyPair, trigger: 'change' }
  ],
  to_currency: [
    { required: true, message: '请选择目标币种', trigger: 'change' },
    { validator: validateCurrencyPair, trigger: 'change' }
  ],
  rate: [{ validator: validatePositiveRate, trigger: 'blur' }],
  effective_date: [{ required: true, message: '请选择生效日期', trigger: 'change' }]
}

// 加载最新汇率
const loadLatestRates = async () => {
  loadingLatest.value = true
  try {
    const response = await api.getLatestRates()
    latestRates.value = response.data
  } catch (error) {
    showApiError(error, '加载最新汇率失败')
    console.error(error)
  } finally {
    loadingLatest.value = false
  }
}

// 加载历史汇率
const loadRateHistory = async () => {
  loadingHistory.value = true
  try {
    const response = await api.getExchangeRates({
      limit: HISTORY_LIMIT,
      ...(showInactive.value ? { include_inactive: true } : {})
    })
    rateHistory.value = response.data
  } catch (error) {
    showApiError(error, '加载汇率历史失败')
    console.error(error)
  } finally {
    loadingHistory.value = false
  }
}

// 官方中间价 vs 第三方比对（近 30 天）：辅助信息，失败不打扰页面主流程
const loadSourceChecks = async () => {
  try {
    const response = await api.getExchangeRateSourceChecks(30)
    sourceChecks.value = response.data
  } catch (error) {
    console.error(error)
  }
}

const loadInitialData = async () => {
  try {
    await Promise.all([loadLatestRates(), loadRateHistory(), loadSourceChecks()])
  } finally {
    hasLoaded.value = true
  }
}

// 从API刷新汇率
const refreshFromAPI = async () => {
  try {
    refreshing.value = true
    const response = await api.refreshRatesFromAPI()
    ElMessage.success(`成功更新 ${response.data.count} 个汇率`)
    void loadSourceChecks()
    await loadLatestRates()
    await loadRateHistory()
  } catch (error) {
    showApiError(error, { prefix: '从 API 更新汇率失败' })
    console.error(error)
  } finally {
    refreshing.value = false
  }
}

// 显示添加对话框
const showAddDialog = () => {
  editingRate.value = null
  rateForm.value = {
    from_currency: '',
    to_currency: 'CNY',
    rate: null,
    effective_date: todayLocalISODate()
  }
  dialogVisible.value = true
}

// 编辑汇率
const editRate = (rate: RateRow) => {
  editingRate.value = rate
  rateForm.value = {
    from_currency: rate.from_currency,
    to_currency: rate.to_currency,
    rate: Number(rate.rate),
    effective_date: rate.effective_date
  }
  dialogVisible.value = true
}

// 提交汇率
const submitRate = async () => {
  if (!rateFormRef.value) return

  await rateFormRef.value.validate(async (valid) => {
    if (!valid) return

    try {
      submitting.value = true

      if (editingRate.value) {
        // 更新
        // 只改数值：来源由服务端记为 manual，状态不在此编辑（停用走单独按钮）
        await api.updateExchangeRate(editingRate.value.id, { rate: rateForm.value.rate as number })
        ElMessage.success('汇率更新成功')
      } else {
        // 创建
        // 来源由服务端固定为 manual（#277：客户端不能再指定来源）
        // 表单校验（rules）保证汇率必填
        await api.createOrUpdateExchangeRate({ ...rateForm.value } as ExchangeRateCreate)
        ElMessage.success('汇率添加成功')
      }

      dialogVisible.value = false
      await loadLatestRates()
      await loadRateHistory()
    } catch (error) {
      showApiError(error, { prefix: '操作失败' })
      console.error(error)
    } finally {
      submitting.value = false
    }
  })
}

// 停用汇率（#277：删除改为停用，保留审计；折算不再使用它，同日重新录入即恢复）
const deactivateRate = async (id: number) => {
  try {
    await ElMessageBox.confirm(
      `确定要停用这条汇率吗？停用后折算不再使用它。${GLOBAL_RATE_NOTICE}。`,
      '停用汇率',
      { type: 'warning', confirmButtonText: '确定停用', cancelButtonText: '取消' }
    )
  } catch {
    return // 取消
  }
  try {
    await api.deleteExchangeRate(id)
    ElMessage.success('已停用')
    // 与 submitRate 对齐：最新汇率卡片也要刷——停用某币种唯一一条汇率后，
    // 卡片不能继续展示已不生效的汇率（E2E 汇率增删改用例锁定此行为）
    await loadLatestRates()
    await loadRateHistory()
  } catch (error) {
    showApiError(error, '停用失败')
  }
}

onMounted(() => {
  loadInitialData()
})
</script>

<style scoped>
.exchange-rates-page {
  width: 100%;
}

.rate-checks-tip {
  display: block;
  margin-bottom: 8px;
}

.current-rates {
  margin-bottom: 20px;
}

.current-rates h3,
.rate-history h3 {
  margin-bottom: 15px;
  font-size: 16px;
  font-weight: 600;
}

.currency-code {
  font-weight: bold;
  color: var(--app-primary);
  margin-right: 6px;
}

.rate-card-head {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 4px;
}

.rate-caption {
  font-size: 12px;
  color: var(--app-text-soft);
}

.rate-value {
  margin-top: 8px;
  font-size: 24px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.rate-unit {
  font-size: 13px;
  font-weight: 400;
  color: var(--app-text-soft);
}

.rate-card--stale {
  border-color: var(--app-warning);
}

.rate-tags {
  display: inline-flex;
  gap: 6px;
}

.global-rate-alert,
.list-limit-alert {
  margin-bottom: 14px;
}

.form-tip {
  width: 100%;
  font-size: 12px;
  color: var(--app-text-soft);
  margin-top: 4px;
}

.rate-info {
  margin-top: 10px;
  display: flex;
  gap: 8px;
  justify-content: space-between;
  align-items: center;
}

.rate-skeleton-card {
  min-height: 116px;
}

.rate-title-skeleton {
  width: 42%;
  height: 14px;
}

.rate-value-skeleton {
  width: 72%;
  height: 26px;
  margin: 12px 0 2px;
}

.rate-tag-skeleton {
  width: 52px;
}

.rate-history {
  margin-top: 20px;
}

.history-skeleton {
  min-height: 300px;
  padding: 12px 0;
}

@media (max-width: 900px) {
  .header-actions {
    width: 100%;
    justify-content: flex-start;
  }

  .rate-info {
    align-items: flex-start;
    flex-direction: column;
  }
}
.history-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.inactive-tag {
  margin-left: 4px;
}
</style>
