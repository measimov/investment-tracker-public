import { createApp } from 'vue'
import { createPinia } from 'pinia'
// Element Plus 组件 JS 由 unplugin-vue-components 按需引入（vite.config.ts）；样式整包一次加载
// 并排在 styles.css 之前（#285）：按组件拆的样式会随路由 chunk 懒加载、插到全局覆盖之后，
// 同优先级的 .el-* 覆盖就被组件自身样式冲掉（stat 数值 22px 变回 20px）。整包 gzip 约 35KB，
// 与拆包前首屏预载的 element-plus.css 相同
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import '@fontsource-variable/inter/wght.css'
import '@fontsource-variable/noto-sans-sc/wght.css'
import '@fontsource-variable/noto-serif-sc/wght.css'
import './styles/palette.css'
import './styles.css'
import './styles/dark.css'
import App from './App.vue'
import router from './router'

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.use(router)
app.mount('#app')
