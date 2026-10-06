<script setup lang="ts">
import type { Component } from 'vue'
import { NIcon, NTooltip } from 'naive-ui'

interface NavItem {
  path: string
  label: string
  icon: Component
  adminOnly?: boolean
}
defineProps<{
  primaryItems: NavItem[]
  moreItems: NavItem[]
  activePath: string
  collapsed?: boolean
}>()
const emit = defineEmits<{ navigate: [] }>()
function handleLink(event: MouseEvent) {
  if (event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey)
    emit('navigate')
}
</script>

<template>
  <nav aria-label="主导航" class="app-navigation" :class="{ 'is-collapsed': collapsed }">
    <section
      v-for="group in [
        { title: '常用', items: primaryItems },
        { title: '其他', items: moreItems.filter((item) => !item.adminOnly) },
        { title: '管理员', items: moreItems.filter((item) => item.adminOnly) }
      ]"
      v-show="group.items.length"
      :key="group.title"
      :aria-label="group.title"
      class="nav-group"
    >
      <div class="nav-group-title" :aria-hidden="collapsed">{{ group.title }}</div>
      <NTooltip
        v-for="item in group.items"
        :key="item.path"
        placement="right"
        :disabled="!collapsed"
        :delay="200"
      >
        <template #trigger>
          <router-link
            :to="item.path"
            tabindex="0"
            class="nav-link"
            :class="{ active: activePath === item.path }"
            :aria-label="item.label"
            :aria-current="activePath === item.path ? 'page' : undefined"
            @click="handleLink"
          >
            <NIcon :size="18" aria-hidden="true"><component :is="item.icon" /></NIcon>
            <span class="nav-label" :aria-hidden="collapsed">{{ item.label }}</span>
          </router-link>
        </template>
        {{ item.label }}
      </NTooltip>
    </section>
  </nav>
</template>

<style scoped>
.app-navigation {
  padding: 8px 4px;
}
.nav-group + .nav-group {
  margin-top: 12px;
}
.nav-group-title {
  height: 24px;
  padding-left: var(--app-navigation-icon-column);
  color: var(--app-text-muted);
  font-size: 12px;
  white-space: nowrap;
  transition: opacity var(--app-navigation-label-duration) var(--apple-ease);
}
.nav-link {
  display: grid;
  grid-template-columns: var(--app-navigation-icon-column) minmax(0, 1fr);
  align-items: center;
  min-height: 44px;
  padding: 8px 0;
  border-radius: var(--app-radius-inner);
  color: var(--app-text-muted);
  text-decoration: none;
  font-size: 14px;
  overflow: hidden;
}
.nav-link > .n-icon {
  justify-self: center;
}
.nav-label {
  white-space: nowrap;
  transition: opacity var(--app-navigation-label-duration) var(--apple-ease);
}
.is-collapsed .nav-label,
.is-collapsed .nav-group-title {
  opacity: 0;
  pointer-events: none;
}
.nav-link:hover {
  background: var(--app-hover);
  color: var(--app-text);
}
.nav-link.active {
  color: var(--app-primary-strong);
  background: var(--app-primary-soft);
  font-weight: 600;
}
.nav-link:focus-visible {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: -2px;
}
@media (min-width: 1025px) and (pointer: fine) {
  .nav-link {
    min-height: 40px;
  }
}
</style>
