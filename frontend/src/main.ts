import { createApp } from 'vue'
import { createPinia } from 'pinia'
// Element Plus 组件与 v-loading 由 unplugin-vue-components 按需引入
// （vite.config.ts）。这里只补命令式 API 的样式：ElMessage/ElMessageBox/
// ElNotification 在 .ts 里显式 import，模板解析器看不到它们的用点
import 'element-plus/theme-chalk/base.css'
import 'element-plus/theme-chalk/el-message.css'
import 'element-plus/theme-chalk/el-message-box.css'
import 'element-plus/theme-chalk/el-overlay.css'
import 'element-plus/theme-chalk/el-notification.css'
import 'element-plus/theme-chalk/el-loading.css'
import './styles.css'
import {
  ArrowDown,
  Close,
  DataBoard,
  DocumentCopy,
  Download,
  List,
  Lock,
  Menu,
  Money,
  Odometer,
  PieChart,
  Plus,
  Refresh,
  SwitchButton,
  TrendCharts,
  Upload,
  UploadFilled,
  User,
  Wallet,
  WarningFilled
} from '@element-plus/icons-vue'
import App from './App.vue'
import router from './router'

const app = createApp(App)
const pinia = createPinia()

// Register only the icons used across the app (keeps the bundle small).
const icons = {
  ArrowDown,
  Close,
  DataBoard,
  DocumentCopy,
  Download,
  List,
  Lock,
  Menu,
  Money,
  Odometer,
  PieChart,
  Plus,
  Refresh,
  SwitchButton,
  TrendCharts,
  Upload,
  UploadFilled,
  User,
  Wallet,
  WarningFilled
}
for (const [key, component] of Object.entries(icons)) {
  app.component(key, component)
}

app.use(pinia)
app.use(router)
app.mount('#app')
