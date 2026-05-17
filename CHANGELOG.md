# 更新日志

## Unreleased

### 文档和部署口径

- 统一文档口径为 PostgreSQL + Alembic。
- 明确应用启动不会自动创建数据库表，首次部署必须先执行 `alembic upgrade head`。
- 明确 `data/` 目录只保留原始导入文件和历史文件，不作为运行数据库。
- 清理 SQLite 文件数据库时代的部署和排障说明。
- 合并快速开始和使用指南内容，减少重复维护。

### 已实现能力补充记录

- 支持招商证券资金流水导入和预览。
- 支持 IBKR Activity Statement CSV 导入和预览。
- 支持东方财富 PDF 对账单导入和预览。
- 支持持仓价格手动更新、批量更新和 Tushare 后台刷新任务。
- 支持管理员用户管理和管理员持仓视图。
- 支持账户级总收益、综合已实现收益等统计接口。

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
