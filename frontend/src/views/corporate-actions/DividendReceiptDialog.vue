<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NAlert, NButton, NCheckbox, NCheckboxGroup, NEmpty, NModal, NSpin } from 'naive-ui'
import { useLatestRequest } from '@/composables/useLatestRequest'
import { getApiErrorMessage } from '@/utils/apiErrors'
import api from '@/api'
import type { CorporateAction, DividendSuggestion } from '@/types'
import { formatCurrency, formatDate } from '@/utils/helpers'
import { showApiError } from '@/utils/showApiError'
import { dividendReviewReason } from './shared'

const props = defineProps<{ row: DividendSuggestion | null }>()
const emit = defineEmits<{ close: []; saved: [] }>()
const candidates = ref<CorporateAction[]>([])
const selected = ref<number[]>([])
const complete = ref(false)
const loading = ref(false)
const saving = ref(false)
watch(selected, (ids) => {
  if (!ids.length) complete.value = false
})
const candidatesLoaded = ref(false)
const loadError = ref('')
const completeDisabled = computed(
  () => !selected.value.length || loading.value || !!loadError.value
)
const candidatesRequest = useLatestRequest()
async function loadCandidates(row: DividendSuggestion) {
  const token = candidatesRequest.begin()
  loading.value = true
  loadError.value = ''
  candidatesLoaded.value = false
  try {
    const response = await api.getDividendReceiptCandidates(row.id)
    if (candidatesRequest.isCurrent(token) && props.row?.id === row.id) {
      candidates.value = response.data
      candidatesLoaded.value = true
    }
  } catch (error) {
    if (candidatesRequest.isCurrent(token) && props.row?.id === row.id) {
      loadError.value = getApiErrorMessage(error, '加载到账记录失败')
      showApiError(error, '加载到账记录失败')
    }
  } finally {
    if (candidatesRequest.isCurrent(token)) loading.value = false
  }
}
watch(
  () => props.row,
  (row) => {
    candidatesRequest.invalidate()
    candidates.value = []
    candidatesLoaded.value = false
    loadError.value = ''
    loading.value = false
    if (!row) return
    selected.value = [...(row.receipt_ids || [])]
    complete.value = row.receipt_complete || false
    loadCandidates(row)
  }
)
async function save() {
  if (!props.row || loading.value || loadError.value) return
  saving.value = true
  try {
    await api.updateDividendReceipts(props.row.id, {
      receipt_ids: selected.value,
      receipt_complete: complete.value,
      expected_updated_at: props.row.updated_at
    })
    emit('saved')
    emit('close')
  } catch (error) {
    showApiError(error, '核对到账关系失败')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <NModal
    :show="!!row"
    preset="card"
    title="核对股息到账"
    aria-label="核对股息到账"
    :style="{ width: 'min(620px, calc(100vw - 32px))' }"
    :content-style="{ maxHeight: 'calc(100dvh - 220px)', overflow: 'auto' }"
    :mask-closable="false"
    close-focusable
    class="dividend-receipt-dialog"
    @update:show="
      (value) => {
        if (!value) emit('close')
      }
    "
  >
    <p>{{ row?.symbol }} · 除息日 {{ formatDate(row?.ex_date || '') }}</p>
    <NAlert
      title="选择属于本次公告的实际到账记录。这里只关联凭证，不修改金额、税款或买入成本。"
      type="info"
      :closable="false"
    />
    <p v-if="row?.completion_source === 'statement'" class="receipt-note">
      对账单已自动核对完成，无需再次人工确认。只有需要调整关联或改为人工确认时才需保存。
    </p>
    <p v-else-if="row?.completion_source === 'manual'" class="receipt-note">
      本次派息已人工确认收齐。
    </p>
    <p v-else-if="row?.review_reason" class="receipt-note">
      {{ dividendReviewReason(row.review_reason) }}
    </p>
    <NAlert v-if="loadError" type="error" class="receipt-error" data-testid="receipt-load-error"
      >到账候选加载失败，暂不能确认或保存关联。<NButton
        :loading="loading"
        @click="row && loadCandidates(row)"
        >重试到账候选</NButton
      ></NAlert
    >
    <p v-if="loading" role="status">正在加载实际到账候选…</p>
    <NSpin :show="loading"
      ><div class="receipt-options">
        <NCheckboxGroup v-model:value="selected">
          <NCheckbox v-for="receipt in candidates" :key="receipt.id" :value="receipt.id">
            #{{ receipt.id }} · {{ formatDate(receipt.payment_date || receipt.ex_date) }} ·
            {{
              formatCurrency(
                Number(
                  receipt.net_dividend ??
                    Number(receipt.total_dividend || 0) - Number(receipt.tax_withheld || 0)
                ),
                receipt.currency
              )
            }}
            {{ receipt.amount_basis === 'NET_ONLY' ? '（税前及税额待核实）' : '' }}
          </NCheckbox>
        </NCheckboxGroup>
        <NEmpty
          v-if="candidatesLoaded && !loadError && !candidates.length"
          description="暂无可关联到账。先导入券商凭证，或在记录页明确确认实际到账；公告账户不明确时先修正权益日账本并重新同步。"
        /></div
    ></NSpin>
    <NCheckbox
      v-model:checked="complete"
      :disabled="completeDisabled"
      :aria-disabled="completeDisabled"
      >我已核对，本次公告股息已全部到账</NCheckbox
    >
    <p class="receipt-note">
      分次到账可保持未勾选；缺少原币、换汇或税前金额依据时，需核实后人工确认收齐。取消关联不会删除真实现金记录。
    </p>
    <template #footer
      ><div class="receipt-dialog-actions">
        <NButton @click="emit('close')">取消</NButton>
        <NButton
          type="primary"
          :loading="saving"
          :disabled="loading || !!loadError || (!selected.length && complete)"
          @click="save"
          >保存核对</NButton
        >
      </div></template
    >
  </NModal>
</template>

<style>
.dividend-receipt-dialog .receipt-dialog-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}
.dividend-receipt-dialog .receipt-error {
  margin: 12px 0;
}
.dividend-receipt-dialog .receipt-error .n-button {
  margin: 8px 0;
}
.dividend-receipt-dialog p {
  line-height: 1.8;
  overflow-wrap: anywhere;
}
.dividend-receipt-dialog .receipt-options {
  margin: 16px 0;
  max-height: 320px;
  overflow: auto;
}
.dividend-receipt-dialog .receipt-options .n-checkbox {
  display: flex;
  height: auto;
  margin: 12px 0;
}
.dividend-receipt-dialog .receipt-options .n-checkbox__label {
  white-space: normal;
}
.dividend-receipt-dialog .receipt-note {
  color: var(--app-text-muted);
  line-height: 1.6;
}
@media (max-width: 640px) {
  .dividend-receipt-dialog .n-checkbox,
  .dividend-receipt-dialog .n-button {
    min-height: 44px;
  }
}
</style>
