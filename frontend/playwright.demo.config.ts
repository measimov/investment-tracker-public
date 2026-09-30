import { defineConfig } from '@playwright/test'

/**
 * README 截图与演示视频（docs/media/README.md）。与 E2E 完全分开：独立端口、独立的
 * investment_demo 库（先由 backend/scripts/seed_demo.py 灌数），不会复用 E2E 的服务。
 * 截图写进 docs/media/，视频写进 demo-output/ 再由 scripts/demo-media.sh 转码。
 */
const backendPort = 18100
const frontendPort = 4174
const databaseUrl =
  process.env.DEMO_DATABASE_URL ||
  'postgresql://postgres:postgres@127.0.0.1:5432/investment_demo?gssencmode=disable'

export default defineConfig({
  testDir: './demo',
  outputDir: './demo-output/playwright',
  workers: 1,
  retries: 0,
  timeout: 180000,
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${frontendPort}`,
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    colorScheme: 'light',
    viewport: { width: 1440, height: 900 }
  },
  webServer: [
    {
      command: [
        `export DATABASE_URL='${databaseUrl}';`,
        `export CORS_ORIGINS=http://127.0.0.1:${frontendPort};`,
        'export SECRET_KEY=demo-secret-key ADMIN_INITIAL_PASSWORD=demo-admin DEMO_INITIAL_PASSWORD=demo-unused;',
        'export REQUIRE_HTTPS=false;',
        // 演示库是静态快照：不跑任何周期任务/报价刷新/提醒，也不带外部凭证
        'export PERIODIC_TASKS_ENABLED=false QUOTE_AUTO_REFRESH_ENABLED=false EVENT_NOTIFICATIONS_ENABLED=false;',
        'export LLM_REPORT_API_KEY= TUSHARE_TOKEN= TIINGO_API_TOKEN= XUEQIU_COOKIES= XUEQIU_COOKIE_FILE= NOTIFY_URLS=;',
        'cd backend && alembic upgrade head && cd .. &&',
        `PYTHONPATH=backend uvicorn app.main:app --host 127.0.0.1 --port ${backendPort} --no-proxy-headers`
      ].join(' '),
      cwd: '..',
      url: `http://127.0.0.1:${backendPort}/`,
      reuseExistingServer: false,
      timeout: 120000
    },
    {
      command: `VITE_API_URL=http://127.0.0.1:${backendPort}/api npm run build -- --outDir demo-output/dist && vite preview --outDir demo-output/dist --host 127.0.0.1 --port ${frontendPort}`,
      url: `http://127.0.0.1:${frontendPort}/`,
      reuseExistingServer: false,
      timeout: 180000
    }
  ]
})
