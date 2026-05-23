<template>
  <div class="mock-hint">
    <p class="mock-hint-title">🧪 Mock 模式测试账号</p>
    <div class="mock-accounts">
      <button class="mock-account" @click="$emit('fill-account', 'admin', 'admin123')">
        <span class="mock-role admin">管理员</span>
        <span class="mock-cred">admin / admin123</span>
      </button>
      <button class="mock-account" @click="$emit('fill-account', 'measimov', 'user123')">
        <span class="mock-role user">普通用户</span>
        <span class="mock-cred">measimov / user123</span>
      </button>
      <button class="mock-account" @click="$emit('fill-account', 'demo', 'demo123')">
        <span class="mock-role user">演示用户</span>
        <span class="mock-cred">demo / demo123</span>
      </button>
    </div>
  </div>
</template>

<script setup>
defineEmits(['fill-account'])
</script>

<style scoped>
.mock-hint {
  margin-top: 20px;
  padding: 14px;
  background: var(--app-surface-secondary);
  border-radius: var(--app-radius-inner);
}

.mock-hint-title {
  margin: 0 0 10px;
  font-size: 12px;
  font-weight: 500;
  color: var(--app-text-muted);
}

.mock-accounts {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.mock-account {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-sm);
  cursor: pointer;
  transition: all var(--app-duration) var(--apple-ease);
  font-family: inherit;
  font-size: 13px;
}

.mock-account:hover {
  border-color: var(--app-primary);
  box-shadow: 0 0 0 3px var(--app-primary-soft);
}

.mock-role {
  display: inline-flex;
  padding: 2px 8px;
  border-radius: 980px;
  font-size: 11px;
  font-weight: 600;
  flex-shrink: 0;
}

.mock-role.admin {
  background: var(--app-primary-soft);
  color: var(--app-primary);
}

.mock-role.user {
  background: var(--app-success-soft);
  color: var(--app-success);
}

.mock-cred {
  color: var(--app-text-muted);
  font-family: 'SF Mono', ui-monospace, monospace;
  font-size: 12px;
}
</style>
