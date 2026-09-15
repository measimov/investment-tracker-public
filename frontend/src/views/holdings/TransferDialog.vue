<script setup lang="ts">
import type { TransferFeature } from './useTransfer'

defineProps<{ transfer: TransferFeature }>()
</script>

<template>
  <el-dialog v-model="transfer.state.visible" title="账户间转仓" width="420px">
    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="转仓把持仓从一个账户移到另一个，成本基础跟随迁移，不产生盈亏或现金流。"
      style="margin-bottom: 16px"
    />
    <el-form v-if="transfer.state.form" label-position="top">
      <el-form-item label="证券">
        <el-input
          :model-value="`${transfer.state.form.symbol}（${transfer.state.form.market}）`"
          disabled
        />
      </el-form-item>
      <el-form-item label="转出账户">
        <el-input
          :model-value="transfer.accountLabel(transfer.state.form.from_broker_account_id)"
          disabled
        />
      </el-form-item>
      <el-form-item label="转入账户" required>
        <el-select v-model="transfer.state.form.to_broker_account_id" placeholder="请选择转入账户">
          <el-option
            v-if="transfer.state.form.from_broker_account_id !== null"
            label="未指定账户"
            value="unassigned"
          />
          <el-option
            v-for="account in transfer.targetAccounts"
            :key="account.id"
            :label="account.account_name"
            :value="account.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item :label="`数量（可转 ${transfer.state.form.max_quantity}）`" required>
        <el-input-number
          v-model="transfer.state.form.quantity"
          :min="0.00000001"
          :max="transfer.state.form.max_quantity"
          :precision="8"
        />
      </el-form-item>
      <el-form-item label="转仓日期" required>
        <el-date-picker
          v-model="transfer.state.form.transfer_date"
          type="date"
          value-format="YYYY-MM-DD"
        />
      </el-form-item>
      <el-form-item label="备注">
        <el-input v-model="transfer.state.form.notes" type="textarea" :rows="2" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="transfer.state.visible = false">取消</el-button>
      <el-button type="primary" :loading="transfer.state.submitting" @click="transfer.submit">
        确认转仓
      </el-button>
    </template>
  </el-dialog>
</template>
