# 手机交易卡片审阅修复（P2 第 10 项）

基于 codex/ui-review-analysis，产品变更仅 TransactionsTable.vue；transactions-pilot.spec.ts 更新备注入口和键盘行为断言。原局域网开发工作树热更新预览，截图均为虚构数据，未写真实账本。

编辑、删除移至类型同组的右上区域，移除底部操作分割行。备注入口与账户/手续费同行，展开全文独立占满卡片宽度；保留换行、长链接、键盘展开与关闭、44px触达、只读及转仓限制。复用现有展开状态并继续在结果变化时清理；桌面表格与金额格式不变。

- 四态截图：1440 / 393 × 浅色 / 深色，见本目录。
- Chromium 与 Linux WebKit：320 / 393 / 1440 × 浅深共 12 状态，验证金额精度、长名称和链接、无外层横溢出、空备注、完整只读文案、权限、键盘开合及入口位置稳定。
- 393px 常规虚构卡片高约 200px；320px 自然换行约 249px，不通过截断账户或名称压低高度。
- 最终全量前端 E2E（含 transactions-pilot）：178 passed / 4 skipped，剩余一项为持仓币种位置的旧断言，修正后在新隔离库复跑完整 app 与 transactions-pilot 共 21 项通过。179 个非跳过用例均已覆盖；格式检查、typecheck、459 单元与构建通过。

Linux WebKit 不等于 iOS 真机。原始日志与额外截图：主目录 backups/ui-bug-bash-20261004/review-applied/p2_10、p2_10_stress、p2_10_webkit。该 PR 可独立 revert；原有配色试验 patch 仍保留，不能跨后续变动盲目反向套用。
