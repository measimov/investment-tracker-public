# 现金流水跨文件判重与历史清理

招商对账单重叠区间中，同一笔回购业务的利率可能从两位变成三位小数。原始 `row_hash` 包含价格/利率，仍保持原算法和原字段；只靠指纹会把同一现金事实再次入账。

招商导入器 v16 在预览与正式导入使用同一个现金语义判重入口，覆盖现金业务、现金管理产品利息及实际扣税日税款。账户、币种、日期、业务、事件类型、带符号数量、发生金额、各项费用、余额、结算汇率及流水/合同号必须一致，价格精度差异不再绕过保护。匹配按已入账事实数量消费额度；同批真实多笔等值流水仍能入账。历史来源别名共用同一现金事实时，只算一笔。

疑似行保留为来源归档，不生成现金事件。用户勾选「真实的另一笔流水」重新预览再导入，可在原来源行上转正；不确认则重导按重复跳过。源行、原文件和指纹保留，交易判重规则不变。

## 历史清理

`backend/scripts/repair_cash_duplicates.py` 只接受显式、逐笔核实的来源配对，参数是**来源流水 ID**，不是现金事件 ID。当前清理限定为招商跨文件、价格/利率精度不同的同向转入/转出，拒绝跨用户/账户、不同费用/余额、税分摊或额外来源引用。不会扫描并自动删除所有等值流水。

```bash
cd backend
python scripts/repair_cash_duplicates.py --user-id <USER_ID> \
  --pair <DUPLICATE_SOURCE_ID>:<KEEP_SOURCE_ID> \
  --out ../backups/cash-duplicate-plan.json
python scripts/repair_cash_duplicates.py --user-id <USER_ID> \
  --apply-reviewed-plan ../backups/cash-duplicate-plan.json \
  --out ../backups/cash-duplicate-applied.json
```

默认只读。计划记录两侧来源/现金事实的完整字段及受影响快照的现金 before/after；执行时持用户导入锁重建并逐字段核对，变化或阻断就拒绝。生产执行前在恢复库验证并冻结价格、汇率和业务日期。清理将重复来源关联到保留事实，再删除多出来的派生现金事件，保留两份原始来源与 hash，并在同一事务刷新受影响对账快照。二次生成计划应零动作。

若随后升级历史红利税，应先清理已证实的重复现金，再重新生成默认无阻断税款计划；不以 `--preserve-existing-cash-delta` 跳过已知重复，也不把差额吸收到残差锚点。具体税款升级见 [股息口径](DIVIDEND_ACCOUNTING.md#日期独立的股息税374)。两类数据任务均不下载、不调用 LLM，无新增 env 或结构迁移。私有逐笔清单写入 Git 忽略目录。
