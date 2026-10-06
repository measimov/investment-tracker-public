<script setup lang="ts">
import { computed } from 'vue'
import {
  NAlert,
  NButton,
  NDatePicker,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NModal,
  NSelect
} from 'naive-ui'
import { formatQuantity } from '@/utils/helpers'
import { accountOptionLabel, UNASSIGNED_ACCOUNT, UNASSIGNED_ACCOUNT_LABEL } from '@/utils/labels'
import type { TransferFeature } from './useTransfer'

const props = defineProps<{ transfer: TransferFeature }>()
const targetOptions = computed(() => [
  ...(props.transfer.state.form?.from_broker_account_id !== null
    ? [{ label: UNASSIGNED_ACCOUNT_LABEL, value: UNASSIGNED_ACCOUNT }]
    : []),
  ...props.transfer.targetAccounts.map((account) => ({
    label: accountOptionLabel(account),
    value: account.id
  }))
])
</script>

<template>
  <NModal
    v-model:show="transfer.state.visible"
    preset="dialog"
    title="账户间转仓"
    aria-label="账户间转仓"
    :mask-closable="false"
    :close-on-esc="!transfer.state.submitting"
    :closable="!transfer.state.submitting"
    style="width: 460px; max-width: calc(100vw - 24px)"
    data-testid="transfer-dialog"
  >
    <NAlert type="info" :show-icon="false" style="margin: 16px 0">
      转仓把持仓从一个账户移到另一个，成本基础跟随迁移，不产生盈亏或现金流。
    </NAlert>
    <NForm v-if="transfer.state.form" label-placement="top" :disabled="transfer.state.submitting">
      <NFormItem label="标的">
        <NInput
          :value="`${transfer.state.form.symbol}（${transfer.state.form.market}）`"
          disabled
          :input-props="{ 'aria-label': '标的' }"
        />
      </NFormItem>
      <NFormItem label="转出账户">
        <NInput
          :value="transfer.accountLabel(transfer.state.form.from_broker_account_id)"
          disabled
          :input-props="{ 'aria-label': '转出账户' }"
        />
      </NFormItem>
      <NFormItem label="转入账户" required>
        <NSelect
          :value="transfer.state.form.to_broker_account_id ?? null"
          :options="targetOptions"
          placeholder="请选择转入账户"
          filterable
          data-testid="transfer-target"
          aria-label="转入账户"
          :input-props="{ 'aria-label': '转入账户' }"
          @update:value="
            (value) => (transfer.state.form!.to_broker_account_id = value ?? undefined)
          "
        />
      </NFormItem>
      <NFormItem
        :label="`数量（可转 ${formatQuantity(transfer.state.form.max_quantity)}）`"
        required
      >
        <NInputNumber
          :value="transfer.state.form.quantity"
          :min="0.00000001"
          :max="transfer.state.form.max_quantity"
          :step="1"
          :input-props="{ 'aria-label': '转仓数量' }"
          @update:value="(value) => (transfer.state.form!.quantity = value ?? 0)"
        />
      </NFormItem>
      <NFormItem label="转仓日期" required>
        <NDatePicker
          v-model:formatted-value="transfer.state.form.transfer_date"
          type="date"
          value-format="yyyy-MM-dd"
          format="yyyy/MM/dd"
          placeholder="转仓日期"
          :clearable="false"
        />
      </NFormItem>
      <NFormItem label="备注">
        <NInput
          v-model:value="transfer.state.form.notes"
          type="textarea"
          :rows="2"
          :input-props="{ 'aria-label': '转仓备注' }"
        />
      </NFormItem>
    </NForm>
    <template #action>
      <NButton :disabled="transfer.state.submitting" @click="transfer.state.visible = false"
        >取消</NButton
      >
      <NButton
        type="primary"
        :loading="transfer.state.submitting"
        :disabled="transfer.state.submitting"
        @click="transfer.submit"
        >确认转仓</NButton
      >
    </template>
  </NModal>
</template>
