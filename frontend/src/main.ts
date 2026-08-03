import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
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
app.use(ElementPlus)
app.use(router)
app.mount('#app')
