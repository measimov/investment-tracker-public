<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NButton, NDatePicker, NInput, NModal } from 'naive-ui'
import { formatLocalDate, parseLocalDate } from '@/utils/dateRange'
const props = defineProps<{ show: boolean; dateRange: [string, string] | null; title: string }>()
const emit = defineEmits<{
  'update:show': [value: boolean]
  apply: [value: [string, string] | null]
}>()
const draftStart = ref('')
const draftEnd = ref('')
watch(
  () => props.show,
  (show) => {
    if (!show) return
    draftStart.value = props.dateRange?.[0] ?? ''
    draftEnd.value = props.dateRange?.[1] ?? ''
  },
  { immediate: true }
)
const draftRange = computed<[string, string] | null>(() => {
  const validDate = (value: string) =>
    /^\d{4}-\d{2}-\d{2}$/.test(value) && formatLocalDate(parseLocalDate(value)) === value
  return validDate(draftStart.value) &&
    validDate(draftEnd.value) &&
    draftStart.value <= draftEnd.value
    ? [draftStart.value, draftEnd.value]
    : null
})
function updateDateDraft(value: string | [string, string] | null) {
  draftStart.value = Array.isArray(value) ? value[0] : ''
  draftEnd.value = Array.isArray(value) ? value[1] : ''
}
function applyDateRange(clear: boolean) {
  if (!clear && !draftRange.value) return
  emit('apply', clear ? null : draftRange.value)
}
</script>
<template>
  <NModal
    :show="show"
    @update:show="emit('update:show', $event)"
    preset="card"
    :title="title"
    role="dialog"
    :aria-label="title"
    class="date-range-dialog"
    :style="{ width: 'min(640px, calc(100vw - 32px))' }"
    :content-style="{ padding: '12px', maxHeight: 'calc(100dvh - 180px)', overflow: 'auto' }"
  >
    <div class="date-draft-inputs">
      <NInput
        v-model:value="draftStart"
        placeholder="开始日期"
        :input-props="{ 'aria-label': '开始日期' }"
      />
      <span>至</span>
      <NInput
        v-model:value="draftEnd"
        placeholder="结束日期"
        :input-props="{ 'aria-label': '结束日期' }"
      />
    </div>
    <p v-if="draftStart && draftEnd && !draftRange" class="date-error" role="alert">
      请输入完整有效的日期范围，开始日期不能晚于结束日期。
    </p>
    <NDatePicker
      panel
      type="daterange"
      :formatted-value="draftRange"
      value-format="yyyy-MM-dd"
      :actions="[]"
      :theme-overrides="{
        itemCellWidth: '32px',
        itemCellHeight: '36px',
        arrowColor: 'var(--app-text-muted)'
      }"
      @update:formatted-value="updateDateDraft"
    />
    <template #footer
      ><div class="date-dialog-actions">
        <NButton @click="applyDateRange(true)">清除范围</NButton
        ><NButton @click="emit('update:show', false)">取消</NButton
        ><NButton type="primary" :disabled="!draftRange" @click="applyDateRange(false)"
          >应用范围</NButton
        >
      </div></template
    >
  </NModal>
</template>
<style>
.date-range-dialog .date-draft-inputs {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.date-range-dialog .date-draft-inputs .n-input__input-el {
  font-variant-numeric: tabular-nums;
}
.date-range-dialog .date-draft-inputs .n-input {
  min-width: 0;
}
.date-range-dialog .date-dialog-actions {
  display: flex;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 8px;
}
.date-range-dialog .date-error {
  color: var(--app-danger-text);
  font-size: 13px;
}
.date-range-dialog .n-date-panel {
  box-shadow: none;
  max-width: 100%;
  margin-inline: auto;
}
@media (max-width: 700px) {
  .date-range-dialog .n-date-panel--daterange {
    grid-template-areas: 'left-calendar' 'right-calendar' 'footer' 'action';
    grid-template-columns: minmax(0, 1fr);
  }
  .date-range-dialog .n-date-panel__vertical-divider {
    display: none;
  }
  .date-range-dialog .n-date-panel-calendar {
    padding: 6px 0 4px;
  }
  .date-range-dialog .n-button,
  .date-range-dialog .n-input {
    min-height: 44px;
  }
}
</style>
