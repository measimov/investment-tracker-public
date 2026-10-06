# 截图与演示视频

最新 UI bug bash 的清白蓝 / 中性石墨配色、交易列表与手机阅读效果见 [本轮验收与截图](ui-bug-bash-check.md)。

持仓页当前试点的同数据对照、状态与验证见 [持仓试点验收](holdings-pilot-check.md)，导航外壳的同数据前后图与检查见 [导航验收](navigation-shell-check.md)，仪表盘新旧展示与状态记录见 [仪表盘验收](dashboard-ui-check.md)，交易日期范围功能见 [日期筛选验收](transaction-date-filter-check.md)，交易主页面视觉与状态见 [交易展示验收](transactions-ui-check.md)，统计的累计/曲线层级、状态与交互见 [统计展示验收](statistics-ui-check.md)，公司行动记录与预计/待收核对见 [公司行动验收](corporate-actions-ui-check.md)，标的档案五个标签页的阅读、状态与键盘检查见 [研究阅读验收](research-reading-ui-check.md) / [AI复盘阅读验收](reports-reading-ui-check.md) / [观察页验收](watchlist-ui-check.md) / [汇率验收](exchange-rates-ui-check.md) / [账户数据验收](account-data-ui-check.md)。

根目录 README 的「界面预览」、以及 GitHub Release 附带的演示视频，都由这里的流程生成，
可以随界面改动重跑。

## 数据从哪来

- **账本与行情是虚构的**：演示用户 `demo`、两个券商账户、两年的买卖/分红/资金流水/对账快照，
  以及全部价格、汇率、指数与 SHIBOR 序列，都由 `backend/scripts/seed_demo.py` 用固定种子生成。
  代码借用常见标的以便读者认得，价格路径与真实走势无关。
- **标的详情页（600900 长江电力）是真实的**：档案、官方公告、年报摘要与 AI 分析描述的是一家
  真实公司，不能编造，所以由 `backend/scripts/demo_research.py` 调用系统自己的任务从公开数据源
  抓取、再由 LLM 生成。需要真实的 `TUSHARE_TOKEN` 与 `LLM_REPORT_API_KEY`，有少量费用
  （4 份年报摘要 + 1 次分析）。

本次研究阅读对照使用另存的 `research-reading-before.png`、`research-reading.png` 和 `mobile-research-reading.png`，标题明确为“虚构阅读样例（非真实公司）”。输入见 [明确虚构的验收 fixture](research-reading-fixture.json)，只由浏览器拦截响应显示，不导入数据库、不调用生成任务，也不替换上面真实 600900 的 `security-*` 图片。新版为 1440×852@1 与 393×852@2；可用 `npm run demo:capture -- -g '明确虚构的研究阅读'` 重采新版两图。已有独立预览时可用 `DEMO_API_URL` 指定配套演示API；预览origin必须已被该API允许，拍摄步骤会拒绝请求失败/连接错误图。

AI复盘对照使用 `reports-reading-before.png`、`reports-reading.png` 和 `mobile-reports-reading.png`，输入见 [明确虚构的报告fixture](reports-reading-fixture.json)，只浏览器GET拦截，不运行生成/追问或定期任务。新版同为1440×852@1与393×852@2，可用 `npm run demo:capture -- -g '明确虚构的AI复盘阅读'` 重采；独立预览配套API可用 `DEMO_API_URL`，实际origin需在API允许范围内。

观察页同实际虚构demo账本对照为 `watchlist-ui-before.png`、`watchlist.png` 与 `mobile-watchlist.png`（1440×852@1、393×852@2）。未手写生产行情/研究数据，可用 `npm run demo:capture -- -g '观察清单演示账本'` 重采新版两图；该步骤检查无加载遮罩、连接错误及失败请求。长内容与未知/零压力样例仅浏览器明确虚构响应，不入库。

汇率同实际demo三API的对照为 `exchange-rates-ui-before.png`、`exchange-rates.png`、`mobile-exchange-rates.png`（1440×852@1、393×852@2）。可用 `npm run demo:capture -- -g '汇率演示账本'` 重采新版两图；四位汇率与历史来自现有虚构演示库，不调用外部刷新或写入。压力样例仅浏览器GET拦截。

账户同实际虚构demo五API对照为 `account-data-ui-before.png`、`account-data.png`、`mobile-account-data.png`（1440×852@1、393×852@2）。可用 `npm run demo:capture -- -g '账户数据演示账本'` 重采新版两图；沿原账本读取，不新增账户、现金、导入或核对记录。失败/批次乱序及长内容样例仅浏览器虚构响应，不写演示库。

雪球入口能力另见 [D3验收](xueqiu-capabilities-check.md)，补充 `xueqiu-capabilities.png`（1440×852@1，无配置/无历史）与 `mobile-xueqiu-capabilities.png`（393×852@2，仅帖子历史）。只浏览器拦截既有明确虚构阅读fixture和合成帖子，截图不是生产数据，也不修改真实来源配置；常规观点拍摄提供与其历史fixture相符的能力响应。

所有写入都只针对库名含 `demo` 的数据库，脚本会先清空它。

## 步骤

```bash
# 1. 建库并迁移（库名须含 demo）
createdb investment_demo
cd backend
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/investment_demo
alembic upgrade head

# 2. 账本与行情（离线、可重复）
python scripts/seed_demo.py

# 3. 标的研究数据（联网、有费用；从带真实 .env 的 backend 目录运行）
python scripts/demo_research.py

# 之后改了账本脚本只重灌账本，保留第 3 步的产物：
python scripts/seed_demo.py --keep-research

# 4. 截图写进 docs/media/*.png，视频写进 frontend/demo-output/videos/
cd ../frontend
npm run demo:capture                 # 或 -- -g 截图 / -- -g 视频 只跑一部分

# 5. 转码：GIF 进 docs/media/，MP4 进 frontend/demo-output/release/（上传到 Release，不进仓库）
npm run demo:media
```

`playwright.demo.config.ts` 自己起一套独立的后端（18100）与前端（4174），关闭全部周期任务、
报价刷新与外部凭证，所以演示库是一份静态快照，录制过程不会触网。

## 产物

| 文件 | 内容 |
|---|---|
| `dashboard.png` `holdings.png` `statistics.png` `transactions.png` `corporate-actions.png` `watchlist.png` | 核心看板 |
| `security-analysis.png` `security-graham.png` `security-fundamentals.png` `security-statements.png` `security-announcements.png` | 标的详情页（AI 分析、格雷厄姆准则、基本面、报表、公告） |
| `import-preview.png` `reconciliation.png` | IBKR 活动报表导入预览、月末对账差异 |
| `mobile-*.png` | 手机尺寸（393×852 @2x） |
| `tour.gif` `research.gif` | README 内嵌短片 |
| `demo-output/release/*.mp4` | 完整分辨率视频（含 `walkthrough.mp4` 完整演示），作为 Release 附件 |

观点与采集器本轮验收见[记录](opinions-ui-check.md)：`opinions-ui-before.png`、`opinions.png`、`mobile-opinions.png`及`mobile-opinions-authors.png`使用同一[明确虚构浏览器fixture](opinions-ui-fixture.json)，不写入演示库或调用真实采集/模型。桌面1440×852@1，手机393×852@2。复现：`npm run demo:capture -- --grep '观点明确虚构阅读样例'`，等待字体/数据/过渡完成，并阻断外部与业务写入。

用户管理阶段：`npm run demo:capture -- --grep '用户管理明确虚构样例'`。`user-management-ui-fixture.json` 是只在浏览器读取响应中使用的明确虚构合法用户（不写数据库）；管理员标志仅UI场景。正式桌面1440×852@1、手机393×852@2，`user-management-ui-before.png`是相同fixture的旧页；全部管理写请求与外呼阻断。详见`user-management-ui-check.md`。

管理员持仓阶段：`npm run demo:capture -- --grep '管理员持仓明确虚构样例'`。`admin-holdings-ui-fixture.json` 为仅浏览器GET响应使用的明确虚构合法用户/持仓/汇率，不写演示库，管理员标志仅UI场景。旧新版同输入1440×852@1、手机393×852@2；阻断业务写与外呼，保留真实零/未知、原币精度和人民币部分覆盖说明。见[管理员持仓验收](admin-holdings-ui-check.md)。

系统告警阶段：`npm run demo:capture -- --grep '系统告警明确虚构样例'`。`system-alerts-ui-fixture.json` 为合法明确虚构告警/通知读取，管理员标志仅UI场景。旧新版同输入1440×852@1、手机393×852@2；全部业务写与外呼阻断，不发送通知，保留完整渠道/周期/告警与送达失败事实。见[系统告警验收](system-alerts-ui-check.md)。

术语与动作名称阶段：[验收记录](ui-language-exit-check.md)。`action-confirm.png`为1440×852@1、`mobile-action-confirm.png`为393×852@2，复用既有明确虚构报告，仅GET与打开原确认窗，不执行删除；沿公开成熟确认组件，等待字体与真实过渡完成后捕获。

显示退出阶段：[验收记录](ui-display-quality-check.md)。`import-quality.png`为1440×852@1、原窗滚动至结果/警告/疑似明细；`mobile-import-quality.png`为393×852@2单列摘要含14项，原header/footer保留。`mobile-market-quality.png`为393×852@2真实统计环图hover；`not-found-quality.png`为1440×852@1未登录长路径。所有正式图等待预览忙态/字体/过渡完成，仅读演示或合成响应，不实际导入/写入/外呼。

## 2026-10-04 可读性与主题截图

本轮重新拍摄核心看板、持仓、交易、公司行动、观察、汇率、账户，以及明确虚构的研究、观点与管理样例。账本来自独立 `investment_ui_refinement_demo` 固定种子演示库；研究与观点沿用本目录注明“明确虚构”的浏览器 fixture，不生成真实公司研究、不运行采集、行情刷新或付费任务。

新增 `dashboard-dark.png`、`statistics-dark.png`、`holdings-dark.png`、`opinions-dark.png`、`research-reading-dark.png`，均为1440×900的深色示例。采集器展开另见 `collector-details-dark.png` 与 `mobile-collector-details-dark.png`（1440/393×900），只用明确虚构fixture。原 `security-*` 真实600900研究图片保留为历史样例，不能据此承诺新版主题外观；本轮研究主题示例请看 `research-reading.png` 和 `research-reading-dark.png`。

正式检查范围与限制见 [本轮视觉验收](ui-refinement-theme-check.md)。

## 长文字体（2026-10-04）

[长文与字体验收](editorial-typography-check.md)记录自托管字体、连续正文与数据的分工、资源量测及加载边界。四张 `editorial-*.png` 是此轮实际页面的明确虚构阅读样例；沿 `research-reading-fixture.json`、`reports-reading-fixture.json`、`opinions-ui-fixture.json` 并追加明确虚构的中英混排段落，仅拦截浏览器响应，没有导入账户或调用模型。原截图保留各自交付时点。
