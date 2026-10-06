<template>
  <el-config-provider :locale="zhCn">
    <NConfigProvider
      :theme="theme.resolved.value === 'dark' ? darkTheme : null"
      :theme-overrides="holdingsTheme"
      :locale="zhCN"
      :date-locale="dateZhCN"
    >
      <div
        class="app-container"
        :class="{
          'has-sidebar': authStore.isAuthenticated,
          'sidebar-expanded': authStore.isAuthenticated && !sidebarCollapsed
        }"
      >
        <aside
          v-if="authStore.isAuthenticated"
          id="desktop-navigation"
          class="desktop-sidebar"
          :class="{ 'is-collapsed': sidebarCollapsed }"
        >
          <div class="sidebar-heading">
            <router-link to="/" class="brand-link sidebar-brand" aria-label="投资追踪系统首页">
              <span class="brand-mark" aria-hidden="true"></span>
              <span class="brand-title sidebar-label" :aria-hidden="sidebarCollapsed"
                >投资追踪系统</span
              >
            </router-link>
            <button
              ref="navigationToggle"
              type="button"
              class="navigation-toggle"
              :aria-label="sidebarCollapsed ? '展开导航' : '收起导航'"
              :title="sidebarCollapsed ? '展开导航' : '收起导航'"
              :aria-expanded="isCompact ? mobileNavVisible : desktopExpanded"
              :aria-controls="isCompact ? 'mobile-navigation' : 'desktop-navigation'"
              @click="toggleNavigation"
            >
              <NIcon :size="20" aria-hidden="true"
                ><component :is="sidebarCollapsed ? Expand : Fold"
              /></NIcon>
              <span class="sidebar-label" :aria-hidden="sidebarCollapsed">收起导航</span>
            </button>
          </div>
          <AppNavigation
            class="sidebar-navigation"
            :primary-items="primaryNavItems"
            :more-items="moreNavItems"
            :active-path="activeMenu"
            :collapsed="sidebarCollapsed"
          />
          <div class="sidebar-footer">
            <NTooltip
              v-if="route.name === 'SecurityDetail'"
              placement="right"
              :disabled="!sidebarCollapsed"
              :delay="200"
            >
              <template #trigger>
                <button type="button" class="sidebar-back" aria-label="返回" @click="goBack">
                  <ArrowLeft :size="20" aria-hidden="true" />
                  <span class="sidebar-label" :aria-hidden="sidebarCollapsed">返回</span>
                </button>
              </template>
              返回
            </NTooltip>
            <AppSettings :collapsed="sidebarCollapsed" @command="handleUserCommand" />
          </div>
        </aside>
        <div v-else class="public-settings">
          <AppSettings collapsed />
        </div>
        <div class="app-content">
          <transition name="status-banner">
            <div v-if="appStatus.hasBlockingIssue" class="status-overlay">
              <div class="status-content">
                <NIcon :size="20"><WarningFilled /></NIcon>
                <div>
                  <strong>{{ appStatus.statusTitle }}</strong
                  ><span>{{ appStatus.message }}</span>
                </div>
              </div>
              <NButton size="small" @click="appStatus.clear">关闭</NButton>
            </div>
          </transition>
          <main class="app-main">
            <div v-if="initialNavigationPending" class="app-boot-loading" role="status">
              <NIcon class="is-loading"><Loading /></NIcon><span>正在连接服务…</span>
            </div>
            <router-view />
          </main>
        </div>
        <NDrawer
          v-if="authStore.isAuthenticated"
          v-model:show="mobileNavVisible"
          placement="left"
          :width="288"
          class="mobile-nav-drawer"
          aria-label="应用导航"
          @after-leave="finishNavigationClose"
        >
          <NDrawerContent
            :body-content-style="{ padding: '0', display: 'flex', flexDirection: 'column' }"
            :header-style="{ padding: '4px' }"
            :footer-style="{
              padding: '8px 4px',
              paddingBottom: 'max(8px, env(safe-area-inset-bottom))'
            }"
          >
            <template #header
              ><div class="drawer-heading">
                <router-link
                  to="/"
                  class="brand-link sidebar-brand"
                  @click="mobileNavVisible = false"
                >
                  <span class="brand-mark" aria-hidden="true"></span>
                  <span class="brand-title">投资追踪系统</span>
                </router-link>
                <button
                  type="button"
                  class="navigation-toggle"
                  aria-label="收起导航"
                  @click="mobileNavVisible = false"
                >
                  <NIcon :size="20" aria-hidden="true"><Fold /></NIcon><span>收起导航</span>
                </button>
              </div></template
            >
            <div id="mobile-navigation" class="mobile-nav-panel">
              <AppNavigation
                class="sidebar-navigation"
                :primary-items="primaryNavItems"
                :more-items="moreNavItems"
                :active-path="activeMenu"
                @navigate="mobileNavVisible = false"
              />
            </div>
            <template #footer>
              <div class="drawer-footer-actions">
                <button
                  v-if="route.name === 'SecurityDetail'"
                  type="button"
                  class="sidebar-back"
                  aria-label="返回"
                  @click="goBack"
                >
                  <ArrowLeft :size="20" aria-hidden="true" />
                  <span>返回</span>
                </button>
                <AppSettings @command="handleUserCommand" />
              </div>
            </template>
          </NDrawerContent>
        </NDrawer>
      </div>
      <ChangePasswordDialog v-if="authStore.isAuthenticated" v-model="passwordDialogVisible" />
    </NConfigProvider>
  </el-config-provider>
</template>

<script setup lang="ts">
import { computed, ref, watch, nextTick, type Component } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  darkTheme,
  NButton,
  NConfigProvider,
  NDrawer,
  NDrawerContent,
  NIcon,
  NTooltip,
  zhCN,
  dateZhCN
} from 'naive-ui'
import { useAuthStore } from './stores/auth'
import { useAppStatusStore } from './stores/appStatus'
import { useMediaQuery } from './composables/useMediaQuery'
import { ElMessage } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { useHoldingsTheme } from './styles/naive'
import { useTheme } from './styles/theme'
const theme = useTheme()
const holdingsTheme = useHoldingsTheme()
import { useXueqiuCapabilitiesStore } from './stores/xueqiuCapabilities'
import ChangePasswordDialog from './components/ChangePasswordDialog.vue'
import AppNavigation from './components/AppNavigation.vue'
import AppSettings from './components/AppSettings.vue'
import {
  ArrowLeft,
  Bell,
  MessagesSquare as ChatDotRound,
  LayoutDashboard as DataBoard,
  Eye as View,
  Sparkles as MagicStick,
  Files as DocumentCopy,
  List,
  Banknote as Money,
  Gauge as Odometer,
  ChartNoAxesCombined as TrendCharts,
  PanelsTopLeft as Tickets,
  UserRound as User,
  LoaderCircle as Loading,
  Wallet,
  TriangleAlert as WarningFilled,
  PanelLeftClose as Fold,
  PanelLeftOpen as Expand
} from '@lucide/vue'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const xueqiuCapabilities = useXueqiuCapabilitiesStore()
const appStatus = useAppStatusStore()
const isCompact = useMediaQuery('(max-width: 1100px)')
const navigationToggle = ref<HTMLButtonElement | null>(null)
const preferenceKey = 'investment-navigation-expanded'
function readDesktopPreference() {
  try {
    return localStorage.getItem(preferenceKey) !== 'false'
  } catch {
    return true
  }
}
const desktopExpanded = ref(readDesktopPreference())
const mobileNavVisible = ref(false)
const sidebarCollapsed = computed(() => isCompact.value || !desktopExpanded.value)
const initialNavigationPending = computed(() => route.matched.length === 0)
function goBack() {
  mobileNavVisible.value = false
  const previous = window.history.state?.back
  if (previous && router.resolve(previous).name !== 'Login') router.back()
  else router.push('/holdings')
}
function toggleNavigation() {
  if (isCompact.value) mobileNavVisible.value = !mobileNavVisible.value
  else {
    desktopExpanded.value = !desktopExpanded.value
    try {
      localStorage.setItem(preferenceKey, String(desktopExpanded.value))
    } catch {
      /* 本地偏好不可写时本次会话仍可使用。 */
    }
  }
}
watch(isCompact, async (compact) => {
  const focusedInSidebar = compact && document.activeElement?.closest('.desktop-sidebar')
  mobileNavVisible.value = false
  if (focusedInSidebar) {
    await nextTick()
    navigationToggle.value?.focus()
  }
})
watch(
  () => route.fullPath,
  () => {
    mobileNavVisible.value = false
  }
)
watch(
  () => authStore.isAuthenticated,
  () => {
    mobileNavVisible.value = false
  }
)

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
  { path: '/admin/users', label: '用户管理', icon: User, adminOnly: true },
  { path: '/admin/alerts', label: '系统告警', icon: Bell, adminOnly: true }
]

const visible = (item: NavItem) =>
  (!item.adminOnly || authStore.isAdmin) &&
  (item.path !== '/opinions' || xueqiuCapabilities.showOpinions)
const primaryNavItems = computed(() => primaryNav.filter(visible))
const moreNavItems = computed(() => moreNav.filter(visible))

// 详情/嵌套路由用 meta.nav 指向所属菜单（标的档案 → 当前持仓），否则按 path 高亮
const activeMenu = computed(() => route.meta.nav ?? route.path)

const passwordDialogVisible = ref(false)
let openPasswordAfterDrawer = false
function finishNavigationClose() {
  if (openPasswordAfterDrawer) {
    openPasswordAfterDrawer = false
    passwordDialogVisible.value = true
  }
}
const handleUserCommand = async (command: string) => {
  if (command === 'password') {
    if (mobileNavVisible.value) {
      openPasswordAfterDrawer = true
      mobileNavVisible.value = false
    } else passwordDialogVisible.value = true
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
  --sidebar-width: 0px;
  --app-header-height: 0px;
}
.app-container.has-sidebar {
  --sidebar-width: 56px;
}
.app-container.sidebar-expanded {
  --sidebar-width: 208px;
}
.app-content {
  margin-left: var(--sidebar-width);
  min-width: 0;
  transition: margin-left var(--app-navigation-duration) var(--apple-ease);
}
.public-settings {
  position: fixed;
  left: 8px;
  bottom: max(8px, env(safe-area-inset-bottom));
  width: 44px;
  z-index: 100;
}
.desktop-sidebar {
  position: fixed;
  inset: 0 auto 0 0;
  display: flex;
  flex-direction: column;
  width: var(--sidebar-width);
  height: 100vh;
  height: 100dvh;
  background: var(--app-sidebar);
  border-right: 1px solid var(--app-border);
  z-index: 100;
  transition: width var(--app-navigation-duration) var(--apple-ease);
}
.sidebar-heading {
  display: grid;
  flex-shrink: 0;
  gap: 4px;
  padding: 4px;
  overflow: hidden;
}
.sidebar-navigation {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
}
.sidebar-footer {
  display: grid;
  gap: 4px;
  flex-shrink: 0;
  padding: 8px 4px;
  padding-bottom: max(8px, env(safe-area-inset-bottom));
  border-top: 1px solid var(--app-border-soft);
}
.drawer-footer-actions {
  display: grid;
  gap: 4px;
  width: 100%;
}
.sidebar-back {
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
.sidebar-back > svg {
  justify-self: center;
}
.sidebar-back:hover {
  background: var(--app-hover);
  color: var(--app-text);
}
.sidebar-back:focus-visible {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: -2px;
}
.brand-link {
  display: flex;
  align-items: center;
  gap: 8px;
  text-decoration: none;
  color: var(--app-text);
  min-width: 0;
}
.sidebar-brand {
  display: grid;
  grid-template-columns: var(--app-navigation-icon-column) minmax(0, 1fr);
  gap: 0;
  min-height: 44px;
  overflow: hidden;
}
.sidebar-brand .brand-mark {
  justify-self: center;
}
.sidebar-label {
  white-space: nowrap;
  transition: opacity var(--app-navigation-label-duration) var(--apple-ease);
}
.is-collapsed .sidebar-label {
  opacity: 0;
  pointer-events: none;
}
.brand-title {
  font-family: var(--app-font-serif);
  font-size: 17px;
  font-weight: 600;
  white-space: nowrap;
}
.brand-mark {
  display: block;
  flex: 0 0 26px;
  width: 26px;
  height: 26px;
  background-color: var(--app-primary);
  -webkit-mask: url('./assets/brand-mark.png') center / contain no-repeat;
  mask: url('./assets/brand-mark.png') center / contain no-repeat;
}
.navigation-toggle {
  display: grid;
  grid-template-columns: var(--app-navigation-icon-column) minmax(0, 1fr);
  align-items: center;
  width: 100%;
  height: 44px;
  padding: 0;
  border: 0;
  border-radius: var(--app-radius-inner);
  background: transparent;
  color: var(--app-text-muted);
  font: inherit;
  text-align: left;
  overflow: hidden;
  cursor: pointer;
}
.navigation-toggle > .n-icon {
  justify-self: center;
}
.navigation-toggle:hover {
  background: var(--app-hover);
  color: var(--app-text);
}
.navigation-toggle:focus-visible {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: -2px;
}
.app-main {
  width: 100%;
  max-width: 1440px;
  margin: 0 auto;
  padding: 20px 24px;
}
.app-boot-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 40vh;
  color: var(--app-text-muted);
  font-size: 14px;
}
.status-overlay {
  position: sticky;
  top: 0;
  z-index: 19;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  max-width: 1440px;
  margin: 0 auto;
  padding: 12px 24px;
  color: var(--app-danger-text);
  background: var(--app-danger-surface);
  border-bottom: 1px solid var(--app-danger-border);
}
.status-content {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  overflow-wrap: anywhere;
}
.status-content strong,
.status-content span {
  display: block;
}
.status-content strong {
  font-size: 14px;
}
.status-content span {
  font-size: 13px;
}
.status-banner-enter-active,
.status-banner-leave-active {
  transition: opacity 0.15s ease;
}
.status-banner-enter-from,
.status-banner-leave-to {
  opacity: 0;
}
.drawer-heading {
  display: grid;
  gap: 4px;
  width: 100%;
}
.mobile-nav-panel {
  flex: 1;
}
@media (max-width: 1100px) {
  .app-main {
    padding: 20px 16px;
  }
  .status-overlay {
    padding-inline: 16px;
  }
}
@media (min-width: 1025px) {
  .app-main {
    padding: var(--app-space-sm) var(--app-space-lg);
  }
}
@media (max-width: 640px) {
  .app-main {
    padding: 12px 10px;
  }
}
</style>
