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

完整变量清单（后台任务、Tushare 限速、价格新鲜度窗口、`LLM_REPORT_*` 等）
见 `.env.example`，各变量与 `backend/app/config.py` 一一对应。

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

## 排障

- CORS 错误：检查 `CORS_ORIGINS` 是否包含实际前端地址。
- 登录失败：确认数据库迁移已执行，且 `users` 表存在。
- 数据库连接失败：确认 `DATABASE_URL` 指向可访问的 PostgreSQL。
- API 文档不可访问：确认 `ENABLE_DOCS=true`。

## 认证与后台任务

认证/CSRF 流程与后台任务机制的权威描述见 [CLAUDE.md](CLAUDE.md)（Auth flow、
Background jobs 两节）；要点：浏览器走 HttpOnly Cookie + `X-CSRF-Token`，
脚本走 `POST /api/auth/token` 的 Bearer Token；价格刷新与历史行情同步共用
PostgreSQL `background_jobs` 表，由数据库原子领取，相关 `BACKGROUND_JOB_*`
参数见 `.env.example`。
