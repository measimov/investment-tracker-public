<script setup lang="ts">
import { computed, type CSSProperties } from 'vue'
import { EMPTY, formatNumber } from '@/utils/helpers'

const props = defineProps<{
  title: string
  value?: number | string | null
  precision?: number
  formatter?: (value: number | string | null | undefined) => string
  valueStyle?: CSSProperties
}>()
const missing = computed(() => formatNumber(props.value) === EMPTY)
const formatValue = () =>
  missing.value
    ? EMPTY
    : props.formatter
      ? props.formatter(props.value)
      : formatNumber(props.value, props.precision ?? 2)
</script>

<template>
  <div class="financial-statistic">
    <span class="financial-statistic-label">{{ title }}</span>
    <strong class="financial-statistic-value" :style="missing ? undefined : valueStyle">{{
      formatValue()
    }}</strong>
  </div>
</template>

<style scoped>
.financial-statistic {
  display: grid;
  gap: 8px;
  min-width: 0;
}
.financial-statistic-label {
  color: var(--app-text-muted);
  font-size: 13px;
  line-height: 1.5;
}
.financial-statistic-value {
  color: var(--app-text);
  font-size: var(--app-number-secondary);
  font-weight: 600;
  line-height: 1.35;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
</style>
