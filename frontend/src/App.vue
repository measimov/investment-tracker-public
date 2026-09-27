<template>
  <!-- 中文 locale：Element Plus 默认英文，空表「No Data」、分页「25/page」、日期选择器月份都是英文（#219） -->
  <el-config-provider :locale="zhCn">
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
            <el-menu-item v-for="item in primaryNavItems" :key="item.path" :index="item.path">
              <el-icon><component :is="item.icon" /></el-icon>
              <span>{{ item.label }}</span>
            </el-menu-item>
            <!-- 低频页主动收进「更多」：常用页不会因宽度不够被挤进 EP 的「…」 -->
            <el-sub-menu index="more" class="header-more-menu">
              <template #title>
                <el-icon><MoreFilled /></el-icon>
                <span>更多</span>
              </template>
              <el-menu-item v-for="item in moreNavItems" :key="item.path" :index="item.path">
                <el-icon><component :is="item.icon" /></el-icon>
                <span>{{ item.label }}</span>
              </el-menu-item>
            </el-sub-menu>
          </el-menu>
          <div v-if="authStore.isAuthenticated" class="user-info">
            <el-dropdown @command="handleUserCommand">
              <span class="user-dropdown">
                <span class="user-avatar" aria-hidden="true">{{ userInitial }}</span>
                <span class="username">{{ authStore.user?.username }}</span>
                <el-icon class="el-icon--right"><ArrowDown /></el-icon>
              </span>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item disabled>
                    <el-tag v-if="authStore.isAdmin" type="danger" size="small">管理员</el-tag>
                    <el-tag v-else type="info" size="small">普通用户</el-tag>
                  </el-dropdown-item>
                  <el-dropdown-item divided command="password">
                    <el-icon><Lock /></el-icon>
                    修改密码
                  </el-dropdown-item>
                  <el-dropdown-item command="logout">
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
          append-to-body
        >
          <div class="mobile-nav-panel">
            <div class="mobile-nav-user">
              <div class="mobile-nav-identity">
                <span class="user-avatar user-avatar-lg" aria-hidden="true">{{ userInitial }}</span>
                <div>
                  <div class="mobile-nav-name">{{ authStore.user?.username }}</div>
                  <el-tag v-if="authStore.isAdmin" type="danger" size="small">管理员</el-tag>
                  <el-tag v-else type="info" size="small">普通用户</el-tag>
                </div>
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
              <el-menu-item v-for="item in primaryNavItems" :key="item.path" :index="item.path">
                <el-icon><component :is="item.icon" /></el-icon>
                <span>{{ item.label }}</span>
              </el-menu-item>
              <!-- 抽屉里空间充足：「更多」平铺成分组，不再折叠一层 -->
              <el-menu-item-group title="更多">
                <el-menu-item v-for="item in moreNavItems" :key="item.path" :index="item.path">
                  <el-icon><component :is="item.icon" /></el-icon>
                  <span>{{ item.label }}</span>
                </el-menu-item>
              </el-menu-item-group>
            </el-menu>
            <div class="mobile-account-actions">
              <el-button :icon="Lock" @click="handleUserCommand('password')">修改密码</el-button>
              <el-button
                class="mobile-logout-button"
                :icon="SwitchButton"
                @click="handleUserCommand('logout')"
              >
                退出登录
              </el-button>
            </div>
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
    <ChangePasswordDialog v-if="authStore.isAuthenticated" v-model="passwordDialogVisible" />
  </el-config-provider>
</template>

<script setup lang="ts">
import { computed, ref, type Component } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth'
import { useAppStatusStore } from './stores/appStatus'
import { ElMessage } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import ChangePasswordDialog from './components/ChangePasswordDialog.vue'
import {
  ChatDotRound,
  Close,
  DataBoard,
  View,
  MagicStick,
  DocumentCopy,
  List,
  Lock,
  Menu,
  Money,
  MoreFilled,
  Odometer,
  SwitchButton,
  Tickets,
  TrendCharts,
  User,
  Wallet,
  WarningFilled
} from '@element-plus/icons-vue'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const appStatus = useAppStatusStore()
const mobileNavVisible = ref(false)

// 导航项集中定义，桌面端与移动端菜单共用（认证状态由路由守卫恢复）。
// 按使用频率排：常用页在前；低频页主动收进「更多」，不等宽度不够时被 EP 挤进「…」
interface NavItem {
  path: string
  label: string
  icon: Component
  adminOnly?: boolean
}

const primaryNav: NavItem[] = [
  { path: '/', label: '仪表盘', icon: DataBoard },
  { path: '/holdings', label: '当前持仓', icon: Wallet },
  { path: '/transactions', label: '交易记录', icon: List },
  { path: '/statistics', label: '统计分析', icon: TrendCharts },
  { path: '/watchlist', label: '观察清单', icon: View },
  { path: '/opinions', label: '雪球观点', icon: ChatDotRound },
  { path: '/reports', label: 'AI 复盘', icon: MagicStick }
]

const moreNav: NavItem[] = [
  { path: '/corporate-actions', label: '公司行动', icon: DocumentCopy },
  { path: '/account-data', label: '账户数据', icon: Tickets },
  { path: '/exchange-rates', label: '汇率管理', icon: Money },
  { path: '/admin/holdings', label: '查看所有持仓', icon: Odometer, adminOnly: true },
  { path: '/admin/users', label: '用户管理', icon: User, adminOnly: true }
]

const visible = (item: NavItem) => !item.adminOnly || authStore.isAdmin
const primaryNavItems = computed(() => primaryNav.filter(visible))
const moreNavItems = computed(() => moreNav.filter(visible))

const userInitial = computed(() => authStore.user?.username?.trim().charAt(0).toUpperCase() || '?')

// 详情/嵌套路由用 meta.nav 指向所属菜单（标的档案 → 当前持仓），否则按 path 高亮
const activeMenu = computed(() => route.meta.nav ?? route.path)

const passwordDialogVisible = ref(false)

const handleUserCommand = async (command: string) => {
  if (command === 'password') {
    mobileNavVisible.value = false
    passwordDialogVisible.value = true
  } else if (command === 'logout') {
    mobileNavVisible.value = false
    await authStore.logout()
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
  min-height: var(--app-header-height);
  background-color: var(--app-surface);
  border-bottom: 1px solid var(--app-border-soft);
  padding: 0;
}

.header-content {
  max-width: 1440px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  height: 100%;
  min-height: var(--app-header-height);
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

.brand-mark {
  display: grid;
  grid-template-columns: repeat(3, 5px);
  align-items: end;
  gap: 3px;
  width: 28px;
  height: 28px;
  padding: 5px;
  background: var(--app-primary);
  border: none;
  border-radius: var(--app-radius-inner);
}

.brand-mark span {
  display: block;
  width: 5px;
  border-radius: 1px 1px 0 0;
  background: var(--app-on-primary);
}

.brand-mark span:nth-child(1) {
  height: 11px;
  opacity: 0.75;
}

.brand-mark span:nth-child(2) {
  height: 17px;
}

.brand-mark span:nth-child(3) {
  height: 8px;
  opacity: 0.6;
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
  height: 36px;
  line-height: 36px;
  margin: 8px 3px;
  padding: 0 14px;
  border: none !important;
  border-radius: var(--app-radius-inner);
  color: var(--app-text-muted) !important;
  font-weight: 500;
  font-size: 14px;
  letter-spacing: -0.01em;
  background: transparent !important;
  transition:
    color var(--app-duration) var(--apple-ease),
    background-color var(--app-duration) var(--apple-ease);
}

:deep(.header-menu.el-menu--horizontal > .el-menu-item.is-active) {
  color: var(--app-primary) !important;
  background: var(--app-primary-soft) !important;
  font-weight: 600;
}

:deep(.header-menu.el-menu--horizontal > .el-menu-item:hover) {
  color: var(--app-text) !important;
  background: var(--app-hover) !important;
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
  gap: 8px;
  cursor: pointer;
  padding: 4px 10px 4px 4px;
  border-radius: var(--app-radius-inner);
  transition: background-color var(--app-duration) var(--apple-ease);
}

.user-dropdown:hover {
  background-color: var(--app-hover);
}

.user-avatar {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--app-primary);
  color: var(--app-on-primary);
  font-size: 13px;
  font-weight: 700;
  line-height: 1;
  user-select: none;
}

.user-avatar-lg {
  width: 40px;
  height: 40px;
  font-size: 17px;
  flex-shrink: 0;
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
  padding: 20px 24px;
}

.status-overlay {
  position: sticky;
  top: calc(var(--app-header-height) + 12px);
  z-index: 19;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  width: min(100%, 1440px);
  margin: 0 auto;
  padding: 12px 32px;
  color: var(--app-danger-text);
  background: var(--app-danger-surface);
  border-bottom: 1px solid var(--app-danger-border);
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
  color: var(--app-danger-text);
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
  background: var(--app-surface-muted);
}

.mobile-nav-identity {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
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
  font-weight: 600;
}

.mobile-nav-menu :deep(.el-menu-item:hover) {
  background: var(--app-hover);
}

.mobile-logout-button {
  margin: 12px 16px 20px;
  border-radius: var(--app-radius-inner);
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
    top: calc(var(--app-header-height) + 52px);
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

<!--
  #219 导航/账号操作新增的样式，单独成块放在末尾：上面的主样式块由扁平主题改造负责，
  分开写减少合并冲突。「更多」与 EP 自动溢出的「…」都是 .el-sub-menu，它们的标题
  要与顶层菜单项同一套外观；当前页在「更多」里时 EP 给 sub-menu 加 .is-active，
  此前没有覆盖这个状态，高亮样式与其他项不一致。
-->
<style scoped>
:deep(.header-menu.el-menu--horizontal > .el-sub-menu .el-sub-menu__title) {
  height: 36px;
  line-height: 36px;
  margin: 8px 3px;
  padding: 0 14px;
  border: none !important;
  border-radius: var(--app-radius-inner);
  color: var(--app-text-muted) !important;
  font-weight: 500;
  font-size: 14px;
  background: transparent !important;
}

:deep(.header-menu.el-menu--horizontal > .el-sub-menu .el-sub-menu__title:hover) {
  color: var(--app-text) !important;
  background: var(--app-hover, rgba(15, 23, 42, 0.05)) !important;
}

:deep(.header-menu.el-menu--horizontal > .el-sub-menu.is-active .el-sub-menu__title) {
  color: var(--app-primary) !important;
  background: var(--app-primary-soft) !important;
  font-weight: 600;
}

.mobile-account-actions {
  display: flex;
  gap: 8px;
  margin: 12px 16px 20px;
}

.mobile-account-actions .el-button {
  flex: 1;
  margin: 0;
  border-radius: var(--app-radius-inner);
}
</style>
