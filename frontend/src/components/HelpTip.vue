<script setup lang="ts">
import { computed, ref, useId } from 'vue'
import { useElementBounding, useWindowSize } from '@vueuse/core'
import { NPopover } from 'naive-ui'
import { CircleHelp } from '@lucide/vue'

defineProps<{ label: string }>()
const show = ref(false)
const id = useId()
const trigger = ref<HTMLButtonElement | null>(null)
const { left, right } = useElementBounding(trigger)
const { width, height } = useWindowSize()
const alignEnd = computed(() => right.value > width.value - left.value)
const popoverWidth = computed(() =>
  Math.max(1, Math.min(360, (alignEnd.value ? right.value : width.value - left.value) - 24))
)
</script>

<template>
  <NPopover
    v-model:show="show"
    trigger="click"
    :placement="alignEnd ? 'bottom-end' : 'bottom-start'"
    :width="popoverWidth"
    scrollable
    :style="{ maxHeight: `${Math.max(1, height - 48)}px` }"
  >
    <template #trigger>
      <button
        ref="trigger"
        type="button"
        class="help-tip"
        :aria-label="label"
        :aria-expanded="show"
        :aria-controls="show ? id : undefined"
        @keydown.esc.stop="show = false"
      >
        <CircleHelp aria-hidden="true" />
      </button>
    </template>
    <div :id="id" class="help-tip-content"><slot /></div>
  </NPopover>
</template>

<style scoped>
.help-tip {
  display: inline-grid;
  place-items: center;
  flex: 0 0 auto;
  width: 24px;
  height: 24px;
  padding: 4px;
  border: 0;
  background: transparent;
  color: var(--app-text-muted);
  cursor: pointer;
  vertical-align: middle;
}
.help-tip svg {
  width: 14px;
  height: 14px;
}
.help-tip:hover {
  color: var(--app-primary-strong);
}
.help-tip:focus-visible {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: 2px;
}
.help-tip-content {
  color: var(--app-text);
  font-family: var(--app-font-sans);
  font-size: 13px;
  font-weight: 400;
  line-height: 1.7;
  overflow-wrap: anywhere;
}
.help-tip-content :deep(p) {
  margin: 0 0 8px;
}
.help-tip-content :deep(p:last-child) {
  margin-bottom: 0;
}
@media (pointer: coarse) {
  .help-tip {
    width: 44px;
    height: 44px;
    margin-block: -10px;
  }
}
</style>
