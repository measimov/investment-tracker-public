# 系统告警阶段实际验收

本阶段基于[#432](https://github.com/measimov/investment-tracker/pull/432)真实父链c3a3c19；用户随后已合入该父PR，本树正常合并main 9cac6d08，独立PR以main为base，遵循[计划第37节](../FRONTEND_UI_UPGRADE_PLAN.md)。独立预览4192/18114；同父旧页4193仅静态对照输出，不改4191既有预览。三张正式图为1440×852@1同输入旧/新及393×852@2手机图；`system-alerts-ui-fixture.json` 全部明确虚构合法响应，管理员标志仅UI场景，不冒充权限证据。没有通知、采集、账本或用户真实写入。

## 实际改变与自查

- 两原读取补各自已知/错误/代次保护和只读GET重试。两区各自成功，不以失败推断健康或没有事件；刷新失败明确保留旧结果。原立即检查和发送测试通知POST/controller、级别/来源/持续时间/渠道/送达/事件设置helper不改，无API或迁移。
- 沿已认可暖白暖灰陶土与宋体页头，必要Naive按钮与本feature两表格、手机完整读卡。当前/已恢复/最近提醒保留全部原字段、脱敏渠道、周期关闭与门槛说明；长错误正文原生details完整展开，宽表原生具名region可聚焦横滚，无通用表格/通知或键盘框架。
- 根首版8状态、十宽、两POST四场景均通过。最后两表复用项目现有useMediaQuery正确初始化手机布局，不新引入媒体封装。
- 对比度实测发现暖底恢复计数及手机已推送NTag成功组合4.4058:1；仅两个真实调用公开colorSuccess使用已有10%成功浅背景，保持原成功文字与状态、全局token不动。最后393/1440所有实测组合最低4.5328:1，不宣称未测状态全部无障碍。

## 六维实际依据

| 维度 | 实际依据 | 范围与限制 |
|---|---|---|
| 视觉与风格 | 同fixture三正式图逐张检查，暖色宋体页头/薄行分隔/数字对齐，手机完整告警标题与来源 | 无装饰动画、主题或通知平台，不自评获奖级 |
| 内容与可信度 | 原三块完整字段，级别/阈值/渠道未配置与失败/送达事实、纯次数与真实零、原formatDateTime上海跨日 | 虚构管理员仅UI；正常截图不隐藏连接错误或通知 |
| 任务与交互 | 原GET分别重试；两POST均原无body，忙时Enter/Space/真实pointer仅一次，成功检查更新、部分发送保留失败渠道、关闭结果、503保旧值 | 所有通知写严格浏览器mock，无真实检查/发送，不重写controller |
| 响应式与可访问性 | 根十宽320–1920、长正文summary Enter全文；桌面原生具名宽表局部横滚，手机44目标；根最后320/393首帧无桌面表闪现、宽→窄不重读，720×450等效CSS重排通过 | 非完整读屏认证；720×450等效CSS重排另记，非literal浏览器缩放 |
| 状态与对比度 | 两区单/双503、首次未知、旧数据失败、真空0、无渠道/apprise缺失/无效渠道；最后两宽透明合成背景最低4.53:1 | 先前4.4058失败准确保留，局部修复不改变global tokens或后端状态 |
| 性能与维护 | 同父c3/合法fixture/1440×852/生产/4倍CPU五轮；末次资源270147gzip bytes，比父234400增加34.91KiB/15.3% | 本地短样本就绪略慢、双库过渡成本，无全面提升结论；仅实际两feature组件，不加拆包机制 |

## 同条件短样本

| 轮次 | 旧FCP(ms) | 新FCP(ms) | 旧就绪(ms) | 新就绪(ms) |
|---|---:|---:|---:|---:|
| 1 | 180 | 152 | 657.4 | 625.1 |
| 2 | 148 | 152 | 591.6 | 618.8 |
| 3 | 152 | 156 | 588.0 | 606.5 |
| 4 | 160 | 148 | 591.7 | 617.7 |
| 5 | 152 | 152 | 581.8 | 613.5 |

FCP中位均152ms，就绪591.6→617.7ms略慢。所有其他本机验收浏览器和完整E2E结束后采样；初样gzip JS234400→270109bytes。末次仅背景属性后单独资源270147bytes，不替换旧五轮计时或混合样本。encodedBodySize为gzip口径；初始GET同为auth/me、notifications/alerts、notifications/events?limit=50。

## 门禁时点与残项

- 428单元/59文件、API类型零漂移通过；3实际定向16.7s通过。之后测试mock补全NotifyResult必填ok/configured/sent/channels.kind、检查counts与实际1行一致，原断言不变；最终完整workers1 E2E145通过/4既有跳过（4.5m）、exit0，无失败或重试。
- 上述完整门禁与五轮计时在最后成功标签背景修正前。最后只有公开背景属性修改，类型/格式/生产构建、两宽原4.5对比度断言和正式capture1项4.1s通过；不把分次定向拼成本地最终完整clean。首轮远端CI37129157145后端3406通过/1失败/6跳过，前端428单元、144通过+1重试通过/4跳过，不能标稳定通过。后端固定有效期样本已过期；前端busy先于mock请求进入。仅对应两测试修复，产品/后端业务/schema保持；最新完整CI待验。
- 真实普通demo身份访问本页返回仪表盘、告警GET403，`/tmp/system-alerts-permissions.json`；虚构admin展示不冒充权限测试。外部/通知真实写/JS错误零。
- 本机完整日志 `/tmp/system-alerts-e2e-complete.log`与`.exit`，`system-alerts-targeted.log`、`system-alerts-unit.log`、`system-alerts-typecheck-contrast-final.log`、`system-alerts-format-contrast-final.log`、`system-alerts-api-types.log`、`system-alerts-build-contrast-final.log`、`system-alerts-contrast-before.json`、`system-alerts-contrast.json`、`system-alerts-performance.json`、`system-alerts-final-resources.json`与`system-alerts-demo-capture-final.log`。根独立 `/tmp/admin-alerts-root-state.json`、`admin-alerts-root-responsive.json`、`admin-alerts-root-actions.json`覆盖上述8状态、十宽、四POST场景；最后`admin-alerts-root-final-wrapper.json`三个真实首帧/切换/720×450重排场景通过，即时resize采样与稳定后几何明确区分，未把过渡瞬时body宽度认作长期溢出。
- 34.2采集开关P2修复已沿真实父链继承，#4307696c4d→#431df9244b→#432c3a3c19。旧#43171bf CI135/4及#43265f CI140/4为父修复前历史；最新父完整CI已另核实：#431df9244b137/4、#432c3a3c19142/4均无失败/重试，后端3407/6、428单元59文件；#4307696c4d则132通过+1重试通过/4跳过（既有app单账户改价用例），不得称clean，该实际退出调查不混入本页业务。父CI不当作本阶段门禁。
- 生成声明无关删除恢复，日志/凭据/临时配置不入库。独立D3来源能力及第15节实际退出残项（账户开关/保存焦点与忙时保护、导入日期/金额展示）继续各自最小PR；总体#353/#362/#367/#368/#369/#370未完全闭环，不自动关闭、合并或部署。

第39节既有测试修复：原Cookie API用例在隔离测试库实际复现good返回422（1失败），只给该正常API样本传入相对当前时间，纯函数默认固定时钟及认证时钟不动。完整Cookie测试63通过（7.95s），ruff/format通过。用户保存测试仅在click后等待mock请求数进入，保留原busy、焦点、重复操作、取消/Esc和最终单写断言；保存用例5次独立重复均通过（26.2s），Prettier通过；新head远端完整CI待验。日志 `/tmp/system-alerts-users-busy-ci-fixed.log`。日志 `/tmp/system-alerts-cookie-ci-before.log`、`/tmp/system-alerts-cookie-ci-fixed.log`。
