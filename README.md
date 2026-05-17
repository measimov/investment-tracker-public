# Investment Tracker

个人投资交易、持仓、收益、公司行动、汇率和券商流水导入追踪系统。

当前技术栈已经统一迁移到 **PostgreSQL**。生产和本地开发都应使用 PostgreSQL；数据库结构由 Alembic 管理，应用启动只会补齐初始用户，不会自动创建表。

## 功能

- 交易记录管理：买入、卖出、筛选、导入、导出
- 持仓计算：按交易和公司行动重算数量、成本和当前价格
- 公司行动：现金股息、红股、配股、拆股、合股、税费调整
- 收益统计：持仓表现、FIFO 已实现盈亏、股息收入、账户总收益
- 多币种：汇率维护、换算和双币种展示
- 券商导入：标准 CSV/Excel、招商证券资金流水、IBKR Activity CSV、东方财富 PDF 对账单
- 行情刷新：手动更新持仓价格，或通过 Tushare 后台刷新
- 多用户：JWT 登录、管理员用户管理、管理员持仓视图
- Docker 部署：Nginx 前端反代、FastAPI 后端、外部 PostgreSQL

## 技术栈

- 后端：FastAPI、SQLAlchemy、Alembic、PostgreSQL、Pandas
- 前端：Vue 3、Vite、Pinia、Element Plus、ECharts、Axios
- 部署：Docker Compose、Nginx、TLS 证书

## 快速启动

### Docker Compose

```bash
cp .env.example .env
# 编辑 .env，保留并填写 docker-compose.yml 必需的环境变量

docker compose run --rm backend alembic upgrade head
docker compose up -d
```

访问：

- 前端：`https://app.example.local`
- 健康检查：`https://app.example.local/health`

`.env` 至少需要提供：

- `DATABASE_URL`
- `CORS_ORIGINS`
- `SECRET_KEY`
- `ADMIN_INITIAL_PASSWORD`
- `DEMO_INITIAL_PASSWORD`
- `TUSHARE_TOKEN`
- `NGINX_SERVER_NAME`
- `SSL_CERT_FULLCHAIN`
- `SSL_CERT_PRIVKEY`

首次成功启动后，后端会初始化 `admin` / `demo` 两个用户；密码来自 `.env`。表结构必须先由 Alembic 迁移创建。

### 本地开发

```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env
# 编辑 backend/.env，使用 PostgreSQL DATABASE_URL
alembic upgrade head
ENABLE_DOCS=true uvicorn app.main:app --reload --port 8000
```

```bash
cd frontend
npm install
npm run dev
```

访问 `http://localhost:5173`。

## 文档

- [DEPLOYMENT.md](DEPLOYMENT.md)：部署、升级、备份、恢复、排障
- [DEVELOPMENT.md](DEVELOPMENT.md)：本地开发、迁移、测试、代码规范
- [SAMPLE_DATA.md](SAMPLE_DATA.md)：标准导入和券商导入格式
- [CHANGELOG.md](CHANGELOG.md)：版本变更记录

## 项目结构

```text
investment-tracker/
├── backend/
│   ├── alembic/                 # Alembic migration scripts
│   ├── app/
│   │   ├── api/                 # FastAPI routers
│   │   ├── core/                # auth dependencies and security helpers
│   │   ├── models/              # SQLAlchemy models
│   │   ├── schemas/             # Pydantic schemas
│   │   ├── services/            # holdings, statistics, broker import, price refresh
│   │   ├── config.py
│   │   ├── database.py
│   │   └── main.py
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/
│   ├── e2e/                     # Playwright tests
│   ├── src/
│   │   ├── api/
│   │   ├── components/
│   │   ├── router/
│   │   ├── stores/
│   │   ├── utils/
│   │   └── views/
│   ├── Dockerfile
│   └── package.json
├── data/                        # 原始导入文件/历史文件，保留，不作为运行数据库
├── docker-compose.yml
├── .env.example
└── sample_import.csv
```

## API

开发环境可设置 `ENABLE_DOCS=true` 后访问后端 `/docs` 查看权威 API 文档。生产部署默认关闭 API 文档。

主要能力组：

- `/auth`：登录、当前用户、修改密码
- `/users`：管理员用户管理
- `/api/transactions`：交易 CRUD
- `/api/holdings`：持仓查询、价格更新、行情刷新任务
- `/api/statistics`：汇总、市场/时间统计、FIFO 盈亏、股息、总收益
- `/api/corporate-actions`：公司行动 CRUD 和快捷股息接口
- `/api/exchange-rates`：汇率维护、转换、刷新
- `/api/import/*`：标准文件和券商文件导入
- `/api/export/*`：CSV/Excel 导出

## 备份

运行数据在 PostgreSQL 中，优先使用 `pg_dump` 备份：

```bash
mkdir -p backups
pg_dump "$DATABASE_URL" > backups/investment_$(date +%Y%m%d_%H%M%S).sql
```

也可以使用项目根目录的交互式脚本：

```bash
./backup.sh
```

`data/` 目录只保留原始导入文件和历史文件，不再作为运行数据库位置。
