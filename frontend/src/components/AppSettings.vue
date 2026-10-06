<script setup lang="ts">
import { computed, nextTick, ref, useId, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useWindowSize } from '@vueuse/core'
import { NButton, NPopover } from 'naive-ui'
import { Settings } from '@lucide/vue'
import { useAuthStore } from '@/stores/auth'
import ThemePreference from './ThemePreference.vue'

const props = defineProps<{ collapsed?: boolean }>()
const emit = defineEmits<{ command: [value: 'password' | 'logout'] }>()
const authStore = useAuthStore()
const route = useRoute()
const { width } = useWindowSize()
const show = ref(false)
const id = useId()
const trigger = ref<HTMLButtonElement | null>(null)
const themeControl = ref<HTMLDivElement | null>(null)
const userInitial = computed(() => authStore.user?.username?.trim().charAt(0).toUpperCase() || '?')

function close() {
  show.value = false
  trigger.value?.focus()
}
async function focusSettings() {
  show.value = true
  await nextTick()
  themeControl.value?.querySelector('button')?.focus()
}
function selectCommand(command: 'password' | 'logout') {
  close()
  emit('command', command)
}
watch([() => route.fullPath, () => props.collapsed], () => {
  show.value = false
})
</script>

<template>
  <NPopover
    v-model:show="show"
    trigger="click"
    placement="top-start"
    :width="Math.min(264, width - 24)"
    :to="false"
  >
    <template #trigger>
      <button
        ref="trigger"
        type="button"
        class="settings-trigger"
        :class="{ 'is-collapsed': collapsed }"
        aria-label="设置"
        :aria-expanded="show"
        :aria-controls="show ? id : undefined"
        @keydown.down.prevent="focusSettings"
        @keydown.up.prevent="focusSettings"
        @keydown.esc.stop.prevent="close"
      >
        <Settings :size="20" aria-hidden="true" />
        <span class="settings-label" :aria-hidden="collapsed">设置</span>
      </button>
    </template>
    <div :id="id" class="settings-panel" role="group" aria-label="设置" @keydown.esc.stop="close">
      <div v-if="authStore.isAuthenticated" class="settings-identity">
        <span class="user-avatar" aria-hidden="true">{{ userInitial }}</span>
        <div class="identity-text">
          <strong>{{ authStore.user?.username }}</strong>
          <span>{{ authStore.isAdmin ? '管理员' : '普通用户' }}</span>
        </div>
      </div>
      <div ref="themeControl" class="settings-appearance">
        <span>外观</span>
        <ThemePreference />
      </div>
      <div v-if="authStore.isAuthenticated" class="settings-account">
        <NButton block quaternary @click="selectCommand('password')">修改密码</NButton>
        <NButton block quaternary @click="selectCommand('logout')">退出登录</NButton>
      </div>
    </div>
  </NPopover>
</template>

<style scoped>
.settings-trigger {
  display: grid;
  grid-template-columns: var(--app-navigation-icon-column) minmax(0, 1fr);
  align-items: center;
  width: 100%;
  min-height: 44px;
  padding: 8px 0;
  border: 0;
  border-radius: var(--app-radius-inner);
  background: transparent;
  color: var(--app-text-muted);
  font: inherit;
  text-align: left;
  overflow: hidden;
  cursor: pointer;
}
.settings-trigger > svg {
  justify-self: center;
}
.settings-label {
  white-space: nowrap;
  transition: opacity var(--app-navigation-label-duration) var(--apple-ease);
}
.settings-trigger.is-collapsed .settings-label {
  opacity: 0;
  pointer-events: none;
}
.settings-trigger:hover,
.settings-trigger[aria-expanded='true'] {
  color: var(--app-text);
  background: var(--app-hover);
}
.settings-trigger:focus-visible {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: -2px;
}
.settings-panel {
  display: grid;
  gap: 8px;
}
.settings-identity {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 4px 0 12px;
  border-bottom: 1px solid var(--app-border-soft);
}
.user-avatar {
  display: grid;
  place-items: center;
  flex: 0 0 32px;
  height: 32px;
  border-radius: 50%;
  background: var(--app-primary-fill);
  color: var(--app-on-primary-fill);
  font-weight: 600;
}
.identity-text {
  min-width: 0;
}
.identity-text strong,
.identity-text span {
  display: block;
  overflow-wrap: anywhere;
}
.identity-text span {
  color: var(--app-text-muted);
  font-size: 12px;
}
.settings-appearance {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.settings-account {
  display: grid;
  gap: 4px;
  padding-top: 8px;
  border-top: 1px solid var(--app-border-soft);
}
.settings-account :deep(.n-button) {
  justify-content: flex-start;
  min-height: 44px;
}
</style>
