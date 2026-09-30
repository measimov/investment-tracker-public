# 开发指南

本项目本地开发默认使用 PostgreSQL，数据库结构由 Alembic 管理。SQLite 不再作为开发或部署数据库。

## 环境要求

- Python 3.12
- Node.js 20+（`marked@18` 等依赖要求 ≥20；CI 用 20、生产镜像 22）
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
```

这两行是**显式放宽**：代码默认是 `ENABLE_DOCS=false` / `REQUIRE_HTTPS=true`
（生产 fail-closed）。不写的话本地明文登录会被拒、`/docs` 也打不开。

完整变量清单（后台任务、Tushare 限速、价格新鲜度窗口、`LLM_REPORT_*` 等）
见 `.env.example`，Settings 字段与 `backend/app/config.py` 一一对应（端口/证书/日志目录与
backup 变量只给 compose 和宿主脚本用，本地开发可忽略）。新增配置要同步 `config.py`、
`docker-compose.yml`、`.env.example` 三处，`tests/test_deploy_config_sync.py` 守着。

私有部署使用的雪球客户端库 `xueqiu-market` 不在公开仓库中，`requirements.txt` 也不包含它；
未安装时雪球行情/档案相关功能显式降级，其余不受影响。

初始化或升级数据库：

```bash
alembic upgrade head
python manage.py seed
```

启动后端：

```bash
ENABLE_DOCS=true uvicorn app.main:app --reload --port 8000 --no-proxy-headers
```

访问：

- API root: `http://localhost:8000`
- Swagger: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

应用启动不会自动创建表或补齐初始用户。需要初始账号时运行 `python manage.py seed`。

`TUSHARE_TOKEN` 可留空，但主动行情刷新、分红公告与基本面档案同步会受限；
`LLM_REPORT_API_KEY` 留空时 AI 复盘和标的分析功能禁用。

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
│   ├── services/            # holdings, statistics, broker importers, price refresh
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

测试入口会先执行 Alembic migration，并拒绝连接数据库名不含 `test` 或 `e2e` 的 PostgreSQL，避免误碰真实数据库。

测试进程与外部世界隔离（#274）：`conftest.py` 把 `backend/.env` 里的凭证（LLM Key、Tushare/Tiingo Token、雪球 Cookie、推送 URL）强制置空，并拦截一切非回环地址的出站连接——漏打桩的用例会直接报「测试进程禁止外部网络连接」，而不是悄悄花真 token。真实行情源冒烟用例需显式 `RUN_EXTERNAL_PRICE_TESTS=1`（或 `ALLOW_TEST_NETWORK=1`）运行，此时守卫与凭证置空一并关闭。

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

Playwright 自己起的后端设了 `PERIODIC_TASKS_ENABLED=false` 并置空凭证。本地 `reuseExistingServer` 开着：如果 18000 端口上已有一个自己手动起、开着周期任务的后端，E2E 会直接复用它——跑 E2E 前先停掉它。

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

## 排障

- CORS 错误：检查 `CORS_ORIGINS` 是否包含实际前端地址。
- 登录失败：确认数据库迁移已执行，且 `users` 表存在。开发库口令对不上（例如从生产备份恢复的库、或 `.env` 的初始口令改过而 `seed` 不会覆盖已存在用户）时，在 `backend/` 下运行 `python manage.py reset-password <用户名>` 按提示输入新口令；非交互场景用 `--password-env VAR` 从环境变量读取。该命令会吊销此用户的全部会话。
- 数据库连接失败：确认 `DATABASE_URL` 指向可访问的 PostgreSQL。
- 每次连库都要卡约 3 分钟（后端启动卡在 `Waiting for application startup`、起子进程的测试超时）：macOS 上 libpq 默认 `gssencmode=prefer`，GSSAPI 协商会挂起约 180 秒才回退。本机 `DATABASE_URL` 末尾加 `?gssencmode=disable` 即可。
- API 文档不可访问：确认 `ENABLE_DOCS=true`。

## 认证与后台任务

认证/CSRF 流程与后台任务机制的权威描述见 [CLAUDE.md](CLAUDE.md)（Auth flow、
Background jobs 两节）；要点：浏览器走 HttpOnly Cookie + `X-CSRF-Token`，
脚本走 `POST /api/auth/token` 的 Bearer Token；价格刷新与历史行情同步共用
PostgreSQL `background_jobs` 表，由数据库原子领取，相关 `BACKGROUND_JOB_*`
参数见 `.env.example`。
