<script setup lang="ts">
import { formatNumber } from '@/utils/helpers'
import type { PriceInputsFeature } from './usePriceInputs'

defineProps<{
  prices: PriceInputsFeature
  loading: boolean
}>()

defineEmits<{ calculate: [] }>()
</script>

<template>
  <!-- 价格输入对话框 -->
  <el-dialog v-model="prices.state.dialogVisible" title="输入当前价格" width="720px">
    <div class="responsive-table">
      <el-table :data="prices.state.rows" max-height="560" stripe v-loading="loading">
        <el-table-column prop="symbol" label="代码" width="100" />
        <el-table-column prop="name" label="名称" min-width="130" show-overflow-tooltip />
        <el-table-column prop="market" label="市场" width="100" />
        <el-table-column label="持仓数量" width="120" align="right">
          <template #default="{ row }">
            {{ formatNumber(row.quantity, 2) }}
          </template>
        </el-table-column>
        <el-table-column label="平均成本" width="120" align="right">
          <template #default="{ row }">
            {{ formatNumber(row.avg_cost, 4) }}
          </template>
        </el-table-column>
        <el-table-column label="当前价格" width="180">
          <template #default="{ row }">
            <el-input-number v-model="row.current_price" :min="0" :precision="4" size="small" />
          </template>
        </el-table-column>
      </el-table>
    </div>

    <template #footer>
      <div class="mobile-dialog-footer price-dialog-footer">
        <el-button @click="prices.state.dialogVisible = false">取消</el-button>
        <el-button @click="prices.savePrices" :loading="prices.state.saving">保存价格</el-button>
        <el-button type="primary" @click="$emit('calculate')" :loading="loading">计算</el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
@media (max-width: 900px) {
  .price-dialog-footer {
    grid-template-columns: 1fr;
  }
}
</style>
