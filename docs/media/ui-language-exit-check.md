# 术语与动作名称验收（2026-10-04）

独立 `codex/ui-language-exit` 基于开放 [#437](https://github.com/measimov/investment-tracker/pull/437) 的实际 `81a86efa`，实施第40.3a节。仅已核对的可见术语、任务名称、对象成功反馈与三文件四处破坏确认复用现有 `confirmAction`；不改金额/日期原值、算法、任务kind、权限、取数、保存/取消/请求序号或财务精度。没有依赖、API、schema或数据迁移。Import布局、warning对比度、404和环图边界明确留40.3b独立补丁。

持仓任务名称统一为“批量分析持仓”“财报摘要回填”，观点任务为“批量生成观点摘要”；原按钮、确认标题、已有阶段反馈沿同对象名称。触及的表单使用标的代码/账户，指标沿平均成本、现价、浮动/未实现盈亏率、已实现收益（含股息）；非港股日期称除权除息日，港股除净日保持。只修实际输出，不机械替换合法证券业务名或模型字段。普通表单保存“确定”保留，破坏确认用删除/停用/移出，原失败/取消与单写控制不变。

| 验收维度 | 实际依据 | 限制与残项 |
| --- | --- | --- |
| 功能与交互 | 根393/1440四动作共8场景：Esc/取消零DELETE，具体动词Enter仅一次严格模拟DELETE，报告/汇率/作者/组合沿原重载 | 不执行真实删除/模型任务或来源刷新；原身份、忙时、token与任务断言保留 |
| 内容与可信度 | 仅字符串与原确认helper，完整finance/controller差异审阅；未改值、排序、原币精度、数量、已实现/未实现口径 | 不将未知补零，不改变港股除净日；总体issues仍需退出矩阵逐项核验 |
| 视觉与风格 | 两张实际确认态：1440×852@1、393×852@2，沿原暖色成熟窗口和宋体标题 | 此组不调整布局/CSS；warning/图表/Import/404实际残项留40.3b |
| 响应式与可访问性 | 根两宽8场景真实键盘确认/取消、对象文案、body/documentfit；公开组件焦点管理不重写 | 仅这些触及动作的实测，不宣称全站读屏认证或所有页面退出已完成 |
| 性能 | 同本机生产日志引用的12路由chunk gzip(level6)142505→142499bytes（-6bytes） | 仅对应chunk静态资源，非整页加载或时延；没有性能提升主张 |
| 工程与可维护性 | 既有confirmAction真实四调用复用，词项仅原调用/精确定位；API真实生成exit0且SHA前后同11f166d5… | 无新label库、组件/确认平台、框架或新镜像unit |

验证时点：首轮单元434通过/5旧除权日文案断言失败；仅现有五项tooltip文案更新后完整439单元/60文件通过（1.78s）。最终类型、Prettier与生产构建5.86s通过，API生成真实exit0且内容哈希一致。9个原spec workers1针对首次54通过/1旧停用确认定位失败（2.1m）；仅原crud确认按钮从“确定/OK”改真实“停用”，增改停用与当前卡联动原断言不变，随后该原用例1通过（13.1s）。不把分次针对相加称修后完整clean；本PR最终远端CI创建后待完成。

根独立证据 `/tmp/ui-confirm-exit-root.json` 八场景全部通过：报告DELETE后列表重新读取，汇率停用后list/latest双重读取，作者与组合移出后collector重载并真实行消失；保留不可恢复/全局汇率/历史发言调仓提示。均为明确虚构响应，真实业务写、外部与JS错误零。

正式图：[桌面删除报告确认](action-confirm.png)、[手机删除报告确认](mobile-action-confirm.png)，复用已有 `reports-reading-fixture.json`，仅浏览器GET与合成管理员展示，不删除真实报告。捕获实际正常读入与原确认窗，等待字体/真实过渡结束；首版桌面在进入动画首帧截到未显示窗，材料重采而非隐藏错误。最终 `/tmp/ui-language-capture.json` 两宽blocked/errors/mockWrites全零。临时API启动脚本首次引用已结束进程导致未起服务，改为现有隔离demo进程环境后两端200，不作为产品失败。

其他证据：`/tmp/ui-language-unit-first.log`、`ui-language-unit-final.log`、`ui-language-typecheck-final.log`、`ui-language-format-check-final.log`、`ui-language-build-final.log`、`ui-language-api-types.log/.json`、`ui-language-resource.json`、`ui-language-e2e-target.log/.exit`、`ui-language-rate-target-final.log`。预览4198/API18119，原针对使用独立test/e2e库，无重seed私有库、部署或issue关闭。

父437 `81a86efa` 完整CI37139642689 SUCCESS、attempt1：3418后端/6跳过（272.89s）、439单元/60文件、162 E2E/4既有跳过（6.6m），无失败或重试；生成漂移/类型/格式/构建通过。首bd5fd83的161/1旧ISO tooltip失败/4和父1项定向10.6s分别保留，不将父完整结果当本PR最终结果。

本阶段438 exactdb1b99c0最终完整CI37140658156 SUCCESS、attempt1：后端3418/6跳过247.08s、439单元/60文件、162 E2E/4跳过7.4m，无失败或重试；实际生成漂移/格式/类型/构建全通过。首轮54/1与定位修后1项分别保留，不推仅历史的新head。
