<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { useElementBounding, useWindowSize } from '@vueuse/core'
import { NPopover } from 'naive-ui'

defineOptions({ inheritAttrs: false })
defineProps<{ label: string; text: string; caption?: string; popover?: boolean }>()
const show = ref(false)
const explanationId = useId()
const trigger = ref<HTMLButtonElement | null>(null)
const { left, right, top, bottom } = useElementBounding(trigger)
const { width, height } = useWindowSize()
const alignEnd = computed(() => right.value > width.value - left.value)
const explanationPlacement = computed(() => (alignEnd.value ? 'bottom-end' : 'bottom-start'))
const explanationWidth = computed(() =>
  Math.max(
    1,
    Math.min(320, width.value - 48, (alignEnd.value ? right.value : width.value - left.value) - 24)
  )
)
const explanationMaxHeight = computed(() =>
  Math.max(1, Math.min(height.value - 48, Math.max(top.value, height.value - bottom.value) - 24))
)
watch([left, right, top, bottom, width, height], () => {
  if (
    show.value &&
    (left.value < 0 || right.value > width.value || top.value < 0 || bottom.value > height.value)
  ) {
    show.value = false
  }
})
</script>
<template>
  <NPopover
    v-if="popover"
    v-model:show="show"
    trigger="click"
    :placement="explanationPlacement"
    :width="explanationWidth"
    scrollable
    :style="{ maxHeight: `${explanationMaxHeight}px` }"
  >
    <template #trigger>
      <button
        ref="trigger"
        v-bind="$attrs"
        type="button"
        class="note-trigger"
        :aria-label="label"
        :aria-expanded="show"
        :aria-controls="show ? explanationId : undefined"
        @keydown.esc.stop="show = false"
      >
        {{ caption || '说明' }}
      </button>
    </template>
    <p :id="explanationId" class="note-explanation">{{ text }}</p>
  </NPopover>
  <details v-else v-bind="$attrs" class="sd-note">
    <summary :aria-label="label">{{ caption || '说明' }}</summary>
    <p>{{ text }}</p>
  </details>
</template>
<style scoped>
.sd-note {
  display: inline-block;
  max-width: 100%;
  font-size: 12px;
  vertical-align: top;
  text-align: left;
}
summary {
  box-sizing: border-box;
  min-height: 24px;
  padding-block: calc((24px - 1.8em) / 2);
  color: var(--app-primary-strong);
  cursor: pointer;
  line-height: 1.8;
}
.note-trigger {
  border: 0;
  padding: 0;
  min-height: 24px;
  background: transparent;
  color: var(--app-primary-strong);
  font: inherit;
  font-size: 12px;
  line-height: 1.8;
  cursor: pointer;
}
.note-trigger:focus-visible {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: 3px;
}
.note-explanation {
  margin: 0;
  font-size: 13px;
  line-height: 1.8;
  color: var(--app-text-muted);
  overflow-wrap: anywhere;
}
p {
  margin: 6px 0;
  line-height: 1.8;
  color: var(--app-text-muted);
  white-space: normal;
  overflow-wrap: anywhere;
}
@media (max-width: 640px) {
  .note-trigger {
    min-height: 44px;
  }
  summary {
    min-height: 44px;
    padding-block: calc((44px - 1.8em) / 2);
  }
}
</style>
