<template>
  <span class="dual-currency">
    <span class="cny-amount" :class="{ 'highlight': highlightCurrency === 'CNY' }">
      {{ formatCurrency(amountCny, 'CNY', precision) }}
    </span>
    <span class="separator"> / </span>
    <span class="usd-amount" :class="{ 'highlight': highlightCurrency === 'USD' }">
      {{ formatCurrency(amountUsd, 'USD', precision) }}
    </span>
  </span>
</template>

<script setup>
import { formatCurrency } from '@/utils/currency'

const props = defineProps({
  amountCny: {
    type: Number,
    default: 0
  },
  amountUsd: {
    type: Number,
    default: 0
  },
  precision: {
    type: Number,
    default: 2
  },
  highlightCurrency: {
    type: String,
    default: null // 'CNY' 或 'USD' 或 null
  }
})
</script>

<style scoped>
.dual-currency {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.cny-amount,
.usd-amount {
  transition: all 0.3s ease;
}

.highlight {
  font-weight: bold;
  font-size: 1.1em;
}

.separator {
  color: #909399;
  font-size: 0.9em;
}

.usd-amount {
  color: #606266;
  font-size: 0.95em;
}

.highlight.usd-amount {
  color: #303133;
}
</style>
