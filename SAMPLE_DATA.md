# 导入数据说明

本文件说明标准导入模板和当前支持的券商导入格式。`data/` 目录中的原始文件会保留，不作为运行数据库。

## 标准 CSV/Excel

示例模板：

- `sample_import.csv`

必需字段：

| 字段 | 说明 |
| --- | --- |
| `symbol` | 股票/资产代码 |
| `market` | 市场，例如 A股、港股、美股、加密货币 |
| `transaction_type` | `BUY` 或 `SELL` |
| `quantity` | 数量 |
| `price` | 成交价格 |
| `transaction_date` | 交易日期，建议 `YYYY-MM-DD` |

可选字段：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `name` | 空 | 资产名称 |
| `fee` | `0` | 手续费 |
| `currency` | `CNY` | 币种 |
| `notes` | 空 | 备注 |

前端入口：交易记录页面 -> 导入 -> 标准交易文件。

API：

- `POST /api/import/csv`
- `POST /api/import/excel`

## 招商证券资金流水

前端入口：交易记录页面 -> 导入 -> 招商证券资金流水。

支持文件：

- 招商证券导出的 Excel 历史资金流水

导入行为：

- 预览接口会统计总行数、可导入买卖、股息、红利税、重复行、无效行。
- 正式导入会按流水 hash 跳过重复记录。
- 当前只导入证券买入、证券卖出，以及已实现支持的股息/税费记录。

API：

- `POST /api/import/cmb-fund-flows/preview`
- `POST /api/import/cmb-fund-flows`

## IBKR Activity Statement

前端入口：交易记录页面 -> 导入 -> IBKR 活动报表。

支持文件：

- IBKR Activity Statement CSV

导入行为：

- 导入普通股票/ETF 买卖、股息和外国预扣税。
- 期权、外汇、利息和现金类记录暂不导入。
- 重复流水会自动跳过。

API：

- `POST /api/import/ibkr-activity/preview`
- `POST /api/import/ibkr-activity`

## 东方财富 PDF 对账单

前端入口：交易记录页面 -> 导入 -> 东方财富对账单。

支持文件：

- 已解密的东方财富 PDF 股票明细对账单

导入行为：

- 导入股票买卖、红利入账和红利税。
- 基金、逆回购、银证流水和暂不支持类型会跳过。
- 重复流水会自动跳过。

API：

- `POST /api/import/eastmoney-statement/preview`
- `POST /api/import/eastmoney-statement`

## 导出

- `GET /api/export/csv`
- `GET /api/export/excel`
