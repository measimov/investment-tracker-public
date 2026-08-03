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

## 标准公司行动 CSV/Excel

示例模板：

- `sample_corporate_actions.csv`

必需字段：

| 字段 | 说明 |
| --- | --- |
| `symbol` | 股票/资产代码 |
| `market` | 市场，例如 A股、港股、美股、加密货币 |
| `action_type` | 公司行动类型，例如 `CASH_DIVIDEND`、`STOCK_SPLIT`、`REVERSE_SPLIT` |
| `ex_date` | 除权除息日，建议 `YYYY-MM-DD` |

可选字段：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `name` | 空 | 资产名称 |
| `record_date` | 空 | 登记日 |
| `payment_date` | 空 | 支付日/到账日 |
| `dividend_per_share` | 空 | 每股股息 |
| `total_dividend` | 空 | 股息总额 |
| `tax_withheld` | `0` | 预扣税 |
| `tax_rate` | 空 | 税率，例如 `0.10` |
| `net_dividend` | 空 | 税后净股息 |
| `shares_received` | 空 | 股票股息/送股获得股数 |
| `distribution_ratio` | 空 | 分配比例，例如 `10:3` |
| `subscription_price` | 空 | 配股认购价格 |
| `subscription_quantity` | 空 | 配股认购数量 |
| `subscription_amount` | 空 | 配股认购金额 |
| `split_ratio` | 空 | 拆股/合股比例，例如 `1:2` 或 `40:1` |
| `new_shares` | 空 | 拆股/合股后的股数 |
| `cost_basis_adjustment` | 空 | 成本基础调整 |
| `adjusted_quantity` | 空 | 调整后的数量 |
| `adjusted_cost_per_share` | 空 | 调整后的每股成本 |
| `currency` | `CNY` | 币种 |
| `notes` | 空 | 备注 |

前端入口：交易记录页面 -> 导入 -> 标准公司行动文件。

API：

- `POST /api/import/corporate-actions/csv`
- `POST /api/import/corporate-actions/excel`

## 招商证券电子对账单

前端入口：交易记录页面 -> 导入 -> 招商证券对账单。

支持文件：

- 招商证券电子对账单的已解密 PDF 工作副本；邮件原件应另行完整保留

历史资金流水 Excel 可继续作为原始档案保存，但不再作为本项目的导入来源。

导入行为：

- 预览接口会统计总行数、可导入买卖、股息、红利税、重复行、无效行，并把结果
  分成阻断错误和待人工复核警告。
- 预览和正式导入前都必须选择匹配的招商证券账户。账户的脱敏标识需要包含
  PDF 中各证券账号的尾号；不一致时整批拒绝。
- 系统在该账户内按流水 hash 跳过重复记录；同一份原文件不能跨账户重复入账。
- 当前转化证券买入、证券卖出、可明确归属的股息/红利税、天添利等现金管理
  产品的现金收益，以及银行转存/转取、利息归本等现金业务（入账为对应类型的
  现金事件，金额方向与业务类型不符的行只归档报警）。无法唯一归属的红利税
  只保留为来源异常，不猜测挂靠。排除清单内标的的流水只归档不入账。
- 缺少证券代码、无法唯一归属等仍可安全归档的行显示为警告，不阻止正式导入；
  它们不生成公司行动，正式批次标记为 `PARTIAL` 并保留未入账来源。数值错位、
  成交金额无法核对、账户不匹配等错误仍会阻止导入。
- 每一行成功解析的来源都会归档；只有通过账户级持仓预检的批次才会写入正式
  交易。缺少期初持仓或证券转入时整批失败，不留下不完整持仓。

API：

- `POST /api/import/cmb-fund-flows/preview`
- `POST /api/import/cmb-fund-flows`

## IBKR Activity Statement

前端入口：交易记录页面 -> 导入 -> IBKR 活动报表。

支持文件：

- IBKR Activity Statement CSV

导入行为：

- 导入普通股票/ETF 买卖、股息和外国预扣税。
- 现金活动同步入账为现金事件：存款/取款、贷方利息（INTEREST）、借方利息
  （FEE）、外汇兑换（按货币对拆两条币种腿 + USD 佣金）。导入生成的现金事件
  在 API 层只读。
- `调整`（FX 折算损益等纸面项）与期权只归档不入账；方向异常或货币对与
  Price Currency 列不一致的行归档并报警，绝不入错账。
- 必须先选择账户尾号与 CSV 一致的 IBKR 券商账户；同一账户中已经有明确归属
  的重复流水会自动跳过。归属其他账户的既有来源、孤儿来源或链接冲突都会
  整批拒绝，不做静默判重。
- 外国预扣税只有在同账户、同标的、同日期恰有一笔股息时才挂靠；歧义税行保留
  为未入账来源，不猜测归属。

API：

- `POST /api/import/ibkr-activity/preview`
- `POST /api/import/ibkr-activity`

## 东方财富 PDF 对账单

前端入口：交易记录页面 -> 导入 -> 东方财富对账单。

支持文件：

- 已解密的东方财富普通股票明细对账单 PDF
- 已解密的东方财富港股通股票明细对账单 PDF；两份文件分别导入同一个账户

导入行为：

- 导入 A 股、场内基金与港股通买卖、红利、可唯一归属的红利税，以及港股通组合费。
- 普通股票和港股通分别按各自范围核对期末持仓；缺少期初持仓或证券转入时整批
  失败，不把负持仓写入正式账本。
- 每一行成功解析的来源都会归档；逆回购、银证流水和暂不支持类型明确记为未入账，
  不合成买卖。
- 重复流水按所选账户自动跳过；同一份原文件不能跨账户重复入账。

API：

- `POST /api/import/eastmoney-statement/preview`
- `POST /api/import/eastmoney-statement`

## 导出

- `GET /api/export/csv`
- `GET /api/export/excel`

## 券商账户与月末核对（数据补全要点）

- 每个实际账户建一条券商账户记录：券商名称、自定义名称、脱敏标识（只留末四位；
  招商一份对账单含多个股东代码时把各尾号都登记）、基础币种。不保存登录名、
  完整账号、密码或任何凭据。无法确定归属的交易留空账户，不要猜测。
- 月末核对快照记录的是"券商在该日披露了什么"的断言，只用于发现差异；
  快照不能自动生成买入、卖出或现金事件。
- 货币基金等标的可通过账户数据页的特例规则（`/api/security-rules`，
  兼容旧 `/api/excluded-securities`）按用户排除：排除后导入只归档不入账，
  持仓与对账比对双侧忽略。转板映射、名称覆盖、行情缺口豁免与招商现金
  业务口径同在此表维护。
