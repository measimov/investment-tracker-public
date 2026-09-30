/**
 * 纯函数单测跑道（issue #142）：与 Playwright 分工——vitest 只认 src 内的
 * *.spec.ts（Node 直测，秒级反馈），E2E 仍归 e2e/ 目录的 Playwright。
 * 两边的 include/testDir 互斥，谁也不会捡到对方的用例。
 * 默认 node 环境；要 DOM 的 spec（markdown 消毒、依赖生命周期/visibility 的 composable）
 * 在文件首行写 `// @vitest-environment jsdom` 单独切换，其余用例不背 jsdom 的启动成本。
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
