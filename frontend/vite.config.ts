import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import path from 'path'

export default defineConfig({
  plugins: [
    vue(),
    // Element Plus 按需引入（issue #142）：模板里的 el-* 组件与 v-loading 指令按使用注册；
    // 样式不随组件拆分（importStyle: false），由 main.ts 整包先于全局覆盖加载（#285）
    Components({
      resolvers: [ElementPlusResolver({ importStyle: false })],
      dts: 'src/components.d.ts'
    })
  ],
  build: {
    chunkSizeWarningLimit: 1000,
    rollupOptions: {
      output: {
        // 只固定两组体积大、变动少的依赖（长缓存）；Element Plus 与其余依赖交给 Rollup
        // 按路由拆分——此前强制合成 element-plus/vendor 两个大块并在首屏预载全部 ~680KB（#285）
        manualChunks(id: string) {
          if (!id.includes('node_modules')) {
            return
          }
          if (id.includes('echarts') || id.includes('zrender') || id.includes('vue-echarts')) {
            return 'charts'
          }
          if (/node_modules\/(@vue|vue|vue-router|pinia)\//.test(id)) {
            return 'vue-vendor'
          }
        }
      }
    }
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src')
    }
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true
      }
    }
  }
})
