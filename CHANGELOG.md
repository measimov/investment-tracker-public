# 更新日志

## Unreleased

### AI 复盘报告

- 新增可选的 OpenAI 兼容 LLM 复盘报告，可生成账本全量复盘、围绕报告追问并保存对话。
- 支持按周或按月定期生成；未配置 `LLM_REPORT_API_KEY` 时功能安全禁用，不会误发请求。
- 报告明确保留账本中的估算/实验口径，并提示内容不构成投资建议。

### 收益与行情正确性

- 新增按日期区间限定的组合统计、预设区间和请求竞态防护。
- 修复跨市场同代码价格串用、无价持仓合计不对称、股息跨币种汇总和显式零税后股息等问题。
- XIRR 改用浮点求解并缓存同一请求中的汇率查询；用户可见指标增加独立手算向量审计。
- 腾讯日线可在 Tushare 历史覆盖不足时补齐行情，并采用向前翻页保证区间覆盖。

### 账户、导入与核对

- 持仓、FIFO 和内部转账统一按券商账户回放；账户未分配记录继续保留合并兜底视图。
- 新增自动核对快照、组合快照和账户数据页面。
- 标准交易导入可选择券商账户；IBKR 新增 `trade_history.xlsx` 规范格式并保留 Activity CSV 历史回填。
- 招商和东方财富导入补充基金申购、配售及港股通语义，并强化账户归属、重复来源和持仓预检。
- 新增现金管理标的排除清单；命中清单的来源行归档但不入账，也不会把批次误标为部分失败。

### 安全性、后台任务与界面

- 浏览器认证改为 HttpOnly Cookie + CSRF，服务端会话支持注销/改密撤销和滑动续期。
- 后台任务状态持久化到 PostgreSQL，支持租约、接管、重试、陈旧任务中断和完成记录清理。
- 删除前端模拟数据路径，补充响应式导航、移动端布局、加载骨架和空数据状态。
- 初始前端构建改用 Node.js 22，并补充移动端、日期区间、AI 复盘和数据库隔离 E2E 覆盖。

### 数据库基线重置（v1.0 前一次性）

- 9 个历史迁移压缩为单个 `20260728_0001_initial_schema.py`。**不提供从任何旧版本的升级路径**：
  未处于该基线的数据库应当重建而非迁移。
- 所有用户级表的 `user_id` 外键统一为 `ON DELETE CASCADE`，删除用户不再需要按序手工删子表；
  `api/users.py` 中逐表删除的兜底代码随之移除，由 `tests/test_user_cascade_delete.py` 守护。
- 补齐 `broker_accounts`、`cash_events`、`import_batches`、`reconciliation_snapshots`
  若干列的 `server_default`，使模型成为唯一事实来源、autogenerate 不再丢库级默认值。
- 修复 `alembic.ini` 中空的 `timezone =` 导致 `alembic revision --autogenerate` 直接报错。

### 文档和部署口径

- 统一文档口径为 PostgreSQL + Alembic。
- 明确应用启动不会自动创建数据库表，首次部署必须先执行 `alembic upgrade head`。
- 明确 `data/` 目录只保留原始导入文件和历史文件，不作为运行数据库。
- 清理 SQLite 文件数据库时代的部署和排障说明。
- 合并快速开始和使用指南内容，减少重复维护。
- PostgreSQL 备份统一为 `.partial` 写入、`pg_restore` 完整读检、原子改名并生成 SHA256。

### 已实现能力补充记录

- 新增券商账户归属、导入批次追溯、账户现金事件和持仓核对基础层。
- 收益统计中的账户收益、TTWR、风险与标的胜率明确标记为估算或实验口径，
  不再与完整账户业绩混为一谈。
- 支持招商证券电子对账单 PDF 导入和预览；旧资金流水 Excel 仅保留作历史审计与迁移核对。
- 支持 IBKR Activity Statement CSV 导入和预览。
- 支持东方财富 PDF 对账单导入和预览。
- 支持持仓价格手动更新、批量更新和 Tushare 后台刷新任务。
- 支持管理员用户管理和管理员持仓视图。
- 支持账户级总收益、综合已实现收益等统计接口。
- 支持标准公司行动 CSV/Excel 导入并在导入后重算受影响持仓。
- 支持历史行情缓存、增量同步，以及 TTWR 收益曲线、回撤和风险指标。
- FIFO 统计会显式报告历史超卖记录，不再静默隐藏数据质量问题。
- 关键页面补充首次加载骨架屏和空数据状态。

## v1.2.0 - 2026-02-02

### 新增功能：FIFO 盈亏计算与性能统计

#### 核心算法

- FIFO 先进先出算法：计算已实现盈亏。
- 公司行动整合：FIFO 队列处理送股、配股、拆股。
- 批次级盈亏：基于 FIFO 剩余批次计算未实现盈亏。
- 三个统计维度分离：当前持仓表现、历史交易能力、股息收入。

#### 后端 API

- `POST /api/statistics/current-holdings-performance`
- `GET /api/statistics/realized-pnl-fifo`
- `GET /api/statistics/dividend-summary`

## v1.1.0 - 2026-02-02

### 新增功能：公司行动管理

- 支持现金股息、股票股息、配股、拆股、合股、送股等公司行动。
- 支持股息税务和税后净额记录。
- 公司行动会参与持仓成本和收益统计。

#### API

- `POST /api/corporate-actions/cash-dividend`
- `POST /api/corporate-actions/stock-dividend`
- `GET /api/corporate-actions/`
- `GET /api/corporate-actions/statistics/summary`

> 历史说明中的“升级会自动创建新表”已失效。当前表结构由 Alembic 管理，部署和升级均应显式执行 migration。

## v1.0.0 - 2026-02-01

### 初始版本

- 交易记录管理。
- 持仓自动计算。
- 收益统计分析。
- 数据可视化。
- CSV/Excel 导入导出。
- Docker 容器化部署。
