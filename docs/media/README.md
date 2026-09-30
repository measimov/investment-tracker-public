# 截图与演示视频

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
| `dashboard.png` `holdings.png` `statistics.png` `transactions.png` `watchlist.png` | 核心看板 |
| `security-analysis.png` `security-graham.png` `security-fundamentals.png` `security-statements.png` `security-announcements.png` | 标的详情页（AI 分析、格雷厄姆准则、基本面、报表、公告） |
| `import-preview.png` `reconciliation.png` | IBKR 活动报表导入预览、月末对账差异 |
| `mobile-*.png` | 手机尺寸（393×852 @2x） |
| `tour.gif` `research.gif` | README 内嵌短片 |
| `demo-output/release/*.mp4` | 完整分辨率视频（含 `walkthrough.mp4` 完整演示），作为 Release 附件 |
