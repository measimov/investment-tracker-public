# 券商资料获取与导入优先级

更新日期：2026-07-26

本文区分三类事实：

- **官方确认**：券商帮助、合同或官方投教材料明确说明
- **项目已验证**：本项目已有真实样本和解析器，但不代表券商长期承诺格式不变
- **待账户核验**：公开资料不足，需要在实际客户端确认

## 推荐顺序

1. **汇丰香港**：先下载最近 3 个月的 eAdvice，再下载仍可取得的 24 个月投资/
   证券结单；电子通知的在线保存期最短，最容易先丢失。
2. **IBKR**：优先保留 `trade_history.xlsx` 规范导出；历史 Activity Statement
   CSV 继续用于回填，PDF 用于人工核验。后续再考虑 Flex Query 自动化。
3. **东方财富证券**：按客户端允许的最长区间，分别申请普通股票明细对账单和
   港股通股票明细对账单。两份原始 PDF 互为补充，应分别导入同一个东方财富
   账户。
4. **招商证券**：保存邮件发送的原始电子对账单；若原件加密，另存一份已解密
   的 PDF 工作副本用于导入。以前下载的“历史资金流水”Excel 可以继续作为
   原始档案保留，但不再作为本项目的导入来源。

## 各券商资料

| 券商         | 优先获取                                          | 可用于核验的内容                                                   | 当前项目支持                                                                                                | 主要缺口                                                                                         |
| ------------ | ------------------------------------------------- | ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| 招商证券     | 电子对账单原件 + 已解密 PDF 工作副本              | 成交、金额、现金余额、剩余数量和费用                               | 已解密 PDF；正式导入证券买卖、可归属股息/红利税及现金管理产品收益                                           | 对账单缺少证券代码时不猜测红利税归属；区间之前的成交和部分非交易现金业务仍需补充或核对           |
| 东方财富证券 | 普通股票明细对账单 PDF + 港股通股票明细对账单 PDF | A 股、场内基金、港股通成交、红利、税、港股通组合费及期末持仓       | 两份解密 PDF 分别导入同一账户；转化股票/场内基金/港股通买卖、红利、红利税，并把港股通组合费记为费用现金事件 | 逆回购、银证流水等其他语义尚未完整转化；所有已解析来源行均保留，不支持的语义显式跳过且不合成交易 |
| IBKR         | `trade_history.xlsx` + Activity Statement CSV/PDF | 交易、期末持仓、各币种现金、出入金、股息、利息、费用、税和公司行动 | `trade_history.xlsx` 为后续规范格式，Activity CSV 用于历史回填；转化股票/ETF 买卖、股息、外国预扣税及少量已知转板 | 规范 xlsx 不含账户列，需人工确认归属；期权、外汇、利息和完整现金活动尚未入账；Flex 尚未接入 |
| 汇丰香港     | 投资/证券综合结单、交易 eAdvice、同期结算账户 CSV | 月末持仓、交易通知、结算现金流水                                   | 尚无专用解析器；可先录月末核对快照                                                                          | 个人证券交易缺少稳定的结构化 CSV/API，预计需要 PDF 解析                                          |

## 本地样本验证（不含持仓明细）

三份工作副本均只在一次性隔离数据库与测试券商账户中验证；测试库已清理，文档
不记录真实账户尾号、标的名称、持仓数量、逐笔交易日期或精确业务数量：

- **东方财富证券普通股票对账单**：买卖、股息和红利税均可解析。账户历史仍
  缺少区间前持仓依据，正式导入被安全拒绝；
  失败批次为 `FAILED`，业务记录和来源归档均为 0。
- **东方财富证券港股通对账单**：买卖和组合费均可解析；持仓数量对账为
  `MATCHED`，可完整导入并在同账户重复导入时保持幂等。
- **招商证券**：买卖、可归属股息和现金管理产品收益均可解析；少量缺少证券
  代码的红利税仅保留为人工复核警告。账户历史仍缺少区间前持仓依据，因此
  正式导入整批 `FAILED`，
  `imported=0 / archived=0`，没有污染交易、公司行动、现金事件或持仓。

招商入口现已迁移为 PDF-only：电子对账单可以预览并正式导入，历史资金流水
Excel 不再接受。所有券商文件在正式导入时都必须选择匹配账户，以保证账户级
去重和核对；预览同样必须选择匹配账户。PDF 与历史 Excel 重叠时，仅在证券账号
和经济事实完全一致的情况下逐笔承接旧交易；价格或金额存在近似冲突时整批停止，
避免跨格式重复记账。

验证也说明“解析成功”不等于“账户已对平”。正式导入前仍需补齐期初持仓，
或取得更早的历史文件；无法确认成本或行语义时，不应猜测生成买入交易。
自动 `MATCHED` 只表示对账单范围内的证券数量一致；现金余额目前作为券商断言
保存，尚未自动与完整现金账核对。

## 官方资料

- 招商证券：
  [PC 客户端](https://yht.cmschina.com/product.html)、
  [PC 客户端隐私政策](https://yht.cmschina.com/about.html)
- 东方财富证券：
  [电子对账单查询指南 PDF](https://edu.18.cn/pdf/%E5%85%B3%E7%88%B1%E8%80%81%E5%B9%B4%E6%8A%95%E8%B5%84%E8%80%85_%E6%8F%90%E9%AB%98%E9%80%82%E8%80%81%E5%8C%96%E6%9C%8D%E5%8A%A1%EF%BC%88%E6%9D%83%E9%99%90%E5%BC%80%E9%80%9A%E4%B8%8E%E6%9F%A5%E8%AF%A2%E6%8C%87%E5%BC%95%E6%89%8B%E5%86%8C%EF%BC%89.pdf)、
  [电子印章验证](https://eleseal.eastmoneysec.com/ele-seal-verification)
- IBKR：
  [运行结单](https://www.ibkrguides.com/complianceportal/howtorunastatement.htm)、
  [结单保存期限](https://www.ibkrguides.com/complianceportal/statements.htm)、
  [现金报告](https://www.ibkrguides.com/reportingreference/reportguide/cashreport_default.htm)、
  [出入金](https://www.ibkrguides.com/reportingreference/reportguide/deposits-and-withdrawals_default.htm)、
  [期末持仓](https://www.ibkrguides.com/reportingreference/reportguide/openpositions_modelstatement.htm)、
  [公司行动](https://www.ibkrguides.com/reportingreference/reportguide/corporateactions_default.htm)、
  [Flex Query](https://www.ibkrguides.com/advisorportal/ug/flex.htm)
- 汇丰香港：
  [eStatement 与 eAdvice](https://www.hsbc.com.hk/accounts/estatement/)、
  [个人综合账户条款 PDF](https://www.hsbc.com.hk/content/dam/hsbc/hk/docs/accounts/personal-integrated/terms-and-conditions.pdf)、
  [股票服务](https://www.hsbc.com.hk/investments/products/stocks/hk-trading/)

## 采用的账本原则

- 券商原文件保存事实，人工只补主观意图、缺失现金流和少量异常修正。
- 证券买卖不是外部入出金；账户之间的内部转账也不能重复计入组合本金。
- 每次实际导入都保存文件摘要、解析器版本、导入结果和重复行计数。
- 每月用券商结单核对各币种现金和证券数量；A 股还可使用
  [中国结算证券查询服务](https://www.chinaclear.cn/zdjs/scxfw/201306/197c82ddec6a4dc58d5b953f19339c7d.shtml)
  作为独立持仓核验源。
- 月末核对快照记录的是“券商在该日披露了什么”的断言，只用于发现差异；快照
  不能自动生成买入、卖出或现金事件。
- 解析器暂不理解的业务行必须作为显式跳过项保留在预览或导入批次结果中，不能
  为了对平而猜测成交易。
- 招商证券缺少证券代码或无法唯一归属的红利税属于可归档警告：允许继续导入其余
  可确认记录，但批次保持 `PARTIAL`；数值、金额或账户校验失败仍属于阻断错误。
- 主观回测从稳定记录事前论点、反证和失效条件的日期开始；更早历史可以统计
  收益，但不补写并不存在的“当时判断”。

这些原则与
[Portfolio Performance 导入模型](https://help.portfolio-performance.info/en/reference/file/import/)、
[CSV 账户模型](https://help.portfolio-performance.info/en/reference/file/import/csv-import/)
和
[Beancount 的去重与余额核验实践](https://beancount.github.io/docs/getting_started_with_beancount/)
一致。
