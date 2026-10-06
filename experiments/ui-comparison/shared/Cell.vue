<script setup lang="ts">
import {
  accountName,
  formatCurrency,
  formatDate,
  formatPercent,
  formatPrice,
  formatQuantity,
  type SampleRow
} from './model'
defineProps<{ row: SampleRow; kind: 'name' | 'quantity' | 'price' | 'value' | 'profit' }>()
</script>
<template>
  <div v-if="kind === 'name'" class="cell-name">
    <span class="security-name">{{ row.name }}</span>
    <div class="sub">{{ row.symbol }} · {{ accountName(row.account) }}</div>
    <div v-if="row.note" class="row-note">{{ row.note }}</div>
  </div>
  <div v-else-if="kind === 'quantity'" class="financial-cell">
    <div>{{ formatQuantity(row.quantity) }}</div>
    <div class="sub">均价 {{ formatPrice(row.cost) }}</div>
  </div>
  <div v-else-if="kind === 'price'" class="financial-cell" :data-testid="`price-${row.id}`">
    <div>{{ formatPrice(row.price) }}</div>
    <div class="sub">{{ row.currency }} · {{ formatDate(row.date) }}</div>
  </div>
  <div v-else-if="kind === 'value'" class="financial-cell">
    <div>{{ formatCurrency(row.value, row.currency) }}</div>
    <div class="sub">折 CNY {{ formatCurrency(row.valueCNY) }}</div>
  </div>
  <div
    v-else
    class="financial-cell"
    :class="row.profit === null ? '' : row.profit >= 0 ? 'positive' : 'negative'"
  >
    <div>{{ formatCurrency(row.profit, row.currency) }}</div>
    <div class="sub">{{ formatPercent(row.rate) }}</div>
  </div>
</template>
