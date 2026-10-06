<script setup lang="ts">
/**
 * 期初建仓补录成本（#174；#284 由 RecordsTab 拆出）：导入建的行动整体只读，但成本必须有通道。
 */
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import { useMediaQuery } from '@/composables/useMediaQuery'
import type { CorporateAction } from '@/types'
import { formatQuantity, toNumber } from '@/utils/helpers'
import { showApiError } from '@/utils/showApiError'

const emit = defineEmits<{ saved: [] }>()

const isMobileView = useMediaQuery('(max-width: 640px)')
const costDialogVisible = ref(false)
const costSubmitting = ref(false)
const costForm = reactive<{
  id: number | null
  symbol: string
  quantity: number | null
  cost_per_share: number | null
  total_cost: number | null
  notes: string
}>({ id: null, symbol: '', quantity: null, cost_per_share: null, total_cost: null, notes: '' })

function open(row: CorporateAction) {
  costForm.id = row.id
  costForm.symbol = row.symbol
  costForm.quantity = row.adjusted_quantity ? toNumber(row.adjusted_quantity) : null
  costForm.cost_per_share = row.adjusted_cost_per_share
    ? toNumber(row.adjusted_cost_per_share)
    : null
  costForm.total_cost = row.cost_basis_adjustment ? toNumber(row.cost_basis_adjustment) : null
  costForm.notes = row.notes || ''
  costDialogVisible.value = true
}

async function handleSubmitCost() {
  if (costForm.id === null) return
  if (costForm.cost_per_share === null && costForm.total_cost === null) {
    ElMessage.warning('请填写单位成本或总成本')
    return
  }
  costSubmitting.value = true
  try {
    await api.updateOpeningPositionCost(costForm.id, {
      adjusted_cost_per_share: costForm.cost_per_share,
      cost_basis_adjustment: costForm.total_cost,
      notes: costForm.notes
    })
    ElMessage.success('成本已补录，持仓已重算')
    costDialogVisible.value = false
    emit('saved')
  } catch (error) {
    showApiError(error, { prefix: '补录失败' })
  } finally {
    costSubmitting.value = false
  }
}

defineExpose({ open })
</script>

<template>
  <el-dialog
    v-model="costDialogVisible"
    title="补录期初建仓成本"
    :width="isMobileView ? '95%' : '480px'"
    :fullscreen="isMobileView"
    :close-on-click-modal="false"
  >
    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="cost-dialog-tip"
      :title="`${costForm.symbol} 数量 ${formatQuantity(costForm.quantity)}，来自对账单，不可改`"
      description="填写单位成本或总成本其一即可；两者都填时须一致。保存后持仓与已实现盈亏立即重算。"
    />
    <el-form :model="costForm" label-width="100px" label-position="top">
      <el-form-item label="单位成本">
        <el-input-number v-model="costForm.cost_per_share" :min="0" />
      </el-form-item>
      <el-form-item label="总成本">
        <el-input-number v-model="costForm.total_cost" :min="0" :precision="2" />
      </el-form-item>
      <el-form-item label="备注">
        <el-input v-model="costForm.notes" type="textarea" :rows="2" />
      </el-form-item>
    </el-form>
    <template #footer>
      <div class="mobile-dialog-footer">
        <el-button @click="costDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleSubmitCost" :loading="costSubmitting">
          保存并重算
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.cost-dialog-tip {
  margin-bottom: 16px;
}
</style>
