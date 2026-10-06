# UI bug bash：布局、阅读与公共配色

2026-10-05。本轮在既有界面上修复用户反馈的显示与操作问题。

## 最终行为

- 交易及公司行动筛选复用同一套响应式布局；宽屏同排对齐，窄屏按可用空间换行。
- 交易表合并标的/市场、价格/币种，备注可展开完整原文；页面统一滚动，上下分页同步，顶部文字翻页入口与手机筛选位于交易明细标题行，单页隐藏顶部入口。翻页成功后回到列表，失败保留旧数据并标记页码不可用。桌面操作栏仅显示“只读”，完整来源保留在title，手机保留原文。日期触发器左对齐并带日历图标。手机保留卡片。
- 持仓标题与分组切换对齐，列宽适应可用空间；观察列表缩减留白。小屏数据表仍允许必要的表内横向滚动，不截断金额。
- 手机标的页返回、名称和操作区对齐；分析保留 AI 生成时间，去掉重复标题。摘要与正文采用同一左对齐阅读栏；时效提示用浮层，商业画像说明与标题对齐。作者动态明确作者、元信息、正文及引用层级。
- 公司行动建议缺失名称时，复用既有名称查找服务补全响应；按代码和市场区分，不改变数据库或财务金额。

## 公共颜色入口

`frontend/src/styles/palette.css` 是颜色值的公共来源：`:root` 为暖纸墨蓝浅色，`html.dark` 为中性石墨。深色使用中性灰背景和表面，链接使用亮蓝；实心主按钮使用独立填充色与白字。盈利、亏损、风险保持独立语义，交易买卖标签使用中性色。

- Element Plus 在 `styles/dark.css` 中映射公共变量，该文件同时覆盖浅色和深色。
- Naive UI 在 `styles/naive.ts` 中读取相同变量；图表通过 `styles/chartTheme.ts` 读取颜色。`styles/tokens.ts` 仅保留字体与分类色索引。
- 页面和品牌图形引用公共变量，不重新定义色值。登录页与导航共用 `assets/brand-mark.png` 的透明遮罩，颜色跟随主题。
- JavaScript 消费的变量保持可解析的 hex/rgb 值；CSS 组件可在适配层使用 `color-mix`。调整颜色应修改公共入口，主题切换通过现有主题状态更新组件和图表。

中性石墨取自 [IBM Design Language 的中性灰与蓝色体系](https://www.ibm.com/design/language/color/) 的设计方向；具体语义映射按本产品内容调整。暖纸浅色与墨蓝细节按用户批准的试配调整。主按钮正常、悬停和按下状态的文字对比度最低为浅色 5.90:1、深色 4.69:1；这是已测配对，不代表全站无障碍认证。

## 截图与验证边界

以下截图全部来自浏览器拦截的明确虚构 fixture。未读取真实账本、写入生产数据、刷新行情或调用模型。研究内容沿用 `research-reading-fixture.json`，不代表真实公司。截图尺寸为桌面 1440×950、手机 393×950；手机研究图为 Linux WebKit，不代替真实 iPhone 验收。

| 页面 | 浅色 | 深色 / 手机 |
| --- | --- | --- |
| 交易记录 | [桌面浅色](bug-bash-transactions-light.png) | [桌面深色](bug-bash-transactions-dark.png) · [手机](bug-bash-transactions-mobile.png) |
| 当前持仓 | — | [桌面深色](bug-bash-holdings-dark.png) |
| 标的分析 | [桌面浅色](bug-bash-analysis-light.png) | [手机深色](bug-bash-analysis-mobile.png) |

独立浏览器检查覆盖 Chromium / Linux WebKit × 393 / 1440 × 浅 / 深色 × 仪表盘、统计、持仓、交易、观察、分析、登录，共 56 个场景。检查页面横向溢出、浏览器错误、字体与正文颜色、两个组件库的主色、日期弹窗和主题持久化；切换主题时图表实例、数据、图例选择与缩放范围保持。

持久回归见 `frontend/e2e/transactions-pilot.spec.ts`、`analysis-status.spec.ts`、`theme.spec.ts`；名称补全回归见 `backend/tests/test_dividend_sync_api.py`。历史媒体保留原交付时点，本轮外观以上表为准。

## PR 前检查

- Ruff 0.16.0 lint / format 通过；Python 3.12 后端全量 3,419 passed、6 skipped（默认关闭外部行情测试）。
- API 类型重生成无漂移，Prettier、类型检查、459 项前端单测和 production build 通过。
- Playwright 全套首轮 177 passed、4 skipped；两处旧用例分别未展开备注、未区分上下分页。保留原功能断言、适配交互后，两份受影响文件 19 项复验全部通过；全部179个非跳过用例均已验证。
- 测试后端与 E2E 使用新建独立数据库；局域网预览继续复用既有5173服务，未修改生产配置。

## 审阅修正 P1（2026-10-05）

四项按顺序实施、独立提交，每项均运行完整 transactions-pilot.spec.ts 并截取1440/393浅深四态；单页/多页和手机筛选展开也已检查。第4项同时通过交易日期筛选回归，共6项。共用ledger-filter-form网格及公司行动筛选代码与原提交保持一致。截图为最终P1状态，后续页头/持仓/分析/手机卡片按独立PR提交。

配色正式采用及P1修正后的完整Playwright在新库investment_ui_review_p1_full_e2e通过：179 passed、4 skipped。TypeScript与459项前端单测通过。原试配patch继续保留，配色与四项P1均为独立提交，可分别审阅和回退。
