# 开发指南

本项目本地开发默认使用 PostgreSQL，数据库结构由 Alembic 管理。SQLite 不再作为开发或部署数据库。

## 环境要求

- Python 3.11+
- Node.js 18+
- PostgreSQL
- Docker 和 Docker Compose，可选但推荐

## 后端

```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env
```

编辑 `backend/.env`，至少填写：

```env
DATABASE_URL=postgresql://<db-user>:<db-password>@<db-host>:5432/<db-name>
CORS_ORIGINS=http://localhost:5173
SECRET_KEY=<openssl-rand-hex-32>
ADMIN_INITIAL_PASSWORD=<strong-admin-initial-password>
DEMO_INITIAL_PASSWORD=<strong-user-initial-password>
TUSHARE_TOKEN=<tushare-api-token>
ENABLE_DOCS=true
REQUIRE_HTTPS=false
PRICE_REFRESH_MAX_WORKERS=4
BACKGROUND_WORKER_ENABLED=true
LLM_REPORT_API_KEY=
LLM_REPORT_BASE_URL=https://api.deepseek.com
LLM_REPORT_MODEL=deepseek-v4-pro
```

初始化或升级数据库：

```bash
alembic upgrade head
python manage.py seed
```

启动后端：

```bash
ENABLE_DOCS=true uvicorn app.main:app --reload --port 8000
```

访问：

- API root: `http://localhost:8000`
- Swagger: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

应用启动不会自动创建表或补齐初始用户。需要初始账号时运行 `python manage.py seed`。
`TUSHARE_TOKEN` 可留空，但主动行情刷新能力会受限；`LLM_REPORT_API_KEY` 留空时
AI 复盘功能禁用。

## 前端

```bash
cd frontend
npm install
npm run dev
```

访问 `http://localhost:5173`。前端开发模式连接真实后端。

## 目录结构

```text
backend/
├── alembic/
│   └── versions/
├── app/
│   ├── api/                 # auth, users, transactions, holdings, statistics, imports
│   ├── core/                # dependencies and security helpers
│   ├── models/              # SQLAlchemy models
│   ├── schemas/             # Pydantic schemas
│   ├── services/            # portfolio kernel, statistics, imports, jobs, LLM reports
│   ├── config.py
│   ├── database.py
│   └── main.py
└── requirements.txt

frontend/
├── e2e/
├── src/
│   ├── api/
│   ├── components/
│   ├── router/
│   ├── stores/
│   ├── utils/
│   └── views/
└── package.json
```

## 数据库迁移

新数据库初始化：

```bash
cd backend
alembic upgrade head
```

修改 SQLAlchemy models 后：

```bash
cd backend
alembic revision --autogenerate -m "describe change"
# review generated migration carefully
alembic upgrade head
```

旧版文件数据库和自动建表开发路径已经废弃。新环境直接使用 PostgreSQL 并执行 `alembic upgrade head`。

## 测试

后端：

```bash
cd backend
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/investment_test
pytest
```

测试入口会先执行 Alembic migration，并拒绝连接数据库名不含 `test` 或 `e2e` 的 PostgreSQL，避免误碰真实数据库。部分行情相关测试依赖外部 API、网络或 `TUSHARE_TOKEN`，可能被 skip。

前端 E2E：

```bash
cd frontend
export E2E_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/investment_e2e
npm run test:e2e
```

带浏览器界面：

```bash
npm run test:e2e:headed
```

## 代码风格

Python 使用 Ruff 统一检查和格式化：

```bash
ruff format backend
ruff check --fix backend
```

前端使用 Prettier：

```bash
cd frontend
npx prettier --write src e2e
```

## 开发流程

新增后端能力时通常同步修改：

- `backend/app/models/`
- `backend/app/schemas/`
- `backend/app/services/`
- `backend/app/api/`
- Alembic migration
- 后端测试

新增前端能力时通常同步修改：

- `frontend/src/api/index.js`
- `frontend/src/router/index.js`
- `frontend/src/views/`
- 必要的 E2E 测试

## 常用命令

```bash
# 查看 PostgreSQL 结构
psql "$DATABASE_URL"
\dt
\d transactions

# Docker 方式运行迁移
docker compose run --rm backend alembic upgrade head

# Docker 日志
docker compose logs -f backend
docker compose logs -f frontend
```

## 排障

- CORS 错误：检查 `CORS_ORIGINS` 是否包含实际前端地址。
- 登录失败：确认数据库迁移已执行，且 `users` 表存在。
- 数据库连接失败：确认 `DATABASE_URL` 指向可访问的 PostgreSQL。
- API 文档不可访问：确认 `ENABLE_DOCS=true`。

## 认证与 CSRF

- 浏览器调用 `POST /api/auth/login` 后，由后端设置 HttpOnly 会话 Cookie 和可读的 CSRF Cookie；前端不在 `localStorage` 保存访问令牌。
- 浏览器发起 `POST`、`PUT`、`PATCH` 或 `DELETE` 请求时，必须把 `investment_csrf` Cookie 的值放入 `X-CSRF-Token` 请求头。
- 脚本、CLI 和其他非浏览器客户端使用 `POST /api/auth/token` 获取 Bearer Token，后续请求设置 `Authorization: Bearer <token>`；Bearer 请求不需要 CSRF 头。
- `REQUIRE_HTTPS=true` 时，会话 Cookie 使用 `Secure` 属性，登录和令牌签发也只接受 HTTPS（或可信反向代理传入的 `X-Forwarded-Proto: https`）。

## 后台任务状态

价格刷新和历史行情同步共用 PostgreSQL `background_jobs` 状态表。活跃任务由数据库原子领取，同一用户、同一任务类型不会并发执行；多个 API worker 查询到同一份状态。默认保留已完成任务 168 小时，并在启动或创建新任务时把超过 60 分钟未更新心跳的 `queued`/`running` 任务标记为 `interrupted`。可通过 `BACKGROUND_JOB_RETENTION_HOURS` 和 `BACKGROUND_JOB_STALE_MINUTES` 调整。

AI 复盘的手动生成和定期计划也由同一个后台 worker 执行。开发时如需完全关闭轮询，
设置 `BACKGROUND_WORKER_ENABLED=false`；测试入口已默认关闭 worker，并由测试显式驱动作业。
