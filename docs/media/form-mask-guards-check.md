# 成熟录入表单遮罩保护验收（2026-10-03）

按[计划第30节](../FRONTEND_UI_UPGRADE_PLAN.md)，依赖 [#424 AI复盘](https://github.com/measimov/investment-tracker/pull/424)。本轮产品差异只有 `TransactionFormDialog`、`CorporateActionFormDialog`、`OpeningCostDialog` 三处现有ElDialog的公开 `close-on-click-modal=false`；第一处为Prettier展开。没有组件迁移、草稿存储、离开确认、金额/数量/payload或控制器改动，无API和数据库迁移。

已观察基线为填写未保存虚构备注→点击遮罩关窗→重开草稿消失；期初补录为明确虚构UI-OPEN导入只读记录、数量12.125、成本未知。根基线 `/tmp/retained-form-mask-baseline.json`、`opening-cost-mask-baseline.json` 没有保存请求。

新增一条真实浏览器回归覆盖三弹窗：1440桌面填写备注→点实际面板外遮罩仍可见且草稿保留→取消无写入→焦点回入口；重开仍按原规则恢复空值/原备注，Esc回入口。393手机验证三入口、输入/取消/Esc及焦点，期初补录继续原fullscreen。所有业务非GET被拦截，写请求为0，原保存/关闭/响应式实现保持。

该回归与既有交易编辑/删除、证券输入目录填充、重复现金导入、取消导入共5项定向通过（`/tmp/form-mask-guards-regression.log`）；格式、类型和生产构建通过。两次初试失败为测试误测role外层全屏容器与期初入口名称缺日期，改成实际内层面板/真实具名入口后通过，没有修改产品逻辑或放宽草稿断言。本轮不重复整套本地；已交付[#425](https://github.com/measimov/investment-tracker/pull/425)，最新0745027完整远端CI37110550376通过：后端3407/6跳过、单元428、E2E118/4跳过，无失败或重试。

视觉与财务显示无变化，无需重复同样正式图或性能采样。独立生产预览4185/API18107使用原虚构demo库，未seed/迁移，外部凭据/周期任务/worker关闭；临时脚本、凭据和日志不入库。#369其他子项与全站控件迁移仍按计划验收，不由这三个成熟表单保护关闭总体issue。

根独立393/1440×三弹窗6场景也通过（`/tmp/form-mask-guards-root.json`）：草稿保留、Cancel/Escape主动关闭并回入口、重开原值，业务writes/pageerrors为0。依赖#424最新完整远端CI37109880415为后端3407/6跳过、428单元、E2E117/4跳过，无失败或重试；不替代本PR后续完整CI。
