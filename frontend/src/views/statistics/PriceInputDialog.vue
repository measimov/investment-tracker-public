<script setup lang="ts">
import { NButton, NInputNumber, NModal } from 'naive-ui'
import { formatPrice, formatQuantity } from '@/utils/helpers'
import type { PriceInputsFeature } from './usePriceInputs'
defineProps<{ prices: PriceInputsFeature; loading: boolean }>()
defineEmits<{ calculate: [] }>()
</script>
<template>
  <NModal
    v-model:show="prices.state.dialogVisible"
    preset="card"
    title="输入现价"
    role="dialog"
    aria-label="输入现价"
    :mask-closable="false"
    close-focusable
    :style="{ width: 'min(760px,calc(100vw - 32px))' }"
    :content-style="{ maxHeight: 'calc(100dvh - 220px)', overflow: 'auto' }"
    class="statistics-price-dialog"
  >
    <p class="price-dialog-note">
      「计算」为手工价试算：只影响本页的业绩与 TTWR
      指标，不写入持仓；「保存价格」才会写入。空价行已按服务端估值价（含历史收盘）预填。
    </p>
    <div class="price-rows" :aria-busy="loading">
      <div v-for="row in prices.state.rows" :key="row.key" class="price-row">
        <div class="price-security">
          <strong>{{ row.symbol }}</strong
          ><span>{{ row.name }}</span
          ><small>{{ row.market }} · {{ row.currency }}</small>
        </div>
        <div class="price-holding">
          <span>持仓 {{ formatQuantity(row.quantity) }}</span
          ><span>平均成本 {{ formatPrice(row.avg_cost) }} {{ row.currency }}</span>
        </div>
        <label class="price-input"
          ><span>现价 · {{ row.currency }}</span
          ><NInputNumber
            v-model:value="row.current_price"
            :min="0"
            :precision="4"
            :show-button="false"
            :clearable="false"
            :input-props="{
              'aria-label': `${row.symbol} ${row.market} 现价，${row.currency}`
            }"
        /></label>
      </div>
    </div>
    <template #footer
      ><div class="price-dialog-actions">
        <NButton @click="prices.state.dialogVisible = false">取消</NButton
        ><NButton @click="prices.savePrices" :loading="prices.state.saving">保存价格</NButton
        ><NButton type="primary" @click="$emit('calculate')" :loading="loading">计算</NButton>
      </div></template
    >
  </NModal>
</template>
<style>
.statistics-price-dialog .price-dialog-note {
  margin: 0 0 16px;
  font-size: 12px;
  color: var(--app-text-muted);
  line-height: 1.8;
}
.statistics-price-dialog .price-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 180px;
  gap: 16px;
  align-items: center;
  padding: 16px 0;
  border-top: 1px solid var(--app-border-soft);
}
.statistics-price-dialog .price-security,
.statistics-price-dialog .price-holding,
.statistics-price-dialog .price-input {
  display: grid;
  gap: 6px;
  min-width: 0;
}
.statistics-price-dialog .price-security span {
  font-size: 13px;
  overflow-wrap: anywhere;
}
.statistics-price-dialog .price-security small,
.statistics-price-dialog .price-holding,
.statistics-price-dialog .price-input > span {
  font-size: 12px;
  color: var(--app-text-muted);
  line-height: 1.6;
}
.statistics-price-dialog .price-dialog-actions {
  display: flex;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 8px;
}
@media (max-width: 640px) {
  .statistics-price-dialog .price-row {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  }
  .statistics-price-dialog .price-input {
    grid-column: span 2;
  }
  .statistics-price-dialog .n-input,
  .statistics-price-dialog .n-button {
    min-height: 44px;
  }
}
</style>
