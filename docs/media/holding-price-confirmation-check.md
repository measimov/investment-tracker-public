# 现价保存确认验收（2026-10-03）

仅修复持仓 `useHoldingsTable.ts` 的确认时序：草稿作为原保存函数参数，PUT完成前不写当前价格；成功后从原store受序号保护的确认缓存同步该标的，不用草稿或过时GET返回值回填。失败保留已确认价格和估值，paramsKey防止确认读取覆盖已切换的市场，await后沿原卸载守卫。原PUT、PriceEditor、store、价格metadata与多账户共享、费用/FIFO/市值算法和后端/schema/迁移均不变。

根旧版严格模拟证据 `/tmp/holding-price-root-before.json`：393/1440确认价10，PUT12.3456挂起及503后旧页均显示未确认12.3456。这证明展示时序错误，不能据此声称后端丢失数据或新控件引入缺陷。

| 验收维度 | 实际依据 | 边界 |
| --- | --- | --- |
| 功能与交互 | Enter/blur原一次写、Esc不保存和焦点返回；原app真实测试库PUT响应成功后，两账户显示12.3456且GET仍严格核对价格与manual | 不放松金融值或GET断言，不新增保存平台 |
| 内容与可信度 | 挂起/503保持原价10与原汇总，200后两账户更新；单元服务器确认24.5678与草稿25不同也按服务器值更新 | 已确认值与数学helper保持，不将未确认输入用于估值 |
| 视觉与风格 | 未修改任何组件模板、CSS、主题或页头；真实320/1440持仓页操作回归完整通过 | 不重复归档近似默认图，不自评获奖程度 |
| 响应式与可访问性 | 320/1440真实Enter改价、Esc焦点返回；根393/1440四类保存情形及手机真实SPA卸载检查 | 沿成熟PriceEditor焦点机制；无完整读屏认证结论 |
| 性能 | 无依赖/图表/拆包或取数接口变化，原正常确认直接使用现有缓存 | 本轮为金融事实错误修复，不以换库或短时样本声称性能提升 |
| 工程与可维护性 | 仅1个产品文件；复用paramsKey、原store序号/缓存及卸载守卫，3条必要unit与2宽真实503浏览器回归 | 无通用请求、状态或焦点框架；D3/退出残项仍继续 |

431单元/59文件、类型/Prettier/生产构建通过，API生成零漂移；相关controller/store13项通过。首轮新单元仅测试环境缺document而调用原Element通知导致1失败，随后mock已有showApiError；下一轮新断言4913.56与原浮点乘法4913.5599999999995精确比较失败，改为8位近似断言，未改原数学或价格断言。最终两轮环境/断言修正后的13项全通过。

原app改价及320/1440挂起→503→200回归共3项通过（13.2s），随后完整workers1 E2E147通过/4既有跳过（4.5m），exit0，无失败或重试；未将定向与完整数量相加。根 `/tmp/holding-price-root-fixed.json` 8/8独立通过：失败原价/汇总不变、确认与多账户、同key迟200/503保最新14.5678与391.36、跨key25/12.3456与371.91、手机离开后晚响应不复活旧页；3个HoldingResponse虚构样本schema合法，真实写/外部/JS错误零。

日志：`/tmp/holding-price-targeted-unit-complete.log`、`holding-price-unit-full.log`、`holding-price-targeted-e2e.log`、`holding-price-e2e-complete.log/.exit`、`holding-price-typecheck.log`、`holding-price-format.log`、`holding-price-build.log`与`holding-price-api-types.log`。单元另覆盖旧确认读取跨市场与卸载后不回填。所有浏览器模拟写在合法明确虚构行上，原真实写回归仅专用E2E库，无生产行情或账本变化。原 c9ca338d 完整CI37131160323已通过：后端3407/6跳过、431单元/59文件、E2E147/4跳过，无失败或重试。此旧head结果不代表下述review补丁已通过最终CI。


## 在途时改回确认价的 review 修复

[#434 review](https://github.com/measimov/investment-tracker/pull/434#discussion_r4173662352)指出10→20挂起→重新编辑10被确认价同值判断丢弃。新unit在修前2项实际失败，收到[20]而非[20,10]（`/tmp/holding-price-return-target-before.log`）；根320/1440严格模拟亦收到[20]并最终20，见`/tmp/holding-price-return-root-before.json`。此前完整CI和8场景未覆盖此输入，保留这一验收不足。

只在原feature用Map记录每key最新在途目标用于输入比较，估值仍只读取确认价；finally仅对象仍为该key最新时清理。保留连续编辑、store原序号保护/缓存、一次标的PUT、Enter/blur、Esc、metadata、范围和卸载机制，无新的公共状态/保存框架。

修后controller/store16项通过，其中新增3条覆盖回原价的新10确认后旧200/503仍保10、旧完成不得清除新目标、相同在途目标不重复。320/1440×旧200/503四条真实键盘回归及原持仓六条共10项通过（18.2s）；请求严格[20,10]、两个账户10.00和市值¥2,000.00、Esc焦点均保留。完整前端单元434/59、类型/Prettier/生产构建5.83s通过；无schema变动或默认视觉变动，不重复近似截图。此次未重复完整本地E2E；新提交远端完整CI待验，不能将定向10项拼入旧147项称新完整通过。

日志：`/tmp/holding-price-return-target-unit.log`、`holding-price-return-e2e-target.log`、`holding-price-return-unit-full.log`、`holding-price-return-typecheck.log`、`holding-price-return-format-check.log`、`holding-price-return-build.log`。六维原验收的金融/样式边界保持，本次新增明确覆盖同key输入与在途清理缺口。

根最终`/tmp/holding-price-return-root-fixed.json`四场景通过：320/1440×旧200/503，仅[20,10]；未确认时10与总市值300，新10确认manual后旧20不覆盖，桌面两账户10，成本/汇总保持。真实写/外部/JS零，实际视口通过。补丁最终head完整CI待验，沿真实父提交带入D3，既有c9完整通过不能冒充本补丁CI。
