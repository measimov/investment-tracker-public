# 雪球来源入口能力验收（2026-10-03）

在现价确认 [#434](https://github.com/measimov/investment-tracker/pull/434) 最新 review 补丁 `256d627` 上正常快进，独立 `codex/xueqiu-capabilities`，不重复父改价文件。本轮为已确认D3：无配置且无对应可读历史时隐藏普通用户的相应雪球入口，管理员仍可配置；失败未知保守可见，不阻登录。只新增认证只读 `/api/capabilities` 与两个对象 `opinions` / `xueqiu_symbol_feed` 的 available/reason，available不是来源健康或探活结果。

配置沿原is_configured本地presence；观点历史为全局Utterance或SecurityOpinionSummary，帖子历史为announcement/discussion XueqiuSymbolPost。沿原全局阅读权限，不按个人持仓限制，不把成功空ScanRun当可读历史，不调用source_freshness吞错或会创建state的build_status。DB失败真实500，没有Cookie值/路径/文件信息、外呼、INSERT/commit、模型或迁移。

| 验收维度 | 实际依据 | 边界与残项 |
| --- | --- | --- |
| 功能与交互 | App真实链接、Opinions直达守卫、详情两区和持仓原观点badge/tag/filter共用一个窄Pinia结果；Cookie原updated钩子再读。真实登录503仍成功、SPA登出/登录ABA旧响应完成后不覆盖，Cookie严格mock单PUT/probe:false后第二GET | Watchlist没有此徽标不新增；任务/采集/付费生成控制器和官方公告保持 |
| 内容与可信度 | 两个历史独立：仅帖子时无观点导航/摘要/作者，但帖子可读；仅摘要时不制造帖子入口；null未知不当false。配置presence与来源可用原语义分开 | 无凭据探活或新权限定义；普通用户也保持原全局可读历史 |
| 视觉与风格 | 未改CSS/字体/主题/组件结构；补充桌面无源与手机仅帖子图已逐张核对，暖白衬线标题与既有阅读层级保留，无错误横幅/notification | 两张状态图不是全站重拍或获奖评价；各页原视觉证据仍有效 |
| 响应式与可访问性 | 根393/1440×六状态12/12，官方公告、真实导航、帖子Enter及讨论切换、body/document四边均通过；新增320/1440帖子/摘要四场景真实键盘通过 | 无新的焦点/ARIA框架，不称完整读屏认证；退出§40尚待独立完成 |
| 性能 | 首载与直达守卫复用同一pending，通常一次检查；会话/用户变化或显式Cookie更新重读，登录不等待。相同预览构建入口chunk raw411750→413117，Python gzip level6 133492→133947（+455bytes/约0.44KiB） | 仅入口chunk本机产物口径，非整页已加载资源/时延，未做新五轮采样，不声称整体性能提升 |
| 工程与可维护性 | 单feature store复用useLatestRequest/session-changed；后端一次三EXISTS；真实Pinia环境、契约类型生成幂等、后台失败unknown | 无registry、跨用户localStorage、通用状态平台、新依赖或迁移；总体issues不自动关闭 |

实际门禁：后端完整3418通过/6既有跳过（163.43s），ruff469文件格式与检查通过，exit均0。11条新增后端回归涵盖401/停用原400、普通/admin、配置presence、独立历史/ScanRun-only、只读SELECT与DB失败；前端完整438单元/60文件通过，类型/Prettier/生产构建6.74s通过，新契约生成前后SHA相等（新增生成差异52行正是本接口）。首轮完整workers1 E2E156通过/2失败/4既有跳过（4.8m），失败均旧navigation.spec普通未配置场景仍固定期待10个链接，实际按本轮确认范围隐藏观点后9个。两处仅改实际9并明确观点count0，管理员13及所有focus/newtab/route断言保留，两项导航针对通过（14.9s）后，重新完整workers1 E2E158通过/4既有跳过（4.7m）、exit0，无失败/重试；不能将针对检查拼成完整门禁。

首轮新测试依据问题分开记录：后端只读observer放在TestClient lifespan前捕获原启动background_jobs UPDATE，限定到实际GET后仍严守只SELECT；store测试误写未公开authenticated导致4失败，改真实auth.login与activePinia后9针对通过，产品状态不改以迎合断言。首次针对19通过（23.9s）属于旧父c9；继承256后最终受影响25通过（1.3m），其中Cookie刷新/失败登录/明确旧response结束后ABA保留与原CRUD/全页手机断言完整。未放松金融/权限/单写断言，所有浏览器Cookie写严格mock；身份用例仅专用E2E临时用户并清理，不改真实用户/凭据。

补充图：[桌面无来源](xueqiu-capabilities.png) 1440×852@1、[手机仅帖子历史](mobile-xueqiu-capabilities.png) 393×852@2。来源为既有research/opinions明确虚构fixture与合成历史帖子，只浏览器GET拦截，不入库/采集/生成。桌面保留官方公告且无雪球观点导航/tab，手机摘要/作者不伪造但历史讨论完整可读。采集证据 `/tmp/xueqiu-capabilities-capture.json`：0真实写/外呼/JS错误，两宽body/document等于viewport。正常演示库 `investment_ui_pilot_demo` 只读，UI4195/API18116；测试使用独立test/e2e库。

证据：`/tmp/xueqiu-capabilities-root.json`（根12场景）、`xueqiu-capabilities-root-feed-fixture.json`（实际schema验证）、`xueqiu-capabilities-backend-complete.log/.exit`、`xueqiu-capabilities-backend-lint.log`、`xueqiu-capabilities-backend-format.log`、`xueqiu-capabilities-unit-final.log`、`xueqiu-capabilities-browser-targeted.log`、`xueqiu-capabilities-browser-final-targeted.log`、`xueqiu-capabilities-e2e-complete-first.log/.exit`、`xueqiu-capabilities-navigation-target.log`、`xueqiu-capabilities-e2e-complete.log/.exit`、`xueqiu-capabilities-format-final.log`、`xueqiu-capabilities-typecheck-final.log`、`xueqiu-capabilities-types-{before,after}.sha`、`xueqiu-capabilities-build-final.log`、`xueqiu-capabilities-resource.json`、`xueqiu-capabilities-capture.json`。

父434 c9完整CI结果见计划38.1：后端3407/6、431单元/59、E2E147/4无失败重试。后续256 review补丁完整CI37134192858已通过：后端3407/6、434单元/59、E2E151/4（6.7m），无失败或重试；旧c9与新256结果分开记录。本次不合并/部署/关闭整体issue，后续按已存档§40三个独立小补丁推进。

后续本机补证（2026-10-04）：最后一次本机generate调用未加入Python环境PATH，实际python not found，随后cmp成功不足以证明生成成功。已在原D3干净树与账户保护树用venv PATH及显式退出码重新生成，均exit0且SHA256前后同`11f166d5b50ddc44ef595f8f94dae7b2e31aa1ebce7512007ef52faa3d3fa4ca`；无类型内容变化。当前[#435](https://github.com/measimov/investment-tracker/pull/435) head fc4216ac 完整远端CI37135427341 SUCCESS、run_attempt1：3418后端/6跳过、438单元/60文件、158 E2E/4跳过，无失败/重试，实际Python生成→openapi-typescript→Prettier及零漂移门禁通过。原首次实际契约生成与远端证据均独立保留，不以失败调用作为通过。补证日志`/tmp/xueqiu-capabilities-api-types-confirmed.log/.json`。
