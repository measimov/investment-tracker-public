<template>
  <el-container class="app-container">
    <el-header class="app-header">
      <div class="header-content">
        <router-link to="/" class="brand-link">
          <span class="brand-mark" aria-hidden="true">
            <span></span>
            <span></span>
            <span></span>
          </span>
          <span class="brand-title">投资追踪系统</span>
        </router-link>
        <el-button
          v-if="authStore.isAuthenticated"
          class="mobile-nav-button"
          :icon="Menu"
          circle
          aria-label="打开导航"
          @click="mobileNavVisible = true"
        />
        <el-menu
          v-if="authStore.isAuthenticated"
          :default-active="activeMenu"
          mode="horizontal"
          router
          class="header-menu"
        >
          <el-menu-item index="/">
            <el-icon><DataBoard /></el-icon>
            <span>仪表盘</span>
          </el-menu-item>
          <el-menu-item index="/transactions">
            <el-icon><List /></el-icon>
            <span>交易记录</span>
          </el-menu-item>
          <el-menu-item index="/corporate-actions">
            <el-icon><DocumentCopy /></el-icon>
            <span>公司行动</span>
          </el-menu-item>
          <el-menu-item index="/holdings">
            <el-icon><Wallet /></el-icon>
            <span>当前持仓</span>
          </el-menu-item>
          <el-menu-item index="/statistics">
            <el-icon><TrendCharts /></el-icon>
            <span>统计分析</span>
          </el-menu-item>
          <el-menu-item index="/exchange-rates">
            <el-icon><Money /></el-icon>
            <span>汇率管理</span>
          </el-menu-item>
          <!-- 管理员菜单 -->
          <el-menu-item v-if="authStore.isAdmin" index="/admin/holdings">
            <el-icon><Odometer /></el-icon>
            <span>查看所有持仓</span>
          </el-menu-item>
          <el-menu-item v-if="authStore.isAdmin" index="/admin/users">
            <el-icon><User /></el-icon>
            <span>用户管理</span>
          </el-menu-item>
        </el-menu>
        <div v-if="authStore.isAuthenticated" class="user-info">
          <el-dropdown @command="handleUserCommand">
            <span class="user-dropdown">
              <el-icon><User /></el-icon>
              <span class="username">{{ authStore.user?.username }}</span>
              <el-icon class="el-icon--right"><ArrowDown /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item disabled>
                  <el-tag v-if="authStore.isAdmin" type="danger" size="small">管理员</el-tag>
                  <el-tag v-else type="info" size="small">普通用户</el-tag>
                </el-dropdown-item>
                <el-dropdown-item divided command="logout">
                  <el-icon><SwitchButton /></el-icon>
                  退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>
      <el-drawer
        v-model="mobileNavVisible"
        class="mobile-nav-drawer"
        direction="rtl"
        size="82%"
        :with-header="false"
      >
        <div class="mobile-nav-panel">
          <div class="mobile-nav-user">
            <div>
              <div class="mobile-nav-name">{{ authStore.user?.username }}</div>
              <el-tag v-if="authStore.isAdmin" type="danger" size="small">管理员</el-tag>
              <el-tag v-else type="info" size="small">普通用户</el-tag>
            </div>
            <el-button
              :icon="Close"
              circle
              aria-label="关闭导航"
              @click="mobileNavVisible = false"
            />
          </div>
          <el-menu
            :default-active="activeMenu"
            router
            class="mobile-nav-menu"
            @select="mobileNavVisible = false"
          >
            <el-menu-item index="/">
              <el-icon><DataBoard /></el-icon>
              <span>仪表盘</span>
            </el-menu-item>
            <el-menu-item index="/transactions">
              <el-icon><List /></el-icon>
              <span>交易记录</span>
            </el-menu-item>
            <el-menu-item index="/corporate-actions">
              <el-icon><DocumentCopy /></el-icon>
              <span>公司行动</span>
            </el-menu-item>
            <el-menu-item index="/holdings">
              <el-icon><Wallet /></el-icon>
              <span>当前持仓</span>
            </el-menu-item>
            <el-menu-item index="/statistics">
              <el-icon><TrendCharts /></el-icon>
              <span>统计分析</span>
            </el-menu-item>
            <el-menu-item index="/exchange-rates">
              <el-icon><Money /></el-icon>
              <span>汇率管理</span>
            </el-menu-item>
            <el-menu-item v-if="authStore.isAdmin" index="/admin/holdings">
              <el-icon><Odometer /></el-icon>
              <span>查看所有持仓</span>
            </el-menu-item>
            <el-menu-item v-if="authStore.isAdmin" index="/admin/users">
              <el-icon><User /></el-icon>
              <span>用户管理</span>
            </el-menu-item>
          </el-menu>
          <el-button
            class="mobile-logout-button"
            :icon="SwitchButton"
            @click="handleUserCommand('logout')"
          >
            退出登录
          </el-button>
        </div>
      </el-drawer>
    </el-header>
    <transition name="status-banner">
      <div v-if="appStatus.hasBlockingIssue" class="status-overlay">
        <div class="status-content">
          <el-icon><WarningFilled /></el-icon>
          <div>
            <strong>{{ appStatus.statusTitle }}</strong>
            <span>{{ appStatus.message }}</span>
          </div>
        </div>
        <el-button size="small" @click="appStatus.clear">关闭</el-button>
      </div>
    </transition>
    <el-main class="app-main">
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth'
import { useAppStatusStore } from './stores/appStatus'
import { ElMessage } from 'element-plus'
import { Close, Menu, SwitchButton, WarningFilled } from '@element-plus/icons-vue'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const appStatus = useAppStatusStore()
const mobileNavVisible = ref(false)

// 应用启动时恢复认证状态
onMounted(async () => {
  // 如果未认证但localStorage中有token，尝试恢复
  if (!authStore.isAuthenticated && localStorage.getItem('token')) {
    await authStore.checkAuth()
  }
})

const activeMenu = computed(() => route.path)

const handleUserCommand = (command) => {
  if (command === 'logout') {
    mobileNavVisible.value = false
    authStore.logout()
    ElMessage.success('已退出登录')
    router.push('/login')
  }
}
</script>

<style scoped>
.app-container {
  min-height: 100vh;
  background: var(--app-bg);
}

.app-header {
  position: sticky;
  top: 0;
  z-index: 100;
  height: auto;
  min-height: 52px;
  background-color: rgba(255, 255, 255, 0.72);
  border-bottom: 1px solid var(--app-separator);
  backdrop-filter: saturate(180%) blur(20px);
  padding: 0;
}

.header-content {
  max-width: 1440px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  height: 100%;
  min-height: 52px;
  padding: 0 24px;
  gap: 20px;
}

.brand-link {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-width: 170px;
  color: var(--app-text);
  text-decoration: none;
}

.brand-link:hover .brand-mark {
  transform: scale(1.05);
}

.brand-mark {
  display: grid;
  grid-template-columns: repeat(3, 5px);
  align-items: end;
  gap: 3px;
  width: 26px;
  height: 26px;
  padding: 4px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 6px;
  box-shadow: var(--app-shadow-sm);
  transition: transform var(--app-duration) var(--apple-spring);
}

.brand-mark span {
  display: block;
  width: 5px;
  border-radius: 2px 2px 0 0;
}

.brand-mark span:nth-child(1) {
  height: 12px;
  background: var(--app-success);
}

.brand-mark span:nth-child(2) {
  height: 18px;
  background: var(--app-primary);
}

.brand-mark span:nth-child(3) {
  height: 9px;
  background: var(--app-danger);
}

.brand-title {
  font-size: 18px;
  font-weight: 700;
  letter-spacing: -0.022em;
  white-space: nowrap;
  color: var(--app-text);
}

.header-menu {
  flex: 1;
  min-width: 0;
  border-bottom: none;
  background: transparent !important;
  overflow-x: auto;
  overflow-y: hidden;
  scrollbar-width: none;
}

.header-menu::-webkit-scrollbar {
  display: none;
}

:deep(.header-menu.el-menu--horizontal) {
  height: 52px;
  background: transparent !important;
}

:deep(.header-menu.el-menu--horizontal > .el-menu-item) {
  height: 52px;
  padding: 0 12px;
  border-bottom: 2px solid transparent !important;
  color: var(--app-text-muted) !important;
  font-weight: 500;
  font-size: 14px;
  letter-spacing: -0.01em;
  background: transparent !important;
  transition: color var(--app-duration) var(--apple-ease),
              border-color var(--app-duration) var(--apple-ease);
}

:deep(.header-menu.el-menu--horizontal > .el-menu-item.is-active) {
  color: var(--app-primary) !important;
  border-bottom: 2px solid var(--app-primary) !important;
}

:deep(.header-menu.el-menu--horizontal > .el-menu-item:hover) {
  color: var(--app-text) !important;
}

.user-info {
  margin-left: auto;
  display: flex;
  align-items: center;
}

.mobile-nav-button {
  display: none;
  margin-left: auto;
}

.user-dropdown {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  padding: 5px 10px;
  border-radius: var(--app-radius-sm);
  transition: background-color var(--app-duration) var(--apple-ease);
}

.user-dropdown:hover {
  background-color: rgba(0, 0, 0, 0.04);
}

.username {
  font-size: 14px;
  color: var(--app-text);
  font-weight: 500;
}

.app-main {
  width: 100%;
  max-width: 1440px;
  margin: 0 auto;
  padding: 28px 24px;
  animation: fadeIn 0.4s var(--apple-spring);
}

@keyframes fadeIn {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.status-overlay {
  position: sticky;
  top: 64px;
  z-index: 19;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  width: min(100%, 1480px);
  margin: 0 auto;
  padding: 12px 32px;
  color: #7f1d1d;
  background: #fef2f2;
  border-bottom: 1px solid #fecaca;
  box-shadow: 0 8px 20px rgba(127, 29, 29, 0.08);
}

.status-content {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.status-content strong,
.status-content span {
  display: block;
}

.status-content strong {
  font-size: 14px;
}

.status-content span {
  color: #991b1b;
  font-size: 13px;
}

.status-banner-enter-active,
.status-banner-leave-active {
  transition:
    opacity 0.18s ease,
    transform 0.18s ease;
}

.status-banner-enter-from,
.status-banner-leave-to {
  opacity: 0;
  transform: translateY(-6px);
}

.mobile-nav-panel {
  display: flex;
  flex-direction: column;
  min-height: 100%;
}

.mobile-nav-user {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 20px 20px 16px;
  border-bottom: 1px solid var(--app-separator);
}

.mobile-nav-name {
  max-width: 180px;
  margin-bottom: 4px;
  color: var(--app-text);
  font-size: 17px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mobile-nav-menu {
  flex: 1;
  border-right: 0;
  padding: 8px;
}

.mobile-nav-menu :deep(.el-menu-item) {
  height: 44px;
  margin: 2px 0;
  border-radius: var(--app-radius-inner);
  font-weight: 500;
}

.mobile-nav-menu :deep(.el-menu-item.is-active) {
  background: var(--app-primary-soft);
  color: var(--app-primary);
}

.mobile-logout-button {
  margin: 12px 16px 20px;
}

@media (max-width: 1100px) {
  .header-content {
    align-items: stretch;
    flex-wrap: wrap;
    gap: 0 12px;
    padding: 10px 16px 0;
  }

  .brand-link {
    min-height: 36px;
  }

  .header-menu {
    order: 3;
    flex-basis: 100%;
    margin-inline: -4px;
  }

  .user-info {
    min-height: 36px;
  }

  .app-main {
    padding: 20px 16px;
  }

  .status-overlay {
    top: 104px;
    padding-inline: 20px;
  }
}

@media (max-width: 640px) {
  .header-content {
    gap: 0 8px;
    min-height: 48px;
    padding: 0 16px;
  }

  .brand-link {
    flex: 1;
    min-width: 0;
  }

  .brand-mark {
    flex: 0 0 auto;
  }

  .brand-title {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    font-size: 16px;
  }

  .user-info {
    display: none;
  }

  .mobile-nav-button {
    display: inline-flex;
    flex: 0 0 auto;
  }

  .header-menu {
    display: none;
  }

  :deep(.mobile-nav-drawer .el-drawer__body) {
    padding: 0;
  }

  .app-main {
    padding: 16px;
  }

  .status-overlay {
    position: static;
    align-items: flex-start;
    flex-direction: column;
    gap: 10px;
    padding: 10px 12px;
  }
}
</style>
