<template>
  <div class="exchange-rates-page">
    <header class="page-heading">
      <div>
        <h1 class="page-title">汇率管理</h1>
        <p class="page-intro page-description">
          汇率为全局数据，由管理员维护，应用于所有用户的金额折算与持仓估值。
        </p>
      </div>
      <div v-if="canEdit" class="header-actions">
        <NButton :loading="refreshing" aria-label="刷新汇率" @click="refreshFromAPI"
          >刷新汇率</NButton
        >
        <NButton type="primary" @click="showAddDialog">手动添加汇率</NButton>
      </div>
    </header>

    <section class="current-rates rate-section" aria-label="当前汇率">
      <div class="section-heading">
        <h2>
          当前汇率
          <span class="section-caption"
            >基准货币 {{ latestLoaded ? latestBaseCurrency : '—' }}</span
          >
        </h2>
        <NButton
          text
          type="primary"
          :loading="loadingLatest"
          aria-label="重新加载当前汇率"
          @click="loadLatestRates"
          >重新加载</NButton
        >
      </div>
      <NAlert
        v-if="latestError"
        type="warning"
        :show-icon="false"
        class="read-alert"
        title="当前汇率加载失败"
      >
        {{
          latestLoaded ? '显示上次成功加载的汇率，尚未确认最新结果。' : '尚未确认当前汇率，请重试。'
        }}
        <NButton text type="primary" aria-label="重试当前汇率" @click="loadLatestRates"
          >重试加载</NButton
        >
      </NAlert>
      <p v-else-if="loadingLatest && latestLoaded" class="read-note" role="status">
        正在重新加载，以下为上次成功数据。
      </p>
      <NSpin :show="loadingLatest">
        <NEmpty
          v-if="!displayRates.length"
          :description="latestEmpty"
          :theme-overrides="{ textColor: 'var(--app-text-muted)' }"
        />
        <div v-else class="rates-grid">
          <article
            v-for="card in displayRates"
            :key="card.currency"
            class="rate-card"
            :class="{ 'rate-card--stale': card.stale }"
            :data-testid="`rate-card-${card.currency}`"
          >
            <div class="rate-card-head">
              <span class="currency-code">{{ card.currency }}</span
              ><span class="rate-caption">1 {{ card.currency }} 兑 {{ latestBaseCurrency }}</span>
            </div>
            <div class="rate-value">
              {{ formatNumber(card.rate, 4) }}
              <span class="rate-unit">{{ latestBaseCurrency }}</span>
            </div>
            <p class="effective-date">
              生效 {{ formatDate(card.effectiveDate)
              }}<span v-if="card.ageDays !== null && card.ageDays > 0">
                · {{ card.ageDays }} 天前</span
              >
            </p>
            <div class="rate-tags">
              <NTag
                v-if="card.source"
                size="small"
                :bordered="false"
                :type="rateSourceType(card.source)"
                >{{ sourceLabel(card.source) }}</NTag
              ><NTag v-if="card.stale" type="warning" size="small" :bordered="false">过期</NTag>
            </div>
            <p v-if="card.stale" class="stale-note">
              超过 {{ RATE_STALE_DAYS }} 天未更新，折算与估值仍按这条汇率计算。
            </p>
          </article>
        </div>
      </NSpin>
    </section>

    <section class="rate-checks rate-section" aria-label="官方中间价与第三方比对">
      <div class="section-heading">
        <h2>官方中间价与第三方比对 <span class="section-caption">近 30 天</span></h2>
        <NButton
          text
          type="primary"
          :loading="loadingChecks"
          aria-label="重新加载汇率比对"
          @click="loadSourceChecks"
          >重新加载</NButton
        >
      </div>
      <p class="section-description">
        折算以中国外汇交易中心人民币汇率中间价为准（工作日 9:15
        发布）；第三方报价只用于逐日比对，官方中间价持续不可用时才会顶上并在仪表盘告警。
      </p>
      <NAlert
        v-if="checksError"
        type="warning"
        :show-icon="false"
        class="read-alert"
        title="汇率比对加载失败"
        >{{
          checksLoaded
            ? '显示上次成功加载的比对记录，尚未确认最新结果。'
            : '尚未确认比对记录，请重试。'
        }}
        <NButton text type="primary" aria-label="重试汇率比对" @click="loadSourceChecks"
          >重试加载</NButton
        ></NAlert
      >
      <p v-else-if="loadingChecks && checksLoaded" class="read-note" role="status">
        正在重新加载，以下为上次成功比对。
      </p>
      <SourceChecksTable
        :rows="sourceChecks"
        :loading="loadingChecks"
        :empty-description="checksEmpty"
      />
    </section>

    <section class="rate-history rate-section" aria-label="汇率历史记录">
      <div class="section-heading">
        <h2>汇率历史记录</h2>
        <div class="history-actions">
          <label v-if="canEdit" class="inactive-filter"
            ><input
              v-model="showInactive"
              type="checkbox"
              @change="loadRateHistory"
            />显示已停用</label
          ><NButton
            text
            type="primary"
            :loading="loadingHistory"
            aria-label="重新加载汇率历史"
            @click="loadRateHistory"
            >重新加载</NButton
          >
        </div>
      </div>
      <NAlert
        v-if="historyError"
        type="warning"
        :show-icon="false"
        class="read-alert"
        title="汇率历史加载失败"
        >{{
          historyLoaded
            ? '显示上次成功加载的历史记录，尚未确认当前筛选结果。'
            : '尚未确认汇率历史，请重试。'
        }}
        <NButton text type="primary" aria-label="重试汇率历史" @click="loadRateHistory"
          >重试加载</NButton
        ></NAlert
      >
      <p v-if="historyOutdated" class="read-note" role="status">
        上次成功范围：{{ historyIncludesInactive ? '包含已停用' : '仅生效记录' }}。{{
          loadingHistory ? '正在加载当前筛选。' : '当前筛选尚未成功加载。'
        }}
      </p>
      <p v-if="rateHistory.length >= HISTORY_LIMIT" class="read-note">
        仅显示最近 {{ HISTORY_LIMIT }} 条汇率记录
      </p>
      <RateHistoryTable
        :rows="rateHistory"
        :loading="loadingHistory"
        :empty-description="historyEmpty"
        :can-edit="canEdit"
        @edit="editRate"
        @deactivate="deactivateRate"
      />
    </section>
    <!-- 添加/编辑汇率对话框 -->
    <el-dialog
      v-model="dialogVisible"
      :title="editingRate ? '编辑汇率' : '添加汇率'"
      width="560px"
      :close-on-click-modal="false"
      class="rate-dialog"
    >
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
            aria-label="源币种"
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
            aria-label="目标币种"
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
            aria-label="汇率"
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
            aria-label="生效日期"
            format="YYYY/MM/DD"
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
import { NAlert, NButton, NEmpty, NSpin, NTag } from 'naive-ui'
import RateHistoryTable from './exchange-rates/RateHistoryTable.vue'
import SourceChecksTable from './exchange-rates/SourceChecksTable.vue'
import { computed, ref, onMounted, nextTick } from 'vue'
import { useLatestRequest } from '@/composables/useLatestRequest'
import { ElMessage, type FormInstance, type FormItemRule } from 'element-plus'
import { confirmAction } from '@/composables/useConfirmAction'
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
const latestLoaded = ref(false)
const latestError = ref(false)
const checksLoaded = ref(false)
const checksError = ref(false)
const loadingChecks = ref(false)
const historyLoaded = ref(false)
const historyError = ref(false)
const historyIncludesInactive = ref(false)
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
const latestEmpty = computed(() =>
  latestError.value
    ? '尚未确认当前汇率，请重试'
    : !latestLoaded.value
      ? '当前汇率正在加载'
      : '暂无可用汇率'
)
const checksEmpty = computed(() =>
  checksError.value
    ? '尚未确认比对记录，请重试'
    : !checksLoaded.value
      ? '比对记录正在加载'
      : '暂无比对记录'
)
const historyOutdated = computed(
  () =>
    historyLoaded.value &&
    (loadingHistory.value ||
      historyError.value ||
      historyIncludesInactive.value !== showInactive.value)
)
const historyEmpty = computed(() =>
  historyError.value
    ? '尚未确认当前筛选结果，请重试'
    : !historyLoaded.value
      ? '汇率历史正在加载'
      : historyOutdated.value
        ? '上次成功范围无汇率记录，当前筛选尚未确认'
        : '暂无汇率历史记录'
)
const rateSourceType = (source: string | null) => {
  const type = sourceTagType(source)
  return type === 'danger' ? 'error' : type === 'info' ? 'default' : type
}

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

// 只允许最新读取更新数据、错误及loading；失败不等于已确认没有记录。
const latestRequest = useLatestRequest()
const loadLatestRates = async () => {
  const token = latestRequest.begin()
  loadingLatest.value = true
  try {
    const response = await api.getLatestRates()
    if (!latestRequest.isCurrent(token)) return
    latestRates.value = response.data
    latestLoaded.value = true
    latestError.value = false
  } catch (error) {
    if (!latestRequest.isCurrent(token)) return
    latestError.value = true
    showApiError(error, '加载最新汇率失败')
    console.error(error)
  } finally {
    if (latestRequest.isCurrent(token)) loadingLatest.value = false
  }
}

const historyRequest = useLatestRequest()
const loadRateHistory = async () => {
  const token = historyRequest.begin()
  const includeInactive = showInactive.value
  loadingHistory.value = true
  try {
    const response = await api.getExchangeRates({
      limit: HISTORY_LIMIT,
      ...(includeInactive ? { include_inactive: true } : {})
    })
    if (!historyRequest.isCurrent(token)) return
    rateHistory.value = response.data
    historyIncludesInactive.value = includeInactive
    historyLoaded.value = true
    historyError.value = false
  } catch (error) {
    if (!historyRequest.isCurrent(token)) return
    historyError.value = true
    showApiError(error, '加载汇率历史失败')
    console.error(error)
  } finally {
    if (historyRequest.isCurrent(token)) loadingHistory.value = false
  }
}

// 比对仍是辅助读取；局部提示及只读重试，不阻断当前汇率/历史。
const checksRequest = useLatestRequest()
const loadSourceChecks = async () => {
  const token = checksRequest.begin()
  loadingChecks.value = true
  try {
    const response = await api.getExchangeRateSourceChecks(30)
    if (!checksRequest.isCurrent(token)) return
    sourceChecks.value = response.data
    checksLoaded.value = true
    checksError.value = false
  } catch (error) {
    if (!checksRequest.isCurrent(token)) return
    checksError.value = true
    console.error(error)
  } finally {
    if (checksRequest.isCurrent(token)) loadingChecks.value = false
  }
}

const loadInitialData = () =>
  Promise.all([loadLatestRates(), loadRateHistory(), loadSourceChecks()])

// 从API刷新汇率
const refreshFromAPI = async () => {
  try {
    refreshing.value = true
    const response = await api.refreshRatesFromAPI()
    ElMessage.success(`${response.data.count} 条汇率已更新`)
    void loadSourceChecks()
    await loadLatestRates()
    await loadRateHistory()
  } catch (error) {
    showApiError(error, { prefix: '刷新汇率失败' })
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
  void nextTick(() => rateFormRef.value?.clearValidate())
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
  void nextTick(() => rateFormRef.value?.clearValidate())
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
        ElMessage.success('汇率已更新')
      } else {
        // 创建
        // 来源由服务端固定为 manual（#277：客户端不能再指定来源）
        // 表单校验（rules）保证汇率必填
        await api.createOrUpdateExchangeRate({ ...rateForm.value } as ExchangeRateCreate)
        ElMessage.success('汇率已新增')
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
  if (
    !(await confirmAction({
      title: '停用汇率',
      message: `确定要停用这条汇率吗？停用后折算不再使用它。${GLOBAL_RATE_NOTICE}。`,
      confirmText: '停用'
    }))
  )
    return
  try {
    await api.deleteExchangeRate(id)
    ElMessage.success('汇率已停用')
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
  min-width: 0;
}
.page-heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 24px;
  margin-bottom: 28px;
}
.page-intro {
  max-width: 660px;
}
.header-actions,
.history-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.rate-section {
  padding: 22px 0;
  border-top: 1px solid var(--app-border);
}
.section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 18px;
  flex-wrap: wrap;
}
h2 {
  font-size: 20px;
  font-weight: 600;
  line-height: 1.5;
  margin: 0;
}
.section-caption {
  font-size: 12px;
  font-weight: 400;
  color: var(--app-text-muted);
  margin-left: 8px;
}
.section-heading :deep(.n-button) {
  min-height: 24px;
}
.section-description,
.read-note {
  color: var(--app-text-muted);
  font-size: 13px;
  line-height: 1.7;
  margin: 0 0 16px;
}
.read-alert {
  margin-bottom: 14px;
}
.read-alert :deep(.n-button) {
  margin-left: 10px;
  min-height: 24px;
}
.rates-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}
.rate-card {
  background: var(--app-surface);
  padding: 20px;
  border: 1px solid var(--app-border);
  border-radius: 6px;
  min-width: 0;
}
.rate-card-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
.currency-code {
  font-weight: 600;
  color: var(--app-primary-strong);
}
.rate-caption,
.rate-unit {
  font-size: 12px;
  color: var(--app-text-muted);
}
.rate-value {
  font-size: 30px;
  font-weight: 550;
  letter-spacing: -0.02em;
  line-height: 1.3;
  margin: 14px 0 10px;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.effective-date {
  font-size: 12px;
  color: var(--app-text-muted);
  line-height: 1.6;
  margin: 0 0 10px;
  font-variant-numeric: tabular-nums;
}
.rate-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.stale-note {
  font-size: 12px;
  line-height: 1.7;
  color: var(--app-warning-text);
  margin: 10px 0 0;
}
.rate-card--stale {
  border-color: var(--app-warning);
}
.inactive-filter {
  display: inline-flex;
  gap: 8px;
  align-items: center;
  font-size: 13px;
  min-height: 24px;
  cursor: pointer;
}
.inactive-filter input {
  accent-color: var(--app-primary-strong);
  width: 16px;
  height: 16px;
}
.global-rate-alert {
  margin-bottom: 14px;
}
.form-tip {
  width: 100%;
  font-size: 12px;
  color: var(--app-text-muted);
  line-height: 1.7;
  margin-top: 4px;
}
:deep(.rate-dialog) {
  --el-text-color-placeholder: var(--app-text-soft);
}
@media (max-width: 1280px) {
  .rates-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 640px) {
  .page-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 16px;
    margin-bottom: 22px;
  }
  .header-actions {
    width: 100%;
  }
  .header-actions :deep(.n-button) {
    min-height: 44px;
  }
  .rate-section {
    padding: 20px 0;
  }
  .section-heading {
    margin-bottom: 14px;
  }
  .section-heading :deep(.n-button),
  .read-alert :deep(.n-button),
  .inactive-filter {
    min-height: 44px;
  }
  .rate-card {
    padding: 16px;
  }
  .rate-value {
    font-size: 28px;
  }
  .rate-caption {
    font-size: 11px;
  }
  .rate-card-head {
    gap: 4px;
  }
  .rates-grid {
    gap: 10px;
  }
  :deep(.rate-dialog .el-input__wrapper),
  :deep(.rate-dialog .el-select__wrapper) {
    min-height: 44px;
    box-sizing: border-box;
  }
  :deep(.rate-dialog .el-input__inner) {
    height: 40px;
  }
  :deep(.rate-dialog .el-input-number) {
    width: 100%;
  }
}

@media (min-width: 1025px) {
  .page-heading {
    margin-bottom: 16px;
  }
  .rate-section {
    padding: 16px 0;
  }
  .section-heading {
    margin-bottom: 12px;
  }
  h2 {
    font-size: 18px;
  }
  .rate-card {
    padding: 16px;
  }
  .rate-value {
    font-size: 26px;
  }
}
</style>
