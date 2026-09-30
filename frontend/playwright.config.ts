import { defineConfig, devices } from '@playwright/test'

const backendPort = 18000
const frontendPort = 4173
const backendSourcePath = 'backend'
const databaseUrl =
  process.env.E2E_DATABASE_URL || 'postgresql://postgres:postgres@127.0.0.1:5432/investment_e2e'

export default defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/global-setup.ts',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : [['list']],
  use: {
    baseURL: `http://127.0.0.1:${frontendPort}`,
    trace: 'on-first-retry'
  },
  webServer: [
    {
      command: [
        `export DATABASE_URL=${databaseUrl};`,
        `export CORS_ORIGINS=http://127.0.0.1:${frontendPort};`,
        'export SECRET_KEY=e2e-secret-key;',
        'export ADMIN_INITIAL_PASSWORD=e2e-admin-password;',
        'export DEMO_INITIAL_PASSWORD=e2e-user-password;',
        'export REQUIRE_HTTPS=false;',
        // 周期任务（汇率/参考利率/港交所日报/目录/行业/基准/告警……）会在启动即外呼并写 e2e 库，
        // 总开关一次关掉（#274）。报价与事件提醒两个开关另管非周期路径（加自选即时报价、
        // 异动提醒），必须同时保留关闭
        'export PERIODIC_TASKS_ENABLED=false QUOTE_AUTO_REFRESH_ENABLED=false EVENT_NOTIFICATIONS_ENABLED=false;',
        // 后端从仓库根目录启动，但 alembic/seed 在 backend/ 下运行会读到 backend/.env 的真实凭证：
        // 显式置空，E2E 不应拿开发者的 Key 调外部服务
        'export LLM_REPORT_API_KEY= TUSHARE_TOKEN= TIINGO_API_TOKEN= XUEQIU_COOKIES= XUEQIU_COOKIE_FILE= XUEQIU_COLLECTOR_PUSH_URL= NOTIFY_URLS=;',
        `cd ${backendSourcePath}`,
        '&&',
        'alembic upgrade head',
        '&&',
        'python manage.py seed',
        '&&',
        'cd ..',
        '&&',
        `PYTHONPATH=${backendSourcePath} uvicorn app.main:app --host 127.0.0.1 --port ${backendPort} --no-proxy-headers`
      ].join(' '),
      cwd: '..',
      url: `http://127.0.0.1:${backendPort}/`,
      reuseExistingServer: !process.env.CI,
      timeout: 120000
    },
    {
      command: [
        `VITE_API_URL=http://127.0.0.1:${backendPort}/api`,
        'npm run build',
        '--',
        '--mode',
        'e2e',
        '&&',
        `vite preview --host 127.0.0.1 --port ${frontendPort}`
      ].join(' '),
      url: `http://127.0.0.1:${frontendPort}/`,
      reuseExistingServer: !process.env.CI,
      timeout: 120000
    }
  ],
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] }
    }
  ]
})
