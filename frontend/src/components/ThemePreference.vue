<script setup lang="ts">
import { NButton, NDropdown, NIcon } from 'naive-ui'
import { Monitor, Moon, Sun } from '@lucide/vue'
import { computed } from 'vue'
import { useTheme, type ThemePreference } from '@/styles/theme'

const theme = useTheme()
const options = [
  { label: '跟随系统', key: 'system' },
  { label: '浅色', key: 'light' },
  { label: '深色', key: 'dark' }
]
const label = computed(() => options.find((option) => option.key === theme.preference.value)!.label)
const icon = computed(() =>
  theme.preference.value === 'system' ? Monitor : theme.preference.value === 'dark' ? Moon : Sun
)
</script>

<template>
  <div class="theme-preference">
    <NDropdown
      :options="options"
      trigger="click"
      @select="theme.setPreference($event as ThemePreference)"
    >
      <NButton quaternary :aria-label="`外观：${label}`" class="theme-trigger">
        <template #icon
          ><NIcon><component :is="icon" /></NIcon
        ></template>
        <span class="theme-label">{{ label }}</span>
      </NButton>
    </NDropdown>
  </div>
</template>

<style scoped>
.theme-preference {
  margin-left: auto;
  flex-shrink: 0;
}
.theme-trigger {
  min-height: 44px;
}
</style>
