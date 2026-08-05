# Investment Tracker

个人投资交易、持仓、收益、公司行动、汇率和券商流水导入追踪系统。

运行数据库统一为 **PostgreSQL**；结构由 Alembic 管理，初始用户通过管理命令创建，应用启动不会自动创建表或写入种子数据。

## 功能

- 交易记录管理：买入、卖出、账户间转仓、筛选、导入、导出
- 账户数据：券商账户归属、外部现金事件、导入批次追溯、月末对账快照与自动比对
- 持仓计算：按交易和公司行动重算数量、成本和当前价格（账户级持仓）
- 公司行动：现金股息、红股、配股、拆股、合股、税费调整；A/B 股分红公告自动同步为建议（Tushare，确认后入账）+ 持仓标的未来事件角标（财报披露/分红预案/限售解禁）
- 收益统计：持仓表现、FIFO 已实现盈亏、股息收入；账户级收益为权益仓口径（仅证券投入），组合指标另标实验；TTWR 曲线可叠加基准指数对比（沪深300/恒生/标普500，超额收益为价格指数算术差）
- 多币种：汇率维护、换算和双币种展示
- 数据导入：标准交易/公司行动 CSV/Excel、招商证券电子对账单 PDF（含现金业务入账）、
  IBKR Activity CSV（含存款/利息/外汇入账）、东方财富普通股票与港股通 PDF 对账单
- 现金管理标的排除清单：货币基金等标的可按用户排除出持仓与对账口径
- AI 复盘：DeepSeek 生成投资复盘报告与追问对话，手动或定期触发
- 标的档案（A股/美股/港股）：基本面数据入库（A股=Tushare 财务指标/三大报表/风险信号，美股=SEC EDGAR XBRL，港股=Yahoo 年度科目）、财报全文智能摘要（A股年报 + 美股 10-K，十年回填）、商业画像与产业链估值因子、利润质量指标（含 Beneish M-score）、同业名单；LLM 生成结构化标签+全文分析（持仓列表标签列，详情页全文），生成过程带阶段进度；持仓页可一键批量分析（串行、可终止、24 小时内已分析的自动跳过，并对各数据源自适应限流）
- 行情刷新：手动或后台任务刷新价格与历史行情（PostgreSQL 持久化任务状态）
- 多用户：HttpOnly Cookie 登录、CSRF 防护、管理员用户管理、管理员持仓视图
- Docker 部署：Nginx 前端反代、FastAPI 后端、外部 PostgreSQL

招商证券当前支持的正式导入来源是电子对账单的已解密 PDF 工作副本；邮件原件应另行完整保留。

## 技术栈

- 后端：FastAPI、SQLAlchemy、Alembic、PostgreSQL、Pandas
- 前端：Vue 3、Vite、Pinia、Element Plus、ECharts、Axios
- 部署：Docker Compose、Nginx、TLS 证书

## 快速启动（Docker Compose）

```bash
cp .env.example .env
# 编辑 .env，保留并填写 docker-compose.yml 必需的环境变量

docker compose run --rm backend alembic upgrade head
docker compose run --rm backend python manage.py seed
docker compose up -d
```

访问 `https://<app-host>`；健康检查 `https://<app-host>/health`。
`seed` 初始化 `admin` / `demo` 两个用户，密码来自 `.env`。
必需环境变量清单见 [.env.example](.env.example) 与 [DEPLOYMENT.md](DEPLOYMENT.md)。

本地开发（后端 venv + 前端 vite dev）步骤见 [DEVELOPMENT.md](DEVELOPMENT.md)。

## 文档

- [DEPLOYMENT.md](DEPLOYMENT.md)：部署、升级、备份、恢复、排障
- [DEVELOPMENT.md](DEVELOPMENT.md)：本地开发、迁移、测试、代码规范
- [SAMPLE_DATA.md](SAMPLE_DATA.md)：标准导入和券商导入格式
- [BROKER_DATA_SOURCES.md](BROKER_DATA_SOURCES.md)：各券商资料获取方式与导入优先级
- [METRICS_AUDIT.md](METRICS_AUDIT.md)：展示指标的数字审计口径

## API

开发环境可设置 `ENABLE_DOCS=true` 后访问后端 `/docs` 查看权威 API 文档。生产默认关闭。

主要能力组：

- `/api/auth`：浏览器 Cookie 登录、API Bearer Token、退出、当前用户、修改密码
- `/api/users`：管理员用户管理
- `/api/transactions`：交易 CRUD 与账户间转仓（`POST /transfer`）
- `/api/broker-accounts`、`/api/cash-events`、`/api/import-batches`
- `/api/reconciliation-snapshots`：月末对账快照与自动比对
- `/api/holdings`：持仓查询、价格更新、行情刷新任务
- `/api/statistics`：汇总、市场/时间统计、FIFO 盈亏、股息、TTWR 曲线、组合快照
- `/api/corporate-actions`、`/api/exchange-rates`、`/api/security-rules`（账本特例规则；`/api/excluded-securities` 为兼容路由）
- `/api/llm-reports`：AI 复盘报告生成、追问、定期计划
- `/api/import/*`、`/api/export/*`：文件导入与 CSV/Excel 导出

## 备份

运行数据在 PostgreSQL 中，使用根目录 `./backup.sh`（`.partial` → 读检 → 原子改名 → SHA256）。
恢复演练与升级顺序见 [DEPLOYMENT.md](DEPLOYMENT.md)。`data/` 目录只保留原始导入文件。
