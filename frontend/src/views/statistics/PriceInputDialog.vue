<script setup lang="ts">
import { formatPrice, formatQuantity } from '@/utils/helpers'
import type { PriceInputsFeature } from './usePriceInputs'

defineProps<{
  prices: PriceInputsFeature
  loading: boolean
}>()

defineEmits<{ calculate: [] }>()
</script>

<template>
  <!-- 价格输入对话框：每个标的一行（多账户持仓已合并），价格为原币 -->
  <el-dialog v-model="prices.state.dialogVisible" title="输入当前价格" width="760px">
    <p class="price-dialog-note">
      「计算」为手工价试算：只影响本页的业绩与 TTWR 指标，不写入持仓；「保存价格」才会写入。
      空价行已按服务端估值价（含历史收盘）预填。
    </p>
    <div class="responsive-table">
      <el-table :data="prices.state.rows" row-key="key" max-height="560" stripe v-loading="loading">
        <el-table-column prop="symbol" label="代码" width="100" />
        <el-table-column prop="name" label="名称" min-width="130" show-overflow-tooltip />
        <el-table-column prop="market" label="市场" width="90" />
        <el-table-column label="持仓数量" width="120" align="right">
          <template #default="{ row }">
            {{ formatQuantity(row.quantity) }}
          </template>
        </el-table-column>
        <el-table-column label="平均成本" width="130" align="right">
          <template #default="{ row }">
            {{ formatPrice(row.avg_cost) }}
            <span class="currency-code">{{ row.currency }}</span>
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
.price-dialog-note {
  margin: 0 0 12px;
  color: var(--app-text-soft);
  font-size: 12px;
}

.currency-code {
  margin-left: 4px;
  color: var(--app-text-soft);
  font-size: 12px;
}

@media (max-width: 900px) {
  .price-dialog-footer {
    grid-template-columns: 1fr;
  }
}
</style>
