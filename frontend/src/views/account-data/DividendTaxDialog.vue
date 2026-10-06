<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { NButton } from 'naive-ui'
import api from '@/api'
import type { CashEvent, CorporateAction } from '@/types'
import { useLatestRequest } from '@/composables/useLatestRequest'
import { showApiError } from '@/utils/showApiError'
import { formatCurrency, formatDate } from '@/utils/helpers'

const props = defineProps<{ event: CashEvent; reload: () => Promise<unknown> }>()
const emit = defineEmits<{ close: [] }>()
const requests = useLatestRequest()
const loading = ref(true)
const saving = ref(false)
const visible = ref(true)
const loadFailed = ref(false)
const candidates = ref<CorporateAction[]>([])
const allocations = ref(
  (props.event.tax_allocations ?? []).map((item) => ({
    corporate_action_id: item.corporate_action_id as number | undefined,
    amount: Number(item.amount)
  }))
)
const allocatedAmount = computed(() =>
  allocations.value.reduce((total, row) => total + row.amount, 0)
)
const remaining = computed(() => Number(props.event.amount) - allocatedAmount.value)

onMounted(async () => {
  const request = requests.begin()
  try {
    const rows: CorporateAction[] = []
    for (let skip = 0; ; skip += 500) {
      const response = await api.getCorporateActions({
        broker_account_id: props.event.broker_account_id ?? undefined,
        unassigned_account: props.event.broker_account_id == null,
        action_type: 'CASH_DIVIDEND',
        skip,
        limit: 500
      })
      if (!requests.isCurrent(request)) return
      rows.push(...response.data)
      if (response.data.length < 500) break
    }
    candidates.value = rows.filter(
      (row) =>
        row.currency === props.event.currency &&
        (row.payment_date || row.ex_date) <= props.event.event_date
    )
  } catch (error) {
    if (requests.isCurrent(request)) {
      loadFailed.value = true
      showApiError(error, '股息候选加载失败')
    }
  } finally {
    if (requests.isCurrent(request)) loading.value = false
  }
})

async function save() {
  if (saving.value || loading.value || loadFailed.value) return
  if (
    remaining.value < -0.000000005 ||
    allocations.value.some((row) => !row.corporate_action_id || !row.amount || row.amount <= 0) ||
    new Set(allocations.value.map((row) => row.corporate_action_id)).size !==
      allocations.value.length
  ) {
    ElMessage.error('请选择不同的股息并填写正数金额，分摊合计不能超过税款')
    return
  }
  saving.value = true
  const request = requests.begin()
  try {
    await api.updateDividendTaxAllocations(props.event.id, {
      allocations: allocations.value.map((row) => ({
        corporate_action_id: row.corporate_action_id!,
        amount: row.amount.toFixed(8)
      }))
    })
    if (requests.isCurrent(request)) {
      ElMessage.success('股息税归属已保存')
      visible.value = false
    }
    try {
      await props.reload()
    } catch (error) {
      showApiError(error, '归属已保存，现金事件列表刷新失败')
    }
  } catch (error) {
    if (requests.isCurrent(request)) showApiError(error, '股息税归属保存失败')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="股息税归属"
    width="640px"
    :close-on-click-modal="false"
    class="account-form-dialog"
    :close-on-press-escape="!saving"
    :show-close="!saving"
    @closed="emit('close')"
  >
    <p>
      {{ formatDate(event.event_date) }} 扣税 {{ formatCurrency(event.amount, event.currency) }}。
      税款已计入当日现金与收益；归属用于证券明细，不改变扣款日期和总额。无法确认的部分可保持待归属。
    </p>
    <div v-loading="loading">
      <el-alert
        v-if="loadFailed"
        type="error"
        title="候选股息加载失败，请关闭后重试"
        :closable="false"
      />
      <div v-for="(row, index) in allocations" :key="index" class="allocation-row">
        <el-select
          v-model="row.corporate_action_id"
          :aria-label="`第 ${index + 1} 行归属股息`"
          filterable
          placeholder="选择实际对应的股息"
        >
          <el-option
            v-for="action in candidates"
            :key="action.id"
            :value="action.id"
            :label="`${action.symbol} ${action.name || ''} · ${formatDate(action.payment_date || action.ex_date)} · ${formatCurrency(action.total_dividend, action.currency)}`"
          />
        </el-select>
        <el-input-number
          v-model="row.amount"
          :aria-label="`第 ${index + 1} 行股息税分摊金额`"
          :min="0"
          :precision="8"
          controls-position="right"
        />
        <el-button
          text
          type="danger"
          :aria-label="`移除第 ${index + 1} 行股息税分摊`"
          @click="allocations.splice(index, 1)"
          >移除</el-button
        >
      </div>
      <el-button
        :disabled="loading || loadFailed"
        @click="allocations.push({ corporate_action_id: undefined, amount: 0 })"
      >
        添加分摊
      </el-button>
      <p>待归属：{{ formatCurrency(remaining, event.currency) }}</p>
    </div>
    <template #footer>
      <el-button :disabled="saving" @click="visible = false">取消</el-button>
      <NButton
        type="primary"
        :loading="saving"
        :disabled="loading || loadFailed"
        :aria-disabled="saving || loading || loadFailed"
        :aria-busy="saving"
        aria-label="保存归属"
        @click="save"
        >保存归属</NButton
      >
    </template>
  </el-dialog>
</template>

<style scoped>
.allocation-row {
  display: flex;
  gap: 8px;
  margin: 12px 0;
  flex-wrap: wrap;
}
.allocation-row .el-select {
  flex: 1;
  min-width: 240px;
}
</style>

<style scoped>
.el-button + .n-button {
  margin-left: 12px;
}
@media (max-width: 640px) {
  .n-button {
    min-height: 44px;
  }
}
</style>
