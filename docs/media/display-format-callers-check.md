# 剩余格式调用验收（2026-10-04）

独立 `codex/display-format-callers` 基于开放 [#436](https://github.com/measimov/investment-tracker/pull/436)，按第40.2节仅修改四个真实产品调用：ImportDialog、持仓事件说明、Dashboard成本饼图与useDistributionStats。无API、依赖、数据或schema迁移，财务原值和算法保持。

Import纯日期范围/疑似交易及已入账日期复用formatDate，范围用“至”；已知币种金额复用完整formatCurrency。仅本组件两处未知币种fallback使用原formatNumber与原非空代码，缺码不默认人民币；未知金额配未知代码仍显示“— ZZZ”。成交价格比较与formatPrice精度、数量、预览hash、选择确认与导入payload保持。持仓只格式化eventTooltip，upcoming.date和日期比较仍原ISO。两成本饼图公开percentPrecision=1并沿formatNumber显示一位，金额轴唯一手写人民币调用委托formatCurrency(value,'CNY',0)，原非负0位轴显示保持。announcements.last_synced现已正确使用formatDate，不重复修改。

| 验收维度 | 实际依据 | 边界与残项 |
| --- | --- | --- |
| 功能与交互 | 原cash duplicate实际选源hash、确认后重新预览与commit字段断言保持；原导入乱序、统计日期取消/基准及图表回归8项通过 | 不改导入解析或请求，不将回购利率显示为成交价 |
| 内容与可信度 | 根同一合法五行旧/新两宽：CNY负号前置、HKD真零、未知代码/金额、无币种和1.409→0.085 HKD原精度保持 | 只展示纠正，不更改账本金额；未知币种不默补¥ |
| 视觉与风格 | 两实际明确虚构预览截图，暖色成熟表单/原宽表保留；桌面滚动至金额明细、手机展示日期范围 | 未改页面结构；原警告对比度/未具名账户select与手机环图高亮裁切留第三组，不把格式通过当全部视觉完成 |
| 响应式与可访问性 | 根393/1440导入两场景、两页两宽真实hover四场景，body/documentfit；原表格局部滚动保持 | 格式调整未造ARIA/表单平台；手机图表标签裁切单列实际残项 |
| 性能 | 当前引用的Dashboard/Statistics/Transactions/入口四chunk gzip(level6)合计173327→173390bytes（+63bytes） | 仅本机这些chunk，不代表整页资源/时延；无新五轮采样或性能改善主张 |
| 工程与可维护性 | 复用现有formatter/CURRENCIES/公开series参数；两个既有测试断言按真实文案更新 | 四产品，没有formatter/饼图工厂；总体issues继续开放 |

门禁时点：439单元/60文件、8条原cash import/import race/dashboard/statistics浏览器回归29.9s通过。随后根review发现未知金额null+ZZZ不能丢币种代码，局部fallback收口；另按issue362原文，现有同文件人民币金额轴1行委托formatter。最后类型/Prettier/构建5.86s、事件/公告/helpers共29定向通过，API实际venv Python生成exit0且SHA前后同11f166d5…，不将分次定向拼为最终全量E2E。新PR完整远端CI待完成；父436 exact97a70972完整CI37137124831已通过：3418后端/6跳过、439单元/60文件、162 E2E/4跳过，无失败或重试。

根独立 `/tmp/ui-import-format-root-before.json`、`ui-import-format-root-fixed.json` 各两宽通过，fixture经实际BrokerImportResult验证。日期范围2026/09/29至2026/09/30，现金-¥161,001.61/HK$0.00，未知-123.45 ZZZ/— ZZZ、缺码12.34，1.409→0.085 HKD；第二预览实际携原hash，没有commit、真实写或外呼。首稿root全页option匹配原生筛选与popup两项，限定实际dropdown后正常，不当产品故障。`ui-market-format-root.json` 两页两宽真实A股hover¥474,230.21（45.3%），文字摘要45.3/29.0/25.7一致，body/documentfit、错误零。手机高亮A股canvas label裁切的原图保留给第三组公开参数修复。

正式图：[桌面导入金额明细](import-format.png)1440×852@1、[手机导入日期范围](mobile-import-format.png)393×852@2。同演示库仅GET与严格模拟预览POST，明确虚构PDF/账户/行；没有真实导入。首版capture fixture省略schema默认零字段，股息/红利税格空白；仅材料按实际BrokerImportResult.model_validate(...).model_dump(mode='json')补齐全部默认后重采，产品不变。截图等待字体、弹窗动画及预览成功消息消失；桌面沿原弹窗滚动到明细，金额宽表仍局部滚动。采集JSON `/tmp/display-format-capture.json`，错误/blocked零。

其他证据：`/tmp/display-format-unit.log`、`display-format-unit-final-target.log`、`display-format-browser-target.log/.exit`、`display-format-typecheck-final.log`、`display-format-format-final.log`、`display-format-build-final.log`、`display-format-api-types.log/.json`、`display-format-resource.json`。隔离UI4197/API18118，专用E2E库；未合并/部署/关闭总体issues。

首轮远端bd5fd83完整CI37138796911实际失败：3418后端/6跳过、439单元/60文件通过，E2E161通过/1失败/4跳过（7.7m）。唯一dividend-plan原tooltip仍期待ISO日期，所有重试同一旧展示断言；原8条本机针对未覆盖该spec。仅该原断言显示日期replace(/-/g,'/')，保留原ISO event_date、days_back7、徽标与公告含义，产品不变；原实际用例定向通过（时长见日志），新head完整CI待重验，不以161通过冒称clean。

81a86efa最终完整CI37139642689 SUCCESS、attempt1：3418后端/6跳过272.89s、439单元/60、162 E2E/4跳过6.6m，无失败/重试，实际生成漂移/格式/类型/构建均通过。父原spec定向1通过10.6s与首轮失败分别保留；用户已合入434/435/436，437当前base main仍开放，不部署或关闭总体issues。
