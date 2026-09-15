/**
 * 纯函数单测跑道（issue #142）：与 Playwright 分工——vitest 只认 src 内的
 * *.spec.ts（Node 直测，秒级反馈），E2E 仍归 e2e/ 目录的 Playwright。
 * 两边的 include/testDir 互斥，谁也不会捡到对方的用例。
 */
import { defineConfig } from 'vitest/config'
import path from 'path'

export default defineConfig({
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src')
    }
  },
  test: {
    include: ['src/**/*.spec.ts'],
    environment: 'node'
  }
})
