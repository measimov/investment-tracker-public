import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'

export default defineConfig({
  root: fileURLToPath(new URL('./architecture', import.meta.url)),
  plugins: [vue()],
  build: {
    outDir: '../architecture-dist',
    emptyOutDir: true
  },
  server: {
    host: '127.0.0.1',
    port: 5174,
    strictPort: true,
    proxy: {
      '/api': 'http://127.0.0.1:8765'
    }
  }
})
