# Investment Tracker

个人投资交易、持仓、收益、公司行动、汇率和券商流水导入追踪系统。

当前技术栈已经统一迁移到 **PostgreSQL**。生产和本地开发都应使用 PostgreSQL；数据库结构由 Alembic 管理，初始用户通过管理命令创建，应用启动不会自动创建表或写入种子数据。

## 功能

- 交易记录管理：买入、卖出、筛选、导入、导出
- 账户数据：券商账户归属、账户间转账、外部现金事件、导入批次、月末核对和现金管理标的排除清单
- 持仓计算：按交易和公司行动重算数量、成本和当前价格
- 公司行动：现金股息、红股、配股、拆股、合股、税费调整
- 收益统计：持仓表现、FIFO 已实现盈亏、股息收入、区间统计、TTWR、XIRR、回撤和风险指标
- 多币种：汇率维护、换算和双币种展示
- 数据导入：标准交易/公司行动 CSV/Excel、招商证券电子对账单 PDF、IBKR Activity
  CSV / `trade_history.xlsx`，以及东方财富普通股票与港股通两类 PDF 对账单
- 行情刷新：手动更新持仓价格，或通过带 PostgreSQL 持久化状态的后台任务刷新价格与历史行情；Tushare 不完整时可回退腾讯行情
- AI 复盘：可选的 OpenAI 兼容 LLM 报告、追问和定期生成；未配置 API Key 时安全禁用
- 多用户：HttpOnly Cookie、CSRF、防撤销会话、滑动续期、管理员用户管理和管理员持仓视图
- 响应式界面：桌面端与移动端共享导航和核心数据操作
- Docker 部署：Nginx 前端反代、FastAPI 后端、外部 PostgreSQL

招商证券当前支持的正式导入来源是电子对账单的已解密 PDF 工作副本；邮件原件应另行完整保留。
旧版资金流水 Excel 只保留作历史审计与迁移核对，不再作为新增数据的导入入口。

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
docker compose run --rm backend python manage.py seed
docker compose up -d
```

访问：

- 前端：`https://<app-host>`
- 健康检查：`https://<app-host>/health`

`.env` 至少需要提供：

- `DATABASE_URL`
- `CORS_ORIGINS`
- `SECRET_KEY`
- `ADMIN_INITIAL_PASSWORD`
- `DEMO_INITIAL_PASSWORD`
- `NGINX_SERVER_NAME`
- `SSL_CERT_FULLCHAIN`
- `SSL_CERT_PRIVKEY`

首次部署时运行 `python manage.py seed` 初始化 `admin` / `demo` 两个用户；密码来自 `.env`。表结构必须先由 Alembic 迁移创建。

`TUSHARE_TOKEN` 和 `LLM_REPORT_API_KEY` 均为可选能力配置：前者未填写时不能主动从
Tushare 刷新行情，后者未填写时 AI 复盘接口返回“未配置”，定期任务会静默跳过。
启用 AI 复盘前，还应按需检查 `.env.example` 中的模型、超时和输出长度配置。
生成报告和追问会把报告输入数据发送给所配置的外部模型服务；部署者应先确认
数据范围、服务条款和隐私要求。

### 本地开发

```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env
# 编辑 backend/.env，使用 PostgreSQL DATABASE_URL
alembic upgrade head
python manage.py seed
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
- [DATA_ONBOARDING.md](DATA_ONBOARDING.md)：券商账户、现金流和月末核对的数据补全清单
- [BROKER_DATA_SOURCES.md](BROKER_DATA_SOURCES.md)：各券商资料获取方式、保存期限与导入优先级
- [METRICS_AUDIT.md](METRICS_AUDIT.md)：用户可见收益数字的定义、验证方式与口径说明
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

- `/auth`：浏览器 Cookie 登录、API Bearer Token、退出、当前用户、修改密码
- `/users`：管理员用户管理
- `/api/transactions`：交易 CRUD
- `/api/broker-accounts`：券商账户 CRUD
- `/api/cash-events`：证券交易之外的账户现金事件
- `/api/import-batches`：只读导入批次与来源追溯
- `/api/reconciliation-snapshots`：月末券商现金和持仓核对记录
- `/api/excluded-securities`：现金管理类标的排除清单
- `/api/holdings`：持仓查询、价格更新、行情刷新任务
- `/api/statistics`：汇总、市场/时间统计、FIFO 盈亏、股息、总收益、TTWR 收益曲线和历史行情同步
- `/api/corporate-actions`：公司行动 CRUD 和快捷股息接口
- `/api/exchange-rates`：汇率维护、转换、刷新
- `/api/llm-reports`：AI 复盘报告、追问、删除和定期计划
- `/api/import/*`：标准文件和券商文件导入
- `/api/export/*`：CSV/Excel 导出

## 备份

运行数据在 PostgreSQL 中。使用项目根目录的交互式脚本：

```bash
./backup.sh
```

数据库备份先写入 `.dump.partial`，待 `pg_dump` 成功且
`pg_restore --file=/dev/null` 完整读检通过后，才原子改名为 `.dump` 并生成 `.sha256`。
脚本可使用本机 PostgreSQL 客户端，或回退到正在运行的 `backend` 容器。不要把残留的
`.partial` 当作有效备份。详细的恢复演练与升级顺序见
[DEPLOYMENT.md](DEPLOYMENT.md)。

`data/` 目录只保留原始导入文件和历史文件，不再作为运行数据库位置。
