<script setup lang="ts">
import { nextTick, ref } from 'vue'
import { NInputNumber } from 'naive-ui'
/**
 * 持仓现价：默认只读，点数值或铅笔进入编辑，回车/失焦保存、Esc 取消（保存作用于该标的全部账户，
 * 所以不做常驻输入框，易误改）。桌面表格与移动卡片共用（#284：此前两份逐字重复）。
 */
import { Pencil as EditPen } from '@lucide/vue'
import { formatPrice } from '@/utils/helpers'
import type { HoldingRow, HoldingsTableFeature } from './useHoldingsTable'

const props = defineProps<{ table: HoldingsTableFeature; row: HoldingRow }>()
const displayButton = ref<HTMLButtonElement | null>(null)

// 进入编辑态即聚焦并全选：点一下就能直接输入新价
const vFocus = {
  mounted(el: HTMLElement) {
    const input = el.querySelector('input')
    input?.focus()
    input?.select()
  }
}

// 回车 = 失焦提交：NInputNumber 在原生 change（失焦前触发）时才回写 v-model，
// 直接在 keydown 里读草稿会拿到旧值
async function submitOnEnter(event: KeyboardEvent) {
  ;(event.target as HTMLElement | null)?.blur?.()
  await nextTick()
  displayButton.value?.focus()
}
async function cancelOnEscape() {
  props.table.cancelPriceEdit()
  await nextTick()
  displayButton.value?.focus()
}
</script>

<template>
  <div
    v-if="table.isEditingPrice(row)"
    class="price-editor"
    @keydown.enter.prevent="submitOnEnter"
    @keydown.esc.prevent="cancelOnEscape"
  >
    <NInputNumber
      v-model:value="table.state.priceDraft"
      v-focus
      :min="0"
      size="small"
      :show-button="false"
      :step="0.0001"
      :input-props="{ 'aria-label': `编辑 ${row.name || row.symbol} 的现价` }"
      data-testid="price-input"
      @blur="table.commitPriceEdit(row)"
    />
  </div>
  <button
    v-else
    ref="displayButton"
    type="button"
    class="price-display"
    :aria-label="`编辑 ${row.name || row.symbol} 的现价`"
    data-testid="price-display"
    @click="table.startPriceEdit(row)"
  >
    <span class="num">{{ formatPrice(table.priceOf(row)) }}</span>
    <EditPen class="price-edit-icon" />
  </button>
</template>

<style scoped>
.price-display {
  min-height: 24px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0;
  border: 0;
  background: none;
  color: inherit;
  font: inherit;
  line-height: 1.35;
  cursor: pointer;
}

.num {
  font-variant-numeric: tabular-nums;
}

.price-edit-icon {
  width: 13px;
  height: 13px;
  color: var(--app-text-soft);
  opacity: 0.55;
}

.price-display:hover .price-edit-icon,
.price-display:focus-visible .price-edit-icon {
  opacity: 1;
  color: var(--app-primary);
}

.price-editor :deep(.n-input-number) {
  width: 112px;
}
@media (max-width: 640px) {
  .price-display {
    min-height: 44px;
    min-width: 44px;
  }
}
</style>
