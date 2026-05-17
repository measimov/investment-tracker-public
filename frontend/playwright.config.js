import { defineConfig, devices } from '@playwright/test'

const backendPort = 18000
const frontendPort = 4173
const backendSourcePath = 'backend'
const databaseUrl =
  process.env.E2E_DATABASE_URL ||
  'postgresql://postgres:postgres@127.0.0.1:5432/investment_e2e'

export default defineConfig({
  testDir: './e2e',
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
        `cd ${backendSourcePath}`,
        '&&',
        'alembic upgrade head',
        '&&',
        'cd ..',
        '&&',
        `PYTHONPATH=${backendSourcePath} uvicorn app.main:app --host 127.0.0.1 --port ${backendPort}`
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
