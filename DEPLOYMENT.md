# 部署指南

本文档覆盖 Docker Compose 部署的前置要求、环境变量、运行时架构、首次部署、升级、
部署后数据任务、长任务运维、备份恢复与常见问题。

- 运行数据库统一为**外部 PostgreSQL 16**（不在 compose 里）。`data/` 目录只保留原始导入文件，
  不是数据库，也不是备份目标。
- 文中 `<app-host>`、`<db-host>` 等尖括号内容都是占位符，换成自己的值。
- 文中命令一律写 `docker compose`（Compose v2 插件）。只装了独立二进制 `docker-compose`（v2）
  的主机把它替换成 `docker-compose` 即可，参数完全相同。
- 所有命令都在仓库根目录执行（Compose 要在这里找到 `docker-compose.yml` 与 `.env`）。

目录：

1. [前置要求](#1-前置要求)
2. [环境变量](#2-环境变量)
3. [运行时架构](#3-运行时架构)
4. [首次部署](#4-首次部署)（含 [群晖 NAS](#群晖-nas)）
5. [升级清单](#5-升级清单)
6. [部署后数据任务矩阵](#6-部署后数据任务矩阵)
7. [长任务运维](#7-长任务运维)
8. [雪球运维](#8-雪球运维)
9. [备份与恢复](#9-备份与恢复)
10. [常见问题](#10-常见问题)

---

## 1. 前置要求

| 项目 | 要求 |
| --- | --- |
| Docker | Docker Engine |
| Compose | `docker compose`（v2 插件）或独立二进制 `docker-compose` v2，二选一 |
| 数据库 | 外部 PostgreSQL 16，部署主机与容器网络都能访问；账号需有建表权限（迁移由 Alembic 执行） |
| 端口 | 80/443 空闲，或在 `.env` 里改 `FRONTEND_HTTP_PORT` / `FRONTEND_HTTPS_PORT` |
| 证书 | TLS 证书与私钥文件（LAN 自签也可，见 [首次部署](#4-首次部署)） |
| 备份客户端 | 宿主有 `pg_dump`/`pg_restore`（主版本 ≥ 16），或能 `docker run postgres:16`（`backup.sh` 自动回退） |

### 雪球客户端库（可选）

私有部署使用的雪球行情/基本面客户端库 `xueqiu-market` 不在公开仓库中，`backend/requirements.txt`
也不包含它。应用对缺库是**显式降级**的：所有依赖该库的雪球入口（美股行情的雪球兜底、A股
`xueqiu_*` 档案数据集、港/美股代码到雪球代码的映射）都会报「雪球库 xueqiu-market 未安装」
并跳过，其他数据源与功能不受影响。内置雪球发言采集器（`xueqiu-collector` 服务）不依赖该库。

---

## 2. 环境变量

```bash
cp .env.example .env    # 然后按分组填写；.env 已 gitignore
```

### 读取规则

- `.env` **只由 docker compose 读取**，用来插值 `docker-compose.yml`。镜像里不带 `.env`
  （`backend/.dockerignore` 排除了它），后端容器**只看得到** `docker-compose.yml` 里 backend
  服务 `environment:` 列表下传的变量。一个 `config.py` 字段没写进这张列表，在 `.env` 里怎么改
  都不生效，而且不报错。
- 列表里有两种写法：
  - `- NAME=${NAME:?...}`：必填，缺了 compose 直接报错；
  - `- NAME`（裸键）：`.env` 里设了才下传，没设就不进容器，由 `config.py` 的默认值生效——
    默认值只有 `config.py` 一个家。`- NAME=${NAME:-x}` 是历史写法，`x` 与 `config.py` 默认值保持一致。
- `.env.example` 里带默认值的旋钮都是注释掉的：需要覆盖时取消注释再改值，不要照抄默认值
  （照抄会把今天的默认值钉死，以后代码改默认值时部署拿不到）。
- **改了 `.env` 要重建容器才生效**：`docker compose up -d`（只重建配置有变化的服务；backend 与
  xueqiu-collector 各读各的 `environment:`，改了两边都下传的变量两个都会重建）。`docker compose restart`
  只重启进程、不重读环境变量。核对容器实际拿到的值：`docker compose exec <服务> printenv NAME`。
- **新增配置必须三处同步**：`backend/app/config.py` ⇄ `docker-compose.yml` backend
  `environment:` ⇄ `.env.example`。`backend/tests/test_deploy_config_sync.py` 在 CI 上守着：
  每个 Settings 字段都要下传（或进白名单并写明理由）、compose 里不能有 config.py 不认识的键、
  `.env.example` 的每个变量都要有去处、每个 Settings 字段都要在 `.env.example` 里有说明。

下表「默认」列为 `config.py`（或 compose 插值）的默认值。

### 必填

| 变量 | 说明 |
| --- | --- |
| `DATABASE_URL` | PostgreSQL 连接串，`postgresql://<db-user>:<db-password>@<db-host>:5432/<db-name>` |
| `CORS_ORIGINS` | 允许访问 API 的前端 origin，逗号分隔；非 443 端口要带端口号 |
| `SECRET_KEY` | JWT 签名密钥，`openssl rand -hex 32` |
| `ADMIN_INITIAL_PASSWORD` / `DEMO_INITIAL_PASSWORD` | `manage.py seed` 创建初始用户时的口令（至少 12 位，大小写+数字+符号） |
| `NGINX_SERVER_NAME` | 对外主机名（nginx `server_name`） |
| `SSL_CERT_FULLCHAIN` / `SSL_CERT_PRIVKEY` | 证书与私钥的宿主路径，只读挂载进前端容器 |

### 端口与目录（只给 compose 用）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FRONTEND_HTTP_PORT` | `80` | HTTP 端口（只做跳转到 HTTPS） |
| `FRONTEND_HTTPS_PORT` | `443` | HTTPS 端口；同时以 `NGINX_HTTPS_PORT` 传给 nginx 模板作为跳转目标端口，不要另设 |
| `BACKEND_LOG_DIR` | `./backend/logs` | 后端日志的宿主目录，挂到容器 `/app/logs`；须可被 uid 10001 写入 |
| `NGINX_LOG_DIR` | `./logs/nginx` | nginx 访问/错误日志的宿主目录 |
| `XUEQIU_COOKIE_HOST_DIR` | `./backend/secrets` | 雪球 Cookie 文件所在宿主**目录**，只读挂到容器 `/app/secrets` |

### 安全与会话

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ENABLE_DOCS` | `false` | 是否开放 `/docs`；生产保持 false |
| `REQUIRE_HTTPS` | `true` | 拒绝明文登录；生产保持 true |
| `TRUST_PROXY_HEADERS` | config `false` / compose `true` | 是否采信 `X-Forwarded-Proto` / `X-Forwarded-For`，见下 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | 访问令牌有效期（分钟，滑动续期） |
| `SESSION_ABSOLUTE_MAX_HOURS` | `168` | 会话自首次登录起的绝对上限（小时） |

`ENABLE_DOCS` / `REQUIRE_HTTPS` 的代码默认值就是生产值（fail-closed），漏配不会退回不安全的一侧。

`TRUST_PROXY_HEADERS` 决定后端是否采信反代请求头：`X-Forwarded-Proto` 判断请求是不是 HTTPS，
`X-Forwarded-For` 取审计日志用的客户端 IP。这两个头客户端都可任意伪造，只有在**后端端口不直接
对外**、且反代确实会覆写/追加它们时才可信——本仓的 nginx 用 `$scheme` 覆写、compose 也不发布
8000，故默认拓扑下置 `true`。若改成直接暴露后端端口，必须同时改回 `false`，否则一个请求头就能
绕过 `REQUIRE_HTTPS`。

这个开关成立的前提是 **uvicorn 自己不处理这些头**：它的 `ProxyHeadersMiddleware` 默认开启且信任
`127.0.0.1`，会抢先把 `scope.scheme` 改写成 `https`，那样应用层的开关就成了摆设。因此所有启动
命令（Dockerfile、开发命令、E2E）都带 `--no-proxy-headers`，由应用层独占解析；
`backend/tests/test_proxy_header_trust.py` 起真实 uvicorn 守着这一点。

`SESSION_ABSOLUTE_MAX_HOURS` 是会话自**首次登录**起的绝对上限。滑动续期共享同一个 jti，只看
有效期的话被窃 cookie 按时刷新即可永久续命；到顶即吊销、强制重新登录。

### 版本标识与时区

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `BUILD_SHA` | `unknown` | `/health` 的 `build` 字段；每次升级改成当前提交（见 [升级清单](#5-升级清单)） |
| `APP_VERSION` | `1.0.0` | `/health` 的 `api_version` 字段 |
| `DISPLAY_TIMEZONE` | `Asia/Shanghai` | 业务时区：UTC 时间戳落成「哪一天」、「今天」是哪天都按它算。容器系统时区是 UTC，不能依赖 |

### 数据源

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TUSHARE_TOKEN` | 空 | Tushare token：行情刷新、A股档案、分红同步、标的全集都依赖它；留空则这些功能不可用 |
| `TUSHARE_GLOBAL_MIN_INTERVAL_SECONDS` | `0.35` | 所有 Tushare 调用共享的最小间隔（≈170 次/分）；0 = 关闭全局闸 |
| `TUSHARE_COOLDOWN_BASE_SECONDS` | `65` | 接口级频率错误的首次冷却秒数（只有真撞限才生效） |
| `TUSHARE_COOLDOWN_MAX_SECONDS` | `900` | 冷却指数退避上限 |
| `TUSHARE_HK_MIN_INTERVAL_SECONDS` | `31` | 港股行情接口 `hk_daily`/`hk_mins` 的单接口最小间隔 |
| `TUSHARE_API_BASE_URL` | `https://api.waditu.com/dataapi` | Tushare HTTPS 数据接口地址；留空 = 默认 |
| `EDGAR_USER_AGENT` | 空 | SEC EDGAR（美股档案、10-K/20-F）要求 UA 带联系方式，形如 `your-app your-email@example.com`；留空用占位 UA 并告警，SEC 可能 403 |
| `XUEQIU_COOKIE_FILE` | 空 | 雪球 Cookie 文件的**容器内**路径，如 `/app/secrets/xueqiu.com.json` |
| `XUEQIU_COOKIES` | 空 | 直接给 Cookie JSON；与 `XUEQIU_COOKIE_FILE` 二选一，优先；两者都空 = 关闭雪球数据源（显式降级） |
| `XUEQIU_MIN_DELAY_SECONDS` / `XUEQIU_MAX_DELAY_SECONDS` | `2` / `4` | 雪球请求间的随机间隔；调低会更快撞 WAF |
| `XUEQIU_TIMEOUT_SECONDS` | `20` | 雪球请求超时 |
| `XUEQIU_COOKIE_WARN_DAYS` / `XUEQIU_COOKIE_CRITICAL_DAYS` | `7` / `3` | Cookie 到期告警阈值（`scripts/check_xueqiu_cookie_expiry.py`） |
| `XUEQIU_OPINION_RECENT_DAYS` | `30` | 观点摘要的近期窗口（转向判断分界） |
| `XUEQIU_OPINION_LOOKBACK_DAYS` | `180` | 喂给 LLM 的观点回看深度 |
| `XUEQIU_OPINION_STALE_HOURS` | `48` | 最近一次状态为 `ok`/`partial` 的作者采集记录早于该时长即在观点页与仪表盘告警（见 [8.2](#82-采集器状态与监控)） |

### LLM 与标的分析

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_REPORT_API_KEY` | 空 | DeepSeek / OpenAI 兼容接口的 key；留空 = 依赖 LLM 的功能（AI 复盘、标的分析、财报摘要、港股报表抽取、观点摘要）不可用，AI 复盘接口返回 409、定期计划静默跳过 |
| `LLM_REPORT_BASE_URL` | `https://api.deepseek.com` | 接口地址 |
| `LLM_REPORT_MODEL` | `deepseek-flash` | 模型名 |
| `LLM_REPORT_TIMEOUT_SECONDS` | `120` | 单次调用超时 |
| `LLM_REPORT_MAX_OUTPUT_TOKENS` | `16384` | 输出额度（推理 token 与输出共享）；长报告被截断或为空时调大 |
| `STATEMENT_MAX_OUTPUT_TOKENS` | `32768` | 港股报表科目映射单独的输出额度；`finish_reason=length`、内容为空时调大 |
| `SECURITY_ANALYSIS_MAX_OUTPUT_TOKENS` | `32768` | 标的分析（单只/批量）单独的输出额度；任务报「LLM 输出被截断（finish_reason=length）」时调大 |
| `REPORT_TARGET_PLAN_TTL_HOURS` | `24` | 年报清单缓存 TTL |
| `SECURITY_ANALYSIS_FRESHNESS_HOURS` | `24` | 批量分析跳过该时长内已分析过的标的 |
| `SECURITY_ANALYSIS_BATCH_PAUSE_SECONDS` | `5` | 批量分析标的之间的停顿 |
| `SECURITY_ANALYSIS_BATCH_MAX_SECONDS` | `14400` | 批量任务墙钟上限（心跳护栏） |

### 后台任务与周期任务

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `BACKGROUND_WORKER_ENABLED` | `true` | 进程内 worker 与周期任务总开关；关闭后只剩 API 内联快路径，周期任务全部停止 |
| `BACKGROUND_JOB_RETENTION_HOURS` | `168` | 已结束任务的保留时长 |
| `BACKGROUND_JOB_STALE_MINUTES` | `60` | 启动时把超过该时长仍 running 的任务判为中断 |
| `BACKGROUND_JOB_POLL_SECONDS` | `5` | worker 轮询间隔 |
| `BACKGROUND_JOB_LEASE_SECONDS` | `300` | 任务租约；长任务靠回写进度续租，过期会被接管重跑 |
| `BACKGROUND_JOB_MAX_ATTEMPTS` | `3` | 意外失败的最大尝试次数 |
| `BACKGROUND_JOB_RETRY_BASE_SECONDS` | `30` | 重试指数退避基数 |
| `PRICE_REFRESH_MAX_WORKERS` | `4` | 行情刷新并发线程数 |
| `PRICE_REFRESH_FRESHNESS_SECONDS` | `600` | 主动刷新的新鲜度窗口（窗口内重复点击跳过） |
| `HKEX_DAYQUOT_SYNC_ENABLED` | `true` | 港交所《每日行情报表》周期同步 |
| `HKEX_DAYQUOT_LOOKBACK_DAYS` | `10` | 回看天数（站点存档约一个月） |
| `HKEX_DAYQUOT_MAX_REPORTS_PER_TICK` | `5` | 每次最多下载份数（约 25MB/份） |
| `SECURITY_CATALOG_SYNC_ENABLED` | `true` | 标的全集周期同步 |
| `SECURITY_CATALOG_SYNC_INTERVAL_HOURS` | `168` | 标的全集新鲜度（按上次成功时间判断，重启不重拉） |
| `DIVIDEND_SYNC_LOOKBACK_DAYS` | `365` | 分红公告同步回看天数 |
| `DIVIDEND_SYNC_MATCH_WINDOW_DAYS` | `30` | 分红建议与已入账股息的判重窗口 |
| `DIVIDEND_SYNC_PERIODIC_ENABLED` | `false` | 分红公告每周自动同步；开启前确认 Tushare 积分配额充足 |

### 雪球采集器

采集器是独立的 compose 服务 `xueqiu-collector`（见 [运行时架构](#3-运行时架构)），与 backend
同一个镜像。它读的是**自己那份** `environment:`：`DATABASE_URL`、Settings 必填项、
`DISPLAY_TIMEZONE`、雪球 Cookie 三项（`XUEQIU_COOKIE_FILE` / `XUEQIU_COOKIES` / 到期阈值）与下表
全部变量；与 backend 共用**同一份 Cookie**，没有第二份配置。backend 只下传其中 Web 侧要读的几个
（启用开关、轮次间隔、WAF 冷却、心跳阈值、按标的开关与时刻——用于观点页状态卡与拦截「立即运行」），
其余只下传给采集器。`test_deploy_config_sync.py` 同时守着两个服务：采集器代码里读到的每个
`settings.<字段>` 都必须出现在 xueqiu-collector 的 `environment:`，两个服务都不许用 `env_file:`。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `XUEQIU_COLLECTOR_ENABLED` | `false` | 总开关。false 时采集器进程只空转写心跳（healthcheck 仍健康），观点页显示「未启用」，「立即运行」返回 409 |
| `XUEQIU_COLLECTOR_PUSH_URL` | 空 | Uptime Kuma push 地址（可选）；每轮作者采集结束推送一次，本轮全部作者 `ok`/`partial` 才推 up |
| `XUEQIU_COLLECTOR_CYCLE_MINUTES` | `60` | 作者采集轮次间隔（分钟），以上一轮开始时间计 |
| `XUEQIU_COLLECTOR_MIN_DELAY_SECONDS` / `XUEQIU_COLLECTOR_MAX_DELAY_SECONDS` | `10` / `25` | 进程级礼貌限速：相邻两次请求之间的随机停顿；勿调低 |
| `XUEQIU_COLLECTOR_TIMEOUT_SECONDS` | `20` | 单次请求超时 |
| `XUEQIU_COLLECTOR_MAX_AUTHORS_PER_RUN` | `5` | 每轮最多采集几位作者（最久没跑的优先） |
| `XUEQIU_COLLECTOR_AUTHOR_GAP_MIN_SECONDS` / `..._MAX_SECONDS` | `180` / `600` | 作者之间的随机间隔（秒） |
| `XUEQIU_COLLECTOR_MONITOR_DAYS` | `30` | 回看窗口：主页发言与评论区命中回复都只收这段时间内的 |
| `XUEQIU_COLLECTOR_PROFILE_PAGES` | `0` | 主页时间线页数上限；0 = 翻到早于回看窗口为止 |
| `XUEQIU_COLLECTOR_MAX_COMMENT_PAGES` | `6` | 每帖最多翻几页评论（每页 20 条） |
| `XUEQIU_COLLECTOR_STALE_POST_DAYS` / `XUEQIU_COLLECTOR_STALE_COMMENT_PAGES` | `30` / `1` | 超过该天数的旧帖只翻这么多页评论 |
| `XUEQIU_COLLECTOR_RESCAN_COOLDOWN_HOURS` | `12` | 同一帖在该时长内扫过评论即跳过 |
| `XUEQIU_COLLECTOR_MAX_WAF_HITS` | `1` | 命中阿里云 WAF 挑战页几次即中止本轮 |
| `XUEQIU_COLLECTOR_WAF_COOLDOWN_SECONDS` | `1800` | 命中 WAF 后的冷却期，期内不开新一轮（「立即运行」也等冷却结束） |
| `XUEQIU_COLLECTOR_HEARTBEAT_FILE` | `logs/xueqiu-collector.heartbeat` | 心跳文件（相对 `/app`，即挂载的日志目录）；healthcheck 读它的修改时间 |
| `XUEQIU_COLLECTOR_HEALTH_MAX_AGE_MINUTES` | `30` | 心跳超过该时长即判不健康 |
| `XUEQIU_COLLECTOR_SYMBOLS_ENABLED` | `true` | 每日按标的采集（公告/讨论、组合调仓、热帖）开关；只在总开关打开时生效 |
| `XUEQIU_COLLECTOR_SYMBOLS_RUN_AFTER` | `07:30` | 业务时区每天该时刻之后跑一轮（`HH:MM`；格式错退回 07:30 并告警） |
| `XUEQIU_COLLECTOR_SYMBOL_COUNT` | `20` | 每个标的每类取最新几条 |
| `XUEQIU_COLLECTOR_HOTS_SCOPE` | `day` | 热帖口径：`day` / `week` |
| `XUEQIU_COLLECTOR_SYMBOLS_RETRY_MINUTES` | `60` | 按标的轮次有失败项时，距上一轮多少分钟后只重试失败项 |
| `XUEQIU_COLLECTOR_SYMBOLS_MAX_ATTEMPTS` | `3` | 按标的轮次当日最多几轮（含首轮）；用尽即记当天已跑，剩余失败项明日随整轮再采 |

### 只给宿主脚本用的变量

这些变量 compose 不读；`backup.sh` 也**不读 `.env`**，运行时在 shell 里设置。

| 变量 | 用途 |
| --- | --- |
| `BACKUP_DIR` | 备份目录，默认 `./backups` |
| `BACKUP_MODE` | `postgres` / `excel` / `full`；不设则交互选择 |
| `BACKUP_TABLES` | 空格分隔的表名 → 表级备份 `investment_tables_<时间>.dump`；不设 = 整库 |
| `BACKUP_PG_TOOL` | `local` / `docker`；不设则本机有 `pg_dump` 用本机，否则用一次性容器；本机主版本低于数据库时自动改用容器 |
| `BACKUP_PG_IMAGE` | 容器模式的镜像，默认 `postgres:16`（主版本须 ≥ 数据库主版本） |
| `BACKUP_DOCKER_NETWORK` | 容器模式的网络，默认 `host`；数据库在某个 compose 网络里时改成该网络名 |
| `APP_BASE_URL` / `APP_CA_CERT` / `INVESTMENT_TRACKER_TOKEN` | Excel 导出的访问地址、私有 CA、Bearer token |

---

## 3. 运行时架构

```
浏览器 ──HTTPS──> frontend 容器（nginx）
                   ├─ 静态文件（Vue 构建产物）
                   ├─ /api/*、/health ──> backend:8000（compose 内网，不对外发布）
                   └─ 80 端口只做 301 跳转到 HTTPS
                  backend 容器（uvicorn 单进程，uid 10001）
                   ├─ FastAPI 请求处理（部分任务在请求内联快路径执行）
                   ├─ 进程内 job worker：慢车道 + 快车道
                   ├─ 租约回收线程（每 60s）
                   └─ 周期任务线程（每 60s 检查一次到期任务）
                        │
                        └──> 外部 PostgreSQL 16 <──┐
                  xueqiu-collector 容器（同一镜像，manage.py xueqiu-collector，uid 10001）
                   └─ 常驻循环（每 30s 写心跳、检查是否该开新一轮）
                        ├─ 作者发言轮次：每小时（或管理员「立即运行」）
                        └─ 按标的轮次：每天业务时区 07:30 之后一次（或「立即运行」）
                        （共用一把 PostgreSQL advisory lock、一个限速时钟、一个 WAF 冷却）
```

**nginx（frontend 容器）**：TLS 终结、HTTP→HTTPS 跳转、安全响应头、静态资源缓存；登录接口
限流 5 次/分（突发 3），其余 API 300 次/分（突发 40）；上传上限 21MB（后端判定 20MB，留出
multipart 开销）；普通 API 代理超时 300s。

**backend**：单个 uvicorn 进程（不开 `--workers`），在进程内同时承担 API、后台任务执行与周期任务：

- 任务状态持久化在 `background_jobs` 表，由数据库原子领取（`FOR UPDATE SKIP LOCKED`）。API
  先入队、可能内联执行；worker 兜底执行排队中的任务、接管租约过期的任务、按指数退避重试失败任务。
- **慢车道**：分析家族——`security_analysis`、`security_analysis_batch`、`report_digest_backfill`、
  `report_digest_batch`、`opinion_summary`、`opinion_summary_batch`（它们入队时本就互斥，一条车道
  串行执行）。**快车道**：其余全部——`price_refresh`、`performance_history_sync`、`dividend_sync`、
  `llm_report`。数小时的批量分析只占住慢车道，行情刷新与周期任务照常。
- **周期任务**（进程启动后第一次检查时立即各跑一次，之后按间隔）：

  | 任务 | 间隔 | 开关 / 前提 |
  | --- | --- | --- |
  | 汇率每日快照 | 6 小时检查（当天已有则零外呼） | — |
  | 基准指数尾部补齐 | 24 小时 | 需 `TUSHARE_TOKEN` |
  | 港交所每日行情报表（港股官方收盘价） | 6 小时 | `HKEX_DAYQUOT_SYNC_ENABLED` |
  | 标的全集 | 6 小时检查，按 `SECURITY_CATALOG_SYNC_INTERVAL_HOURS` 判新鲜 | `SECURITY_CATALOG_SYNC_ENABLED` |
  | AI 复盘定期计划调度 | 1 小时 | 需 `LLM_REPORT_API_KEY` |
  | 分红公告同步 | 7 天 | `DIVIDEND_SYNC_PERIODIC_ENABLED`（默认关） |

  以上全部依赖 `BACKGROUND_WORKER_ENABLED=true`。周期任务在一条线程上串行执行，一个任务慢
  （如标的全集首次同步约 1 分钟）只推迟其余任务，不会并发。
- **不要跑多个 backend 副本**（`docker compose up --scale backend=N`、多台机器连同一个库都算）：
  周期任务没有跨进程互斥，多副本会重复外呼（Tushare 配额、港交所 25MB 报表、LLM token）。
- 启动时：把超过 `BACKGROUND_JOB_STALE_MINUTES` 仍 running 的任务标记为中断、清理过期任务，然后
  启动 worker；关闭时不等待在飞的长任务，靠租约过期 + 重启后接管续跑。

**xueqiu-collector（雪球采集器）**：与 backend 同一镜像的第二个服务，`command` 是
`python manage.py xueqiu-collector`（常驻循环）。它与 Web 进程完全分离——不占 worker 车道，
两边各自重启互不影响；**抓取永远不在 Web 进程里跑**，页面上的「立即运行」只在数据库里写一个请求
时间，由采集器在下一次 30 秒轮询时拾取。

- **总开关** `XUEQIU_COLLECTOR_ENABLED` 默认 `false`：此时进程只空转、每 30 秒写一次心跳，
  healthcheck 仍健康，观点页显示「未启用」。
- **作者发言轮次**（每 `XUEQIU_COLLECTOR_CYCLE_MINUTES`=60 分钟，按上一轮开始时间计）：从「关注
  作者」名单里取最久没跑的最多 5 位，依次抓主页时间线 → 候选帖全文 → 评论页，写入
  `xueqiu_archiver_posts / replies / utterances / post_scan_state`；作者之间随机间隔 180–600 秒，
  每位作者每轮在 `xueqiu_archiver_scan_runs` 写一行（状态、错误、是否命中 WAF、发言数）。
  一轮可能持续数十分钟，这是限速的结果，不是卡住。每位作者的状态：
  - `ok`：主页时间线与后续页、全文、评论全部拿到合法响应（主页合法地为空也算 ok）；
  - `partial`：主页首页成功，但之后某页时间线、某帖全文或评论页失败——已取到的数据照常写入，
    失败的帖子下一轮重抓（评论清掉扫描时间、全文不标已补全），`error_message` 写明失败处；
  - `error`：主页首页就拿不到合法响应（非 JSON、登录页、`{error_code}`、HTTP/网络错误），本轮对该作者
    一无所知；`failed`：其他异常；`waf`：命中 WAF；`interrupted`：进程在轮次中被停掉。

  只有 `ok` 与 `partial` 说明「数据在流动」，计入活性（见 [8.2](#82-采集器状态与监控)）。
- **按标的轮次**（每天业务时区 `XUEQIU_COLLECTOR_SYMBOLS_RUN_AFTER`=07:30 之后）：范围 = 全体用户
  持仓 ∪ 自选中 A/B/港/美股、扣除排除与现金管理规则；每个标的取最新一页公告与讨论，外加管理员维护
  的组合调仓名单与今日热帖，幂等写入 `xueqiu_symbol_posts / xueqiu_cube_rebalancing /
  xueqiu_hot_posts`。每一项的响应都按端点校验结构——错误对象（`error_code`、`success=false`）、
  未知结构、非 JSON、HTTP/网络错误都记为该项失败，不会被当成「没有新帖」。**业务日语义**：一轮
  **没有任何失败**才把当天记为已跑（记在数据库里，重启不重跑）；有失败（含 WAF、Cookie 不可用）时只把
  没成功的项写进状态表的 `symbols_pending`，距上一轮 `XUEQIU_COLLECTOR_SYMBOLS_RETRY_MINUTES`（60）
  分钟后**只重试这些项**，当日最多 `XUEQIU_COLLECTOR_SYMBOLS_MAX_ATTEMPTS`（3）轮（含首轮），用尽后
  记当天已跑、剩余项明日随整轮再采；进程被停掉中断的一轮不计次数。它排在作者轮次之后串行执行，
  不写 `scan_runs`，状态在采集器卡片的「按标的」一行（有待重试时显示「今日待重试」与项数）。
- **单实例**：两类轮次共用一把 PostgreSQL advisory lock，第二个实例（或手工 `--once`）拿不到锁会
  直接跳过本轮，所以多开副本没有收益，只会翻倍请求，**只跑一个**。进程崩溃时锁随连接释放，没有
  残留锁文件；上一个进程留下的未结束 `scan_runs` 在下一轮开始时标为 interrupted。
- **WAF**：遇到阿里云 WAF 挑战页立即中止本轮，`XUEQIU_COLLECTOR_WAF_COOLDOWN_SECONDS`（默认 30 分钟）
  内不开新一轮，按标的轮次同样遵守。
- **心跳与 healthcheck**：循环与每次请求都会刷新 `/app/logs/xueqiu-collector.heartbeat`（宿主
  `BACKEND_LOG_DIR` 下）并至多每分钟写一次数据库状态行；compose healthcheck 每 5 分钟跑
  `python manage.py xueqiu-collector-health`，心跳超过 `XUEQIU_COLLECTOR_HEALTH_MAX_AGE_MINUTES`
  （默认 30 分钟）即不健康。镜像自带的 HTTP 探活被这条覆盖（采集器不起 uvicorn）。
- **日志**：写同一个挂载目录下的 `xueqiu-collector.log`（与 `app.log` 分开）。
- **数据表**：五张 `xueqiu_archiver_*` 表由迁移 `…_0024` 以 `CREATE TABLE IF NOT EXISTS` 纳入
  Alembic（沿用旧版独立采集程序的表结构，已有数据零改动），另有作者名单、采集器状态表；`…_0025`
  新建按标的采集的四张表。

<a id="写库进程"></a>**写库进程**：会写本应用数据库的有 **backend** 与 **xueqiu-collector** 两个服务；
切换到内置采集器之前（或切换过渡期）还有宿主 crontab 里**旧 xueqiu-timeline-archiver 的两行定时任务**
（它写 `xueqiu_archiver_*` 表）。下文凡是「停写库进程」——迁移、恢复到原库、切换 `DATABASE_URL`——
都指这三者全部停下：`docker compose stop backend xueqiu-collector`，旧 archiver 仍在用的部署再把
它的 crontab 两行注释掉并确认没有残留进程。只停 backend **不会**停采集器，它是独立的常驻进程，会继续
持事务、提交数据：阻塞迁移或 `pg_restore --clean` 的 DROP，或在恢复完成后把进行中那一轮的结果写回
刚恢复的库。`pg_restore --single-transaction` 只保证恢复自身的原子性，挡不住其他写入者。

**`/health`**：后端对数据库执行一次 `SELECT 1`。

```json
{"status": "healthy", "database": "reachable", "api_version": "1.0.0", "build": "<git 短哈希>"}
```

数据库不可达时返回 503，`detail` 为 `{"status": "unhealthy", "database": "unreachable", "error": "<异常类名>"}`。
两个容器都有 Docker `HEALTHCHECK`：backend 直接探 `http://127.0.0.1:8000/health`，frontend 经 nginx
探 `https://127.0.0.1:443/health`（同时覆盖 nginx 与到后端的链路）。`docker compose ps` 的
`(healthy)` 即来自这里。

**日志**：后端写 `/app/logs`（宿主 `BACKEND_LOG_DIR`）下的 `app.log` 与 `auth.log`（认证事件单独
一份），按 10MB × 10 份轮转；nginx 写 `NGINX_LOG_DIR`，登录接口另有 `login_access.log` /
`login_error.log`。

---

## 4. 首次部署

### 4.1 准备 `.env` 与证书

```bash
git clone https://github.com/measimov/investment-tracker-public.git
cd investment-tracker-public
cp .env.example .env
# 编辑 .env：至少填完「1. 必填」一组；按需填 EDGAR_USER_AGENT、雪球 Cookie、LLM key
```

LAN 自签证书：

```bash
mkdir -p certs/lan
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout certs/lan/privkey.pem \
  -out certs/lan/fullchain.pem \
  -days 825 \
  -subj /CN=<app-host> \
  -addext subjectAltName=DNS:<app-host>,DNS:localhost,IP:127.0.0.1
```

### 4.2 构建

```bash
# 让 /health 的 build 字段可溯源（GNU 与 BSD/macOS sed 通用的写法：-i.bak 再删备份）
SHA=$(git rev-parse --short HEAD)
if grep -q '^BUILD_SHA=' .env; then
  sed -i.bak "s/^BUILD_SHA=.*/BUILD_SHA=$SHA/" .env && rm -f .env.bak
else
  printf '\nBUILD_SHA=%s\n' "$SHA" >> .env
fi
grep '^BUILD_SHA=' .env

docker compose build
```

backend 与 xueqiu-collector 是同一个 Dockerfile 的两个服务，这一条会把两个镜像都构建出来（第二个
几乎全部命中缓存）。

### 4.3 日志目录权限（后端容器非 root）

后端容器以固定 uid **10001** 运行（#143），挂载的日志目录必须可被其写入，否则后端启动时日志
初始化会因权限被拒直接失败——这是 fail-fast，不是静默丢日志。

授权命令**让 Compose 自己解析路径**，一次性起个 root 容器 chown 挂载点：

```bash
docker compose run --rm --user root backend chown -R 10001:10001 /app/logs
```

首次部署与升级都是这一条，也不必先建目录——bind mount 会自动创建宿主目录（root 所有），这条
命令紧接着把它改对。

**不要在宿主上拼 `${BACKEND_LOG_DIR}` 路径**，两个坑：

1. 普通 shell 不读 Compose 的 `.env`（Compose 只在自己执行时读），配了自定义路径的部署会授权到
   默认目录，而 Compose 挂的是另一个；
2. 更不要 `. ./.env`——Compose 的 dotenv 有自己的语法（`VAR: VAL`、`VAR = VAL`、自己的引号/转义/
   插值规则），不保证能被 POSIX shell 解析。口令含特殊字符时，`&`、`$()`、空格括号会被 shell
   分隔、展开甚至执行；而 `set -a` 导出的错误值随后**以更高优先级覆盖** Compose 自己对 `.env`
   的正确解析，等于主动把部署改坏。真要在宿主取值只能走 `docker compose config --environment`。

### 4.4 迁移、初始化用户、启动

```bash
docker compose run --rm backend alembic upgrade head
docker compose run --rm backend python manage.py seed
docker compose up -d
docker compose ps
```

应用启动**不会**自动建表或写种子数据：表结构只由 Alembic 管理，`seed` 创建 `admin` / `demo`
两个用户，口令来自 `.env`。v1.0 基线（`20260728_0001`）之前的迁移已压缩，不在基线上的数据库应
重建而非迁移。

### 4.5 验证

```bash
# 容器内直接探后端（不依赖证书与宿主端口）
docker compose exec -T backend python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).read().decode())"

# 经 nginx（自签证书用 --cacert 指定）
curl --cacert certs/lan/fullchain.pem https://<app-host>/health
```

`build` 应等于刚写入的 `BUILD_SHA`。访问 `https://<app-host>` 登录。`docker compose ps` 里
三个容器都应是 `healthy`（xueqiu-collector 的 healthcheck 首次在启动 2 分钟后判定；未启用时它
只空转写心跳，同样健康）。

启动后约一分钟内，周期任务会各跑一次：标的全集首次同步（约 1 分钟）、港交所日报、汇率快照。
`docker compose logs -f backend` 可以看到。

### 4.6 可选：数据源与 AI

- **美股档案**：填 `EDGAR_USER_AGENT`。
- **雪球**：把浏览器插件导出的 Cookie JSON 放进 `XUEQIU_COOKIE_HOST_DIR`，`XUEQIU_COOKIE_FILE`
  填容器内路径，见 [雪球运维](#8-雪球运维)。
- **Tushare**：`TUSHARE_TOKEN` 可留空；此时不能主动从 Tushare 刷新行情，分红公告与基本面档案同步也不可用。
- **AI 功能**：填 `LLM_REPORT_API_KEY`。留空时 AI 复盘和标的分析接口保持禁用，定期计划不会调用外部模型。
  启用后，生成报告、追问和标的分析会把相应的账本或公开行情输入发送给 `LLM_REPORT_BASE_URL`
  指向的外部服务；上线前应确认数据范围、供应商条款和隐私要求。
- 改完 `.env` 后 `docker compose up -d` 重建配置有变化的容器（雪球 Cookie 两个服务都读）。

### 4.7 可选：启用雪球采集器

采集器随 `docker compose up -d` 一起启动，但默认只空转。全新部署要启用它（从旧版独立采集程序
迁移过来的部署走 [8.4 一次性切换流程](#84-从外部-archiver-切换到内置采集器一次性)）：

1. 按 [8.1](#81-cookie-更新流程) 放好 Cookie，并确认有效：
   `docker compose exec -T backend python scripts/check_xueqiu_cookie_expiry.py --probe`；
2. 先做一次零写入的试抓，确认签名、Cookie 与解析都正常（`<作者ID>` 是雪球个人主页地址里的数字）：

   ```bash
   docker compose exec -T xueqiu-collector python manage.py xueqiu-collector \
     --dry-run --max-posts 3 --author <作者ID>
   ```

   输出 `cycle status=ok`（个别帖子失败时为 `partial`）、作者行为 `ok`/`partial` 并列出若干
   `profile:…` / `comment:…` 键即正常；
   作者行为 `error` 说明主页首页就拿不到合法响应，先查 Cookie；
3. `.env` 设 `XUEQIU_COLLECTOR_ENABLED=true`（可选再设 `XUEQIU_COLLECTOR_PUSH_URL`），然后
   `docker compose up -d backend xueqiu-collector`——backend 也要重建，它靠这个开关决定是否允许
   「立即运行」、观点页显示什么状态；
4. 以管理员登录「雪球观点」页，在采集器卡片里核对关注作者名单（按需增删、停用）与可选的组合名单。
   启用后第一轮作者采集会在 30 秒内开始，之后每 60 分钟一轮；按标的轮次在当天 07:30 之后跑一次；
5. `docker compose logs -f xueqiu-collector` 与卡片上的「最近运行」应出现 `ok` 或 `partial` 记录。

`python manage.py rebuild-holdings` 可随时从交易与公司行动全量重放持仓（幂等；输出 Failures 列表
说明存在真实超卖数据，修正后重跑）。

### 群晖 NAS

1. 安装 Container Manager；视 DSM 版本，SSH 里可用的可能只有 `docker-compose`。
2. 上传项目到例如 `/volume1/docker/investment-tracker`，准备 `.env` 与证书。
3. SSH 进入该目录执行：

```bash
cd /volume1/docker/investment-tracker
SHA=$(git rev-parse --short HEAD)            # 没有 git 就手工写上传的版本号
if grep -q '^BUILD_SHA=' .env; then
  sed -i.bak "s/^BUILD_SHA=.*/BUILD_SHA=$SHA/" .env && rm -f .env.bak
else
  printf '\nBUILD_SHA=%s\n' "$SHA" >> .env
fi
sudo docker compose build
sudo docker compose run --rm --user root backend chown -R 10001:10001 /app/logs
sudo docker compose run --rm backend alembic upgrade head
sudo docker compose run --rm backend python manage.py seed
sudo docker compose up -d
sudo docker compose ps
```

如需改端口，改 `.env` 的 `FRONTEND_HTTP_PORT` / `FRONTEND_HTTPS_PORT`，并同步 `CORS_ORIGINS`。

---

## 5. 升级清单

按顺序执行，每一步确认成功再继续。

### 5.1 看清这次升级带来了什么

```bash
# 当前生产版本 = /health 的 build 字段
OLD=$(docker compose exec -T backend python -c "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5))['build'])")
git fetch origin
git log --oneline "$OLD"..origin/main

# 新迁移（决定备份范围）
git diff --name-only "$OLD" origin/main -- backend/alembic/versions

# 版本号变化（决定部署后数据任务，见第 6 节）
git diff "$OLD" origin/main -- backend/app/services | grep -E '^[-+][A-Z_]+_VERSION = '
```

`OLD` 是 `unknown` 时说明上次部署没写 `BUILD_SHA`，只能按部署日期估一个提交。

### 5.2 定备份范围并备份

| 情况 | 备份范围 |
| --- | --- |
| 数据库实例**专用**于本应用 | 整库：`BACKUP_MODE=postgres ./backup.sh` |
| 数据库实例与其他项目**共享** | 本项目全部表 + `alembic_version`（整库 dump 会带走别人的表，恢复面不可控） |
| 共享库、且本次**只新增表/列**、部署后任务只改写全局派生表 | 可只备份迁移与任务会改写的表 + `alembic_version`（省时间；判断不准就退回上一行） |

判断依据：读 5.1 列出的每个迁移文件的 `upgrade()`——有改写/删除已有数据、删列、回填，或动到
账本表（交易、持仓、公司行动、现金事件、导入批次等），一律备份本项目全部表；再对照第 6 节
矩阵看部署后任务会改写哪些表（大多是 `security_profile_data`）。表级备份时**表清单必须覆盖
迁移动到的全部表**，否则回退后 `alembic_version` 与实际 schema 对不上，下次升级会在同一迁移上失败。

```bash
# 整库
BACKUP_MODE=postgres ./backup.sh

# 本项目全部表（从模型元数据生成清单，不要手抄）+ alembic_version
TABLES="$(docker compose exec -T backend python -c 'from app.database import Base; import app.models; print(" ".join(sorted(Base.metadata.tables)))') alembic_version"
BACKUP_TABLES="$TABLES" BACKUP_MODE=postgres ./backup.sh

# 只备份部分表（例）
BACKUP_TABLES="security_profile_data alembic_version" BACKUP_MODE=postgres ./backup.sh
```

确认脚本退出码为 0、生成了 `.dump` 与 `.sha256` 再继续。脚本细节见 [备份与恢复](#9-备份与恢复)。

### 5.3 部署前 metrics 快照（改了收益口径时）

5.1 里有改动涉及统计/持仓/FIFO/汇率折算时，用**旧代码**给用户看到的数字拍一张快照，部署后再
拍一张对比（`scripts/metrics_parity_report.py`，只读）：

```bash
# 在仍在运行的旧容器里跑，直接写进挂载的日志目录（宿主 BACKEND_LOG_DIR 下可见）
docker compose exec -T backend python scripts/metrics_parity_report.py \
  --user-id <账本用户 id> --out /app/logs/parity_before_$OLD.json
```

**快照必须落在挂载目录或先拷出容器**：`docker compose up -d` 会重建 backend 容器，容器内
`/tmp` 下的文件随之消失。若写在了 `/tmp`，在重建之前拷出来：

```bash
docker cp investment-tracker-backend:/tmp/parity_before.json ./backups/
```

### 5.4 拉代码、构建

```bash
git pull --ff-only
SHA=$(git rev-parse --short HEAD)
if grep -q '^BUILD_SHA=' .env; then
  sed -i.bak "s/^BUILD_SHA=.*/BUILD_SHA=$SHA/" .env && rm -f .env.bak
else
  printf '\nBUILD_SHA=%s\n' "$SHA" >> .env
fi
docker compose build
```

构建期间旧容器照常服务。`.env.example` 有新增变量时，对照着补进 `.env`。

### 5.5 停后端与采集器 → 迁移 → 启动 → 健康检查

先停所有[写库进程](#写库进程)。还在用旧 archiver 定时任务的部署（切换前或过渡期），先注释掉
crontab 里它的两行并确认没有正在运行的实例——迁移 `…_0024` 要给 `xueqiu_archiver_scan_runs` 加列，
旧程序同时在写会让迁移等锁或失败；迁移完成、服务起来后再按需恢复那两行。

```bash
# 采集器与 backend 读写同一个库：迁移期间两者都要停（首次升级到带采集器的版本时，
# xueqiu-collector 容器还不存在，stop 它是空操作）
docker compose stop backend xueqiu-collector
# 日志目录权限（首次从 root 镜像升级到非 root 镜像时必需；之后执行也无害）。
# 必须先停旧后端再 chown：仍在运行的 root 容器会在日志轮转时重新建出 root-owned 文件
docker compose run --rm --user root backend chown -R 10001:10001 /app/logs
docker compose run --rm backend alembic upgrade head
docker compose up -d
docker compose ps

docker compose exec -T backend python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).read().decode())"
```

`build` 必须等于新的 `$SHA`，`database` 为 `reachable`。`docker compose logs -f backend` 看一眼
启动日志与首轮周期任务；`docker compose ps` 里 xueqiu-collector 约 2 分钟后应为 `healthy`。

`up -d` 会用新镜像**同时重建** xueqiu-collector：一轮作者采集进行到一半时被停掉不丢数据——
已写入的行保留，未结束的 `scan_runs` 在下一轮开始时标为 interrupted，重启后按间隔照常继续。
不想打断正在进行的一轮，可以先在采集器卡片或 `docker compose logs xueqiu-collector` 确认
当前没有轮次在跑再停。首次升级到带采集器的版本时，迁移 `…_0024`/`…_0025` 会建出采集器表，
采集器默认不启用；已在用旧版独立采集程序的部署按 [8.4](#84-从外部-archiver-切换到内置采集器一次性)
切换。

### 5.6 部署后数据任务

按 5.1 的版本号变化，对照 [第 6 节矩阵](#6-部署后数据任务矩阵) 执行；长任务按 [第 7 节](#7-长任务运维)
后台运行。

### 5.7 部署后快照对比（做了 5.3 时）

```bash
docker compose exec -T backend python scripts/metrics_parity_report.py \
  --user-id <账本用户 id> --out /app/logs/parity_after_$SHA.json
docker compose exec -T backend python scripts/metrics_parity_report.py \
  --compare /app/logs/parity_before_$OLD.json /app/logs/parity_after_$SHA.json
```

差异分三类：新增字段 / 删除字段 / **数值变化**。数值变化的每一条都要能用本次改动解释，解释
不了就回退排查。

### 5.8 清理旧备份

```bash
./backup.sh --prune --keep 2 --dry-run   # 先看会删什么
./backup.sh --prune --keep 2
```

只删超出份数的完整备份 `investment_<时间>.dump` 及其 `.sha256`；表级备份与未完成的 `.partial`
默认不碰（见 [备份与恢复](#9-备份与恢复)）。

### 回退

**不要在生产库上执行 `alembic downgrade`**。回退 = 恢复升级前那份已读检通过的备份 + 旧代码。
恢复前停所有[写库进程](#写库进程)（仍在用旧 archiver 定时任务的部署同样先注释它的两行），恢复验证
通过、旧代码构建好之后再启动：

```bash
docker compose stop backend xueqiu-collector
# 旧 archiver 定时任务若仍在用：crontab -e 注释它的两行，确认没有残留进程
# 按 9.4 恢复升级前的 dump（整库恢复到新库再切 DATABASE_URL，或表级 --clean 恢复），并验证
git checkout "$OLD"
# 按 5.4 改 BUILD_SHA 并重新构建，然后（旧版 compose 里若没有 xueqiu-collector 服务，
# --remove-orphans 会把新版留下的采集器容器一并删掉，否则它会继续用新代码跑）
docker compose up -d --remove-orphans
```

旧 archiver 的定时任务是否恢复，取决于回退到的版本：回到带内置采集器的版本且已切换过，就保持注释；
回到切换之前的版本，再恢复那两行。

注意 `broker_fund_flows` 按券商账户去重，两个真实账户可以合法拥有相同的 `row_hash`——恢复后
不要按 `row_hash` 单列「去重」。

---

## 6. 部署后数据任务矩阵

派生数据（财报摘要、港股报表行、EDGAR 透视行、ADS 换算比……）都带版本号；代码里的版本号一变，
旧版本的行就被视为过期。下面按「改了什么 → 跑什么」列出。命令都在运行中的 backend 容器里执行
（`docker compose exec -T backend python <命令>`，长任务见第 7 节的后台写法）。

| 改动（5.1 的 grep 结果） | 所在文件 | 跑什么 | 成本 |
| --- | --- | --- | --- |
| `STATEMENT_EXTRACTOR_VERSION` 或 `STATEMENT_PROMPT_VERSION` | `report_statements.py` / `report_statement_prompts.py` | `scripts/rerun_report_statements.py --all`（先 `--dry-run` 看份数）；同时升了构建/校验版本也只跑这一条（重抽时按当前构建与校验口径写行） | prompt 升版：零下载、每份一次 LLM。抽取器升版：每份都重下载重定位（约 15–20 秒/份，约 300 份 ≈ 1.5 小时），但**解析结果与存量逐字节相同的沿用旧映射、不调 LLM**（输出 `mapping_reused`），只有解析真的变了的报告才重新映射（每份约 5k 输入 + 4k 输出 token）。跑完前该版本的全部港股报表行都隐藏（页面显示「待重抽」、分析回退雅虎），部署后立即跑。可按标的分组并行、中断后重跑即续跑；披露易 504/超时是瞬时错误，重跑即可 |
| `STATEMENT_BUILD_VERSION` | `report_statement_prompts.py` | `scripts/rebuild_report_statements.py --all --report`（可先加 `--dry-run`） | 零下载零 LLM，分钟级；`--report` 按标的输出前后对比 |
| `STATEMENT_VALIDATION_VERSION` | `report_statement_checks.py` | `scripts/revalidate_report_statements.py --all` | 零下载零 LLM，分钟级 |
| `SECTION_EXTRACTOR_VERSION` 或 `DIGEST_PROMPT_VERSION` | `report_sections.py` / `report_digest_prompts.py` | 先确认 `scripts/report_extraction_audit.py --fixtures` 的 boilerplate 归零（在开发检出里跑：固件在 `backend/tests/fixtures/`，镜像不带 tests；容器里可用 `--live` 抽查库内节选），再 `scripts/rerun_report_digests.py --all`（先 `--dry-run`） | 最贵：A股/港股/美股年报重下载 + 每份一次 LLM。商业画像按输入指纹自动重算，不用单独跑 |
| `EDGAR_PIVOT_VERSION` | `earnings_quality.py` | 重新同步美股档案（下方命令） | 只打 EDGAR，无 LLM；每只几秒 |
| `ADS_PARSER_VERSION`，或新增 ADS 换算比的迁移（`…_0023_ads_ratio`） | `ads_ratio_service.py` | `scripts/sync_ads_ratios.py --all`（`--force` 忽略缓存重解析） | 每只 20-F 发行人下载一次年报主文档，无 LLM |
| 标的全集加载逻辑 | `security_catalog_service.py` | 周期任务自动跑；要立即生效：`manage.py sync-security-catalog` | 约 1 分钟 |
| 港交所日报解析 / 需要补历史 | `hkex_dayquot_source.py` | 周期任务自动推进；补跑：`manage.py sync-hkex-dayquot --days N`（站点只存约一个月） | 每份约 25MB |
| 持仓重放口径（公司行动语义、持仓计算） | `holding_service.py`、`portfolio/semantics.py` | `manage.py rebuild-holdings` | 分钟级；输出 Failures = 真实超卖数据 |
| 收益/统计口径 | `services/statistics/`、`services/portfolio/` | 5.3 / 5.7 的 metrics 快照对比 | 只读 |
| 无风险利率（参考利率表，迁移 `…_0030_reference_rates` 首次部署） | `reference_rate_service.py` | 周期任务（12 小时）首次按最早交易日自动回填 SHIBOR 3M 与美国国库券 3M；要立即生效：`manage.py sync-reference-rates`。夏普/索提诺从此按 SHIBOR 3M 逐期扣除（此前为 0），metrics 快照对比时这两项与 `risk_free_rate` 的变化是预期的 | 中国货币网与美国财政部每年各一次请求 |
| 汇率历史（官方中间价回填，迁移 `…_0027_exchange_rate_checks` 首次部署） | `exchange_rate_service.py`、`chinamoney_source.py` | 先取 metrics 快照 → `scripts/backfill_official_fx.py --start <最早交易日> --dry-run` 看将改写/新写/停用的行数 → 去掉 `--dry-run` 执行 → 再取快照对比（差异应全部来自汇率变化）。日常刷新由周期任务完成，只回看 15 天 | 中国货币网每年一次请求；**会改变历史人民币折算**（此前早于首条汇率的日期按最新汇率折算） |

重新同步美股档案（只刷新已有 EDGAR 档案的美股，不产生新的 AI 分析）：

```bash
docker compose exec -T backend python - <<'PY'
from app.database import SessionLocal
from app.models.security_profile import SecurityProfileData
from app.services.security_profile_service import sync_symbol_profile

db = SessionLocal()
try:
    rows = (
        db.query(SecurityProfileData.symbol)
        .filter(SecurityProfileData.market == "美股",
                SecurityProfileData.dataset == "edgar_companyfacts")
        .distinct()
    )
    symbols = sorted(symbol for (symbol,) in rows)
    print(f"{len(symbols)} 只美股", flush=True)
    for symbol in symbols:
        result = sync_symbol_profile(db, symbol, "美股")
        print(symbol, result["datasets"].get("edgar_companyfacts"), result["failed"], flush=True)
finally:
    db.close()
PY
```

执行任务前后要知道的几件事：

- **版本号一变，旧行立即隐藏**：过期的报表行/摘要在任务跑完之前不会显示、也不进 AI 分析输入
  （页面显示为「待重抽」或缺数据，不是数据丢了）。所以要在部署后尽快把对应任务跑完。
- **AI 分析是快照**：已生成的标的分析、复盘报告不会因为数据更新而改变，需要新结论就重新生成。
- **批量分析有 24 小时新鲜度**：持仓页「一键分析」会跳过 `SECURITY_ANALYSIS_FRESHNESS_HOURS`
  内已分析过的标的。数据任务跑完想全部重出，可以在标的详情页逐个重新分析，或用 API
  `POST /api/securities/analysis-batch-jobs?force=true`（Bearer token 见 `POST /api/auth/token`）。
- 需要 LLM 的任务在 `LLM_REPORT_API_KEY` 为空时直接报错退出；无效 key / 欠费 / 限流属于致命错误，
  脚本会停下并说明原因，修好后重跑即可续上。

---

## 7. 长任务运维

`rerun_report_statements.py`、`rerun_report_digests.py` 这类脚本可能跑数小时，要在容器里**后台**
运行、日志写进挂载目录：

```bash
docker compose exec -d -e PYTHONUNBUFFERED=1 backend sh -c \
  'python scripts/rerun_report_statements.py --all > /app/logs/rerun_statements_$(date +%Y%m%d_%H%M%S).log 2>&1'

# 等价写法：直接对容器 docker exec -d
docker exec -d -e PYTHONUNBUFFERED=1 investment-tracker-backend sh -c \
  'python scripts/rerun_report_statements.py --all > /app/logs/rerun_statements.log 2>&1'
```

- **日志只能写 `/app/logs`**：它是宿主 `BACKEND_LOG_DIR` 的挂载，容器重建后仍在。写在容器内
  `/tmp` 的日志与结果会随 `docker compose up -d` 重建一起消失。
- **跑长任务期间不要重建 backend**（`up -d`、`build` 后的重启都会重建）：`exec` 起的进程随容器
  一起被杀。脚本都可续跑，被杀了也只是需要重跑一遍。
- **进度**：`docker compose exec backend tail -f /app/logs/<日志名>`（或在宿主直接看
  `BACKEND_LOG_DIR`）；`docker compose top backend` 看进程是否还在；随时再跑一次 `--dry-run`
  看剩余份数；港股标的详情页的「报表抽取进度」也会实时变化。
- **续跑**：版本号本身就是完成标记，已按当前版本处理过的报告会被跳过，所以中断后原样重跑同一
  条命令即可。确定性失败（同一份报告两次都失败）会封顶不再重试，进度里单独列出。
- **按标的分组并行**：`rerun_report_statements.py` 一次处理一个 `--symbol`，可以把 `--dry-run`
  列出的标的分成几组、每组一个后台进程：

  ```bash
  SYMS=$(docker compose exec -T backend python scripts/rerun_report_statements.py --all --dry-run \
    | awk '$1 ~ /:$/ && $3 == "份" {sub(":", "", $1); print $1}')
  for k in 0 1 2; do
    PART=$(printf '%s\n' $SYMS | awk -v k=$k 'NR % 3 == k' | tr '\n' ' ')
    docker compose exec -d -e PYTHONUNBUFFERED=1 -e PART="$PART" backend sh -c \
      "for s in \$PART; do python scripts/rerun_report_statements.py --symbol \$s; done > /app/logs/rerun_part_$k.log 2>&1"
  done
  ```

  限速器是**进程内**的（按站点分桶），N 个进程就是 N 倍的披露易请求速率与 LLM 并发，2–3 组为宜；
  同一只标的不要同时出现在两组里。
- 这些脚本不经过 worker 车道，与页面上触发的分析/回填任务可以同时跑——期间别再在页面上发起
  批量分析或「补齐财报摘要」，否则对外部源是双倍请求。

---

## 8. 雪球运维

雪球只靠登录态 Cookie，不会自动刷新；`xq_a_token` 约 15 天过期。本应用有两处用它，**共用同一份
Cookie**（`XUEQIU_COOKIE_FILE` / `XUEQIU_COOKIES`，同一个只读挂载目录）：

- backend：`stock.xueqiu.com` 的行情兜底与 A股 `xueqiu_*` 档案数据集；
- xueqiu-collector：`xueqiu.com` 的作者发言采集与按标的采集。

Cookie 过期时 backend 侧不显眼（行情只是 Tushare 之后的兜底，档案只多几条失败记录），采集器侧则
整轮失败、观点页出现停摆告警。

### 8.1 Cookie 更新流程

1. 在浏览器登录雪球，用 Cookie 导出插件导出 `xueqiu.com` 的**完整 JSON**（带 `expirationDate`
   字段，探活与采集器每轮自检都靠它提前告警；缺主凭证 `xq_a_token`/`xqat` 直接判 critical）。
2. 覆盖 `XUEQIU_COOKIE_HOST_DIR` 下的文件（文件名与 `XUEQIU_COOKIE_FILE` 对应），并让容器内的
   uid 10001 读得到：`chmod 664 <文件>`（`644` 也可），所在目录要有 `o+x`。
3. 重启两个服务：`docker compose restart backend xueqiu-collector`（只换了文件、没改 `.env`，
   restart 即可）。backend 在首次使用时把 Cookie 读进进程内缓存，必须重启；采集器其实每轮开始都会
   重读文件，一并重启只是省得等。
4. 验证：

   ```bash
   # 到期日（离线）+ --probe 发一次真实请求；退出码 0 正常 / 1 warning / 2 critical / 3 未配置
   docker compose exec -T backend python scripts/check_xueqiu_cookie_expiry.py --probe
   ```

   然后在「雪球观点」页的采集器卡片点「立即运行」，看下一轮作者状态是否回到 `ok`/`partial`（不再是 `error`）。

挂载的是**目录**不是文件：bind 一个不存在的文件路径时 Docker 会在宿主上把它误建成同名目录，
之后真文件就放不进去了。

探活脚本也可以挂在宿主 cron 上推送到 Uptime Kuma（normal 推 up，warning/critical/未配置推 down）：
`docker compose exec -T xueqiu-collector python scripts/check_xueqiu_cookie_expiry.py --push-url <push 地址>`。

### 8.2 采集器状态与监控

- **「雪球观点」页的采集器卡片**（登录可看，操作仅管理员）：启用状态、心跳、上一轮开始/结束时间与
  结果、WAF 冷却到何时、Cookie 到期状态、最近 10 次 `scan_runs`、按标的轮次的状态行（含「今日待重试」）、
  作者与组合名单。
- **停摆告警**：观点页与仪表盘的数据质量提示按 `scan_runs` 里状态为 **`ok` 或 `partial`** 的最新结束
  时间判断（没有这类记录时退回发言表的 `max(last_seen_at)`），超过 `XUEQIU_OPINION_STALE_HOURS`
  （默认 48 小时）即告警。`error` / `failed` / `waf` 不刷新活性——主页都拿不到合法响应的轮次不能证明
  数据在流动，所以 Cookie 失效或被 WAF 拦住时告警会如期出现，而不是被「跑过了」掩盖。`partial` 计入
  活性：主页成功、只是个别帖子的全文或评论失败。采集器从未有过 `ok`/`partial` 且库里一条发言都没有
  时，观点功能显示「数据源未接入」（接口 409），不会静默给空结果。
- **容器健康**：`docker compose ps` 的 `(healthy)` 来自心跳文件，只说明进程活着、循环在转；
  采集是否成功看卡片与 `scan_runs`。
- **Uptime Kuma**：设 `XUEQIU_COLLECTOR_PUSH_URL` 后每轮作者采集结束推送一次：本轮全部作者都是
  `ok`/`partial` 才推 up；任一作者 `error`/`failed`/`waf`、Cookie 不可用或主凭证已到 critical 推 down
  （名单为空推 up，被停机中断的一轮不推）。
  监控项的心跳间隔按轮次间隔（默认 60 分钟）留余量设置。
- **日志**：`docker compose logs -f xueqiu-collector`，或宿主 `BACKEND_LOG_DIR/xueqiu-collector.log`。

直接查库（在 backend 容器里执行，只读）：

```bash
docker compose exec -T backend python - <<'PY'
from sqlalchemy import text
from app.database import engine

with engine.connect() as conn:
    for row in conn.execute(text(
        "select target_user_id, status, waf_hit, utterance_count, started_at, finished_at, "
        "error_message from xueqiu_archiver_scan_runs order by run_id desc limit 10"
    )):
        print(tuple(row))
    print(conn.execute(text(
        "select count(*), max(last_seen_at) from xueqiu_archiver_utterances"
    )).one())
PY
```

### 8.3 名单维护、手动运行与 WAF

- **关注作者 / 组合名单**：在采集器卡片里增加、停用、删除（管理员）。作者用雪球个人主页地址里的数字
  ID，组合用 `ZH` 开头的组合代码。每轮作者采集取最久没跑的最多 `XUEQIU_COLLECTOR_MAX_AUTHORS_PER_RUN`
  位，名单长了就是多轮轮流覆盖。按标的采集的范围不需要维护：它跟着全体用户的持仓与自选走。
- **立即运行**：卡片上的按钮（`POST /api/xueqiu-collector/run-now`，`?target=symbols` 为按标的）只在
  数据库里写请求时间，采集器 30 秒内拾取；未启用时返回 409，WAF 冷却期内要等冷却结束。
- **命令行**（在 xueqiu-collector 容器里；与常驻循环共用 advisory lock，循环正在跑一轮时会直接跳过）：

  ```bash
  # 跑一轮作者采集后退出；--author 可重复，只跑指定作者
  docker compose exec -T xueqiu-collector python manage.py xueqiu-collector --once
  # 零写入试抓：主页第 1 页、最多 N 个候选帖、每帖 1 页评论，打印将写入的 utterance_key
  docker compose exec -T xueqiu-collector python manage.py xueqiu-collector \
    --dry-run --max-posts 3 --author <作者ID>
  # 跑一轮按标的采集；--symbol/--market 只跑指定标的（不含组合与热帖，也不记当天已跑）
  docker compose exec -T xueqiu-collector python manage.py xueqiu-collector --symbols-once
  docker compose exec -T xueqiu-collector python manage.py xueqiu-collector \
    --symbols-once --symbol 600519 --market A股
  ```

- **WAF**：采集器识别到阿里云 WAF 挑战页会立即中止本轮，该作者的 `scan_runs` 记 `waf`，
  `XUEQIU_COLLECTOR_WAF_COOLDOWN_SECONDS`（默认 30 分钟）内不开新一轮。偶发一次不用处理。连续多轮
  命中时：用同一账号在浏览器里打开雪球、完成人机验证，按 8.1 重新导出 Cookie 并重启；仍频繁命中就
  调大 `XUEQIU_COLLECTOR_MIN_DELAY_SECONDS` / `XUEQIU_COLLECTOR_MAX_DELAY_SECONDS` 或减少
  `XUEQIU_COLLECTOR_MAX_AUTHORS_PER_RUN`（改 `.env` 后 `docker compose up -d xueqiu-collector`）。
  **不要**为了赶进度调低限速或多开实例。

### 8.4 从外部 archiver 切换到内置采集器（一次性）

只适用于此前用独立的 xueqiu-timeline-archiver（宿主 crontab 定时跑）往本应用数据库写
`xueqiu_archiver_*` 表的部署；全新部署看 [4.7](#47-可选启用雪球采集器)。内置采集器沿用同一套表、
同一套主键（`utterance_key` = `profile:<作者ID>:<帖子ID>` / `comment:<评论ID>`）和同样的 upsert
语句，存量数据原地续用，不需要迁移数据。

1. **前提检查**：旧程序写的表必须就在本应用 `DATABASE_URL` 指向的库里（观点页一直读的就是它）。
   在 backend 容器里执行 8.2 的查库命令，发言数与 `max(last_seen_at)` 应与旧程序一致。若旧程序写的
   是另一个库，先把五张 `xueqiu_archiver_*` 表迁到本库（`pg_dump -t 'public.xueqiu_archiver_*'` +
   `pg_restore`），再继续。
2. **升级到带采集器的版本**（第 5 节；备份范围要包含 `xueqiu_archiver_*` 五张表——按 5.2 从模型
   元数据生成的表清单已经包含）。迁移 `…_0024` 对已有的五张表是空操作（`IF NOT EXISTS`），只给
   `scan_runs` 追加四列；采集器此时默认不启用。**升级的停机窗口里旧程序也要暂停**（5.5：注释它的
   crontab 两行、确认无残留进程），迁移完成、服务起来后再恢复那两行，旧程序继续写到第 4 步正式停用。
3. **影子运行**（零写入）：挑一两位作者试抓，把打印出的键与库里旧程序写的键对比——键的格式与取值
   一致才能切换，否则切换后会出现重复发言：

   ```bash
   docker compose exec -T xueqiu-collector python manage.py xueqiu-collector \
     --dry-run --max-posts 5 --author <作者ID> | tee shadow.txt
   grep -E '^    (profile|comment):' shadow.txt | tr -d ' ' | sort -u > shadow_keys.txt

   docker compose exec -T backend python -c "
   import sys
   from sqlalchemy import text
   from app.database import engine
   keys = [line.strip() for line in sys.stdin if line.strip()]
   with engine.connect() as conn:
       found = {r[0] for r in conn.execute(text(
           'select utterance_key from xueqiu_archiver_utterances where utterance_key = any(:k)'),
           {'k': keys})}
   print(f'{len(keys)} 个键，库内已有 {len(found)} 个')
   for key in keys:
       if key not in found:
           print('  库内没有：', key)
   " < shadow_keys.txt
   ```

   预期：绝大多数键库内已有；「库内没有」的只应是旧程序上次运行之后才出现的新发言。若大面积对不上
   （例如格式不同），停止切换并排查。也可以用 `--database-url <测试库>` 对一个已迁移的测试库真实写入
   一轮再比对（库名必须含 test/e2e/shadow）。
4. **停掉旧程序的定时任务**：先备份 crontab，再注释掉 archiver 的两行（每小时的作者监控脚本、每天
   07:30 的按标的监控脚本）：

   ```bash
   crontab -l > crontab.backup.$(date +%Y%m%d_%H%M%S)
   crontab -e    # 注释掉 xueqiu-timeline-archiver 的两行
   ```

   确认旧程序没有正在运行的实例（它的锁目录/进程已退出）再继续。旧程序那份单独的 Cookie 从此不再使用。
5. **导入旧程序的按标的 Markdown 快照**（可选；旧程序按标的监控只写 Markdown、不写库）：把它的
   `exports/` 目录只读挂进一次性容器，先演练再正式导入。文件要对 uid 10001 可读（`o+r`）：

   ```bash
   docker compose run --rm --no-deps -v /path/to/xueqiu-timeline-archiver/exports:/import:ro \
     xueqiu-collector python manage.py xueqiu-import-archive-exports --dir /import --dry-run
   docker compose run --rm --no-deps -v /path/to/xueqiu-timeline-archiver/exports:/import:ro \
     xueqiu-collector python manage.py xueqiu-import-archive-exports --dir /import
   ```

   只插入不覆盖（已有行保留）、可重复执行；公告/讨论与热帖快照会导入，组合调仓 Markdown 没有调仓 ID
   不导入；导入的行没有作者昵称与附件链接。
6. **启用内置采集器**：`.env` 设 `XUEQIU_COLLECTOR_ENABLED=true`（旧程序用过 Uptime Kuma 的话，把
   push 地址填进 `XUEQIU_COLLECTOR_PUSH_URL`），然后 `docker compose up -d backend xueqiu-collector`。
   在采集器卡片里对照旧程序的作者名单与组合名单文件核对（迁移可能已预置一份作者名单），第一轮作者
   采集会在 30 秒内开始。
7. **24 小时观察**：
   - `scan_runs` 每小时都有新记录、以 `ok`/`partial` 为主，没有成片的 `error`（卡片「最近运行」或 8.2 的查库命令）；
   - 发言数持续增长、`max(last_seen_at)` 跟着前进；切换后新出现的键只有两种前缀，且没有与旧键
     「同帖不同键」的重复：

     ```sql
     select count(*) from xueqiu_archiver_utterances            -- 应为 0
     where first_seen_at > '<切换时间>'
       and utterance_key not like 'profile:%' and utterance_key not like 'comment:%';

     select target_user_id, post_id, count(*) from xueqiu_archiver_utterances   -- 应无结果
     where utterance_key like 'profile:%' group by 1, 2 having count(*) > 1;
     ```

   - 观点页与仪表盘不再报停摆；
   - 次日 07:30 之后按标的轮次跑过（卡片「按标的」一行、标的详情的「雪球公告 / 讨论」有新帖）；若显示
     「今日待重试」，看它在最多 3 轮内清零，或用尽后次日整轮补上；
   - Uptime Kuma（若配置）按轮次收到推送；`docker compose ps` 中 xueqiu-collector 为 `healthy`。
8. **回退**：`.env` 改回 `XUEQIU_COLLECTOR_ENABLED=false`，`docker compose up -d backend xueqiu-collector`，
   再用第 4 步的备份恢复 crontab（`crontab crontab.backup.<时间>`）。表与主键完全兼容，内置采集器写入
   的行保留，旧程序接着 upsert。

---

## 9. 备份与恢复

运行数据全部在 PostgreSQL 里。`data/` 目录与 Excel 导出都不能替代数据库备份。

### 9.1 `backup.sh`

```bash
./backup.sh                                   # 交互选择：1 数据库 / 2 Excel / 3 两者
BACKUP_MODE=postgres ./backup.sh              # 非交互整库备份
BACKUP_TABLES="t1 t2 alembic_version" BACKUP_MODE=postgres ./backup.sh   # 表级备份
BACKUP_MODE=postgres ./backup.sh --prune --keep 2                        # 备份成功后清理
./backup.sh --help
```

数据库备份流程（不论用哪种客户端都一样）：写 `investment_<时间>.dump.partial` → `pg_dump`
退出码为 0 → 文件非空 → `pg_restore --file=/dev/null` 完整读检 → 计算 SHA256 → 原子改名为
`.dump`。只有读检通过才会出现 `.dump` 与配套 `.sha256`；残留的 `.partial` 表示一次未完成或未通过
验证的备份，**不能**用于恢复。表级备份命名为 `investment_tables_<时间>.dump`。

- **客户端**：宿主有 `pg_dump`/`pg_restore` 就用本机的；没有则自动改用一次性 `docker run --rm
  postgres:16` 容器（backend 镜像刻意不含 pg_dump）。`BACKUP_PG_TOOL=local|docker` 可强制。宿主
  客户端主版本低于数据库时（`pg_dump` 报 `server version mismatch`），没有强制 `local` 就自动改用
  容器重跑一遍；其他失败原因不重跑，`.partial` 原样保留。容器模式默认 `--network host`（与宿主同样的网络可达性），
  以当前用户身份写文件，连接串经环境变量传入、不出现在进程参数里。
- **连接串**：优先取 shell 里的 `DATABASE_URL`；没有就向 compose 的 backend 服务读取（运行中用
  `exec`，已停止用一次性 `run`）。脚本不读 `.env`。
- **`.sha256` 记裸文件名**：在备份目录里就能校验，拷到别的机器、别的路径也一样：

  ```bash
  (cd backups && sha256sum -c investment_YYYYMMDD_HHMMSS.dump.sha256)       # Linux
  (cd backups && shasum -a 256 -c investment_YYYYMMDD_HHMMSS.dump.sha256)   # macOS
  ```

  （旧版脚本生成的 `.sha256` 里记的是 `./backups/...` 相对路径，要在仓库根目录校验。）

### 9.2 保留策略

```bash
./backup.sh --prune --keep 2 --dry-run    # 只列出将删除的文件
./backup.sh --prune --keep 2              # 保留最新 2 份完整备份
./backup.sh --prune --keep 2 --include-partial
```

- 默认只处理完整备份 `investment_YYYYMMDD_HHMMSS.dump` 与同名 `.sha256`，按文件名里的时间保留最新 N
  份（默认 2，至少 1），每删一个文件都会打印出来。
- 表级备份、`.partial` 残留、Excel 导出、其他文件默认一概不碰；`.partial` 只列出提示。
- `--include-partial`：表级备份同样保留最新 N 份；并删除**早于最新完整备份**的 `.partial` 残留
  （更晚的可能是正在进行的备份，不删）。

定时备份示例（crontab；cron 的 PATH 很短，要写全；退出码非 0 即失败）：

```cron
PATH=/usr/local/bin:/usr/bin:/bin
30 3 * * * cd /path/to/investment-tracker && BACKUP_MODE=postgres ./backup.sh --prune --keep 7 >> backups/backup.log 2>&1
```

不要用后台重定向后立即宣告成功；以脚本退出码作为成败依据。

### 9.3 表级备份的表清单

数据库实例被多个项目共享时，整库 dump 既大、恢复面也不可控，改做表级备份。表清单不要手抄，从
模型元数据生成，再加上 `alembic_version`：

```bash
TABLES="$(docker compose exec -T backend python -c 'from app.database import Base; import app.models; print(" ".join(sorted(Base.metadata.tables)))') alembic_version"
BACKUP_TABLES="$TABLES" BACKUP_MODE=postgres ./backup.sh
```

恢复同样只允许精确到本项目的表。

### 9.4 恢复

恢复前先校验 SHA256。**整库恢复优先恢复到新建的空库**，确认无误后再切 `DATABASE_URL`。
恢复到新库本身不受线上写入影响；但**切 `DATABASE_URL` 之前**必须停所有[写库进程](#写库进程)
（`docker compose stop backend xueqiu-collector`，仍在用旧 archiver 定时任务的部署再注释它的两行），
否则备份之后到切换之间旧库上的新写入会丢失、旧 archiver 还会继续写旧库。切换后
`docker compose up -d`，验证通过再决定旧 archiver 的两行是否恢复（见 5「回退」末尾）：

```bash
(cd backups && sha256sum -c investment_YYYYMMDD_HHMMSS.dump.sha256)

# 先在数据库上建一个空库（例如 investment_restore），然后：
export RESTORE_DATABASE_URL='postgresql://<db-user>:<db-password>@<db-host>:5432/investment_restore'

# 宿主有 pg_restore：
pg_restore --exit-on-error --single-transaction --no-owner \
  --dbname="$RESTORE_DATABASE_URL" backups/investment_YYYYMMDD_HHMMSS.dump

# 宿主没有 pg_restore：用一次性容器（连接串经 -e 传入）
docker run --rm --network host --user "$(id -u):$(id -g)" -e RESTORE_DATABASE_URL \
  -v "$PWD/backups:/backups:ro" postgres:16 \
  sh -c 'pg_restore --exit-on-error --single-transaction --no-owner --dbname="$RESTORE_DATABASE_URL" "/backups/$1"' \
  sh investment_YYYYMMDD_HHMMSS.dump
```

**表级备份的恢复**（回退时恢复到原库）：先停**所有**[写库进程](#写库进程)——只停 backend 不够，
xueqiu-collector 是独立的常驻进程，会继续持事务、提交数据，阻塞 `--clean` 的 DROP，或在恢复完成后
把进行中那一轮的结果写回刚恢复的库；过渡期仍在用旧 archiver 定时任务的部署同样先停它。然后加
`--clean --if-exists` 让 pg_restore 先删后建这些表，`--single-transaction` 保证要么全部恢复、要么
原样不动（它只管恢复自身的原子性，挡不住其他写入者，所以停写入者这一步省不掉）：

```bash
docker compose stop backend xueqiu-collector
# 旧 archiver 定时任务若仍在用：crontab -e 注释它的两行，并确认没有残留进程
pg_restore --exit-on-error --single-transaction --clean --if-exists --no-owner \
  --dbname="<本应用的 DATABASE_URL>" backups/investment_tables_YYYYMMDD_HHMMSS.dump
# 验证（alembic_version、核心表行数）通过后再启动；旧 archiver 的两行是否恢复见 5「回退」末尾
docker compose up -d
```

被**不在清单里**的表用外键引用的表无法先删，pg_restore 会报错并整体回滚——说明清单不完整，
按 9.3 用本项目全部表重新规划。

### 9.5 恢复演练

至少每次改动备份流程后做一次：用最新的 `.dump` 恢复到一个新建空库（9.4 整库恢复的命令），然后

```bash
psql "$RESTORE_DATABASE_URL" -c 'select version_num from alembic_version'
psql "$RESTORE_DATABASE_URL" -c 'select count(*) from transactions'
```

版本号应与生产一致、核心表行数合理；演练完删掉这个库。宿主没有 `psql` 时同样用一次性容器：

```bash
docker run --rm --network host -e RESTORE_DATABASE_URL postgres:16 \
  sh -c 'psql "$RESTORE_DATABASE_URL" -c "select version_num from alembic_version"'
```

### 9.6 Excel 导出

Excel 导出需要运行中的服务和 `INVESTMENT_TRACKER_TOKEN`（Bearer token，`POST /api/auth/token`
获取）。HTTPS 默认执行证书及主机名校验；私有 CA 环境设置 `APP_BASE_URL` 和 `APP_CA_CERT`，不能用
跳过 TLS 校验的参数。Excel 只是便于人工查阅的补充导出，不能替代数据库备份。

---

## 10. 常见问题

### 容器内 DNS 失败（`Temporary failure in name resolution`）

宿主跑了透明代理 / TUN 模式的代理软件时，容器经 Docker 内嵌 DNS 转发到宿主解析器的请求可能被
代理吞掉：周期任务（汇率、港交所、行情）与雪球采集全部失败，但数据库与 API 正常。代理恢复即好；
需要容器侧绕过时，给 backend 与 xueqiu-collector 指定一个可直达的 DNS。写在**本地、不入库**的
`docker-compose.override.yml` 里（已在 `.gitignore`；compose 会自动合并同目录的这个文件）：

```yaml
services:
  backend:
    dns:
      - <局域网 DNS，例如路由器地址>
  xueqiu-collector:
    dns:
      - <局域网 DNS，例如路由器地址>
```

然后 `docker compose up -d backend xueqiu-collector`。绕过的只是 DNS；TLS 连接本身若也被代理截断，仍要修代理。

### 美股档案报 EDGAR 403

SEC 拒绝了不合规的 User-Agent。在 `.env` 设置 `EDGAR_USER_AGENT="your-app your-email@example.com"`
（带真实联系方式），`docker compose up -d backend`。

### 港股报表抽取报披露易 504 / 下载超时

披露易（及其 CDN 边缘节点）的瞬时错误。下载有墙钟上限（单份 180 秒、宽限期后最低速率），超限按
瞬时失败处理，不计入确定性失败次数。稍后原样重跑同一条命令即可续跑。

### LLM 输出为空或被截断（日志里 `finish_reason=length`）

推理 token 与输出共享额度，大报表或长分析会把额度吃穿，返回空内容或半截输出（半截内容不会被
当成结果使用，一律报「LLM 输出被截断」失败）。港股报表映射调大 `STATEMENT_MAX_OUTPUT_TOKENS`，
标的分析调大 `SECURITY_ANALYSIS_MAX_OUTPUT_TOKENS`，复盘/财报摘要/观点摘要调大
`LLM_REPORT_MAX_OUTPUT_TOKENS`，然后
`docker compose up -d backend`（`restart` 不会重读环境变量）。

### 雪球采集器反复命中 WAF（`scan_runs` 状态为 `waf`、卡片显示冷却中）

阿里云 WAF 把请求判成了机器流量。采集器会中止本轮并冷却 `XUEQIU_COLLECTOR_WAF_COOLDOWN_SECONDS`
（默认 30 分钟），偶发一次不用管。连续多轮命中时：在浏览器里用同一账号打开雪球完成人机验证，按
[8.1](#81-cookie-更新流程) 重新导出 Cookie 并重启；仍频繁命中就调大
`XUEQIU_COLLECTOR_MIN_DELAY_SECONDS` / `XUEQIU_COLLECTOR_MAX_DELAY_SECONDS`、减少
`XUEQIU_COLLECTOR_MAX_AUTHORS_PER_RUN`，然后 `docker compose up -d xueqiu-collector`。确认没有第二个
采集程序（例如切换后忘了停的旧 archiver 定时任务）在用同一账号并发请求。

### 雪球采集器某位作者一直是 `partial` 或 `error`

- **`error`**：该作者的主页首页就拿不到合法响应——多半是 Cookie 失效（被重定向到登录页）或账号被风控，
  先按 [8.1](#81-cookie-更新流程) 验证/更新 Cookie；所有作者同时 `error` 基本就是 Cookie 问题，只有一位
  `error` 可能是该用户主页已注销或设了权限，核对后在卡片里停用。`error` 不计活性，持续下去观点页会报停摆。
- **`partial`**：主页成功、个别帖子的全文页或评论页失败，失败的帖子下一轮重抓，看 `error_message`
  写的是哪一帖。某些帖子**永久**拿不到（全文页 404、作者关了评论）时，这位作者可能长期停在 `partial`——
  数据仍在流动、仍计活性，不影响观点功能，可以不处理；Uptime Kuma 也按 up 推送。

### 雪球采集器 unhealthy

healthcheck 只看心跳文件：心跳超过 `XUEQIU_COLLECTOR_HEALTH_MAX_AGE_MINUTES`（默认 30 分钟）未刷新。
常见原因是日志目录不可写（心跳文件就在 `/app/logs` 下，按 [4.3](#43-日志目录权限后端容器非-root)
chown）或进程卡死（`docker compose logs xueqiu-collector` 看最后的输出，再 `docker compose restart
xueqiu-collector`）。采集本身失败（Cookie、WAF）不影响健康状态，看采集器卡片。

### 改了 `.env` 不生效

1. 这个变量在不在**对应服务**（backend 或 xueqiu-collector）的 `environment:` 里？不在就不会进
   那个容器（`test_deploy_config_sync.py` 保证 `config.py` 的字段各有去处，但自定义变量不会）。
   采集器专用的 `XUEQIU_COLLECTOR_*` 大多只下传给 xueqiu-collector。
2. 是否用 `docker compose up -d <服务>` 重建了容器？`restart` 不重读环境变量。
3. `docker compose exec <服务> printenv <变量名>` 看容器里的实际值。

### 后端启动失败，提示 relation/users 不存在

数据库表尚未迁移：

```bash
docker compose run --rm backend alembic upgrade head
docker compose up -d
```

### 后端启动/迁移报 PermissionError: '/app/app/...'

宿主检出的源码文件带 600 权限（历史 umask 遗留）且镜像是修复前构建的：容器内 uid 10001 读不了
root 属主的 600 文件。当前 Dockerfile 已在构建期 `chmod -R u+rwX,go+rX /app` 兜底，重新
`docker compose build` 即可；顺手把宿主仓库也归一化，避免用旧镜像时复发：

```bash
chmod -R u+rwX,go+rX backend frontend
```

（仓库里没有单独的 `nginx` 目录，nginx 模板在 `frontend/nginx.conf.template`。）

### 后端启动报日志文件 PermissionError（`/app/logs/...`）

日志目录不归 uid 10001。采集器也写这个目录（日志与心跳文件），两个服务一起处理：
`docker compose stop backend xueqiu-collector`，执行 [4.3](#43-日志目录权限后端容器非-root) 的
chown 命令，再 `docker compose up -d`。

### 数据库连接失败

- `.env` 中 `DATABASE_URL` 是否为 PostgreSQL 连接串；
- 主机、端口、用户名、密码是否正确；
- 部署主机**和容器网络**是否都能访问数据库（容器里的 `localhost` 是容器自己）。

### 前端无法连接后端

- `docker compose logs -f frontend` / `docker compose logs -f backend`；
- `.env` 中 `CORS_ORIGINS` 是否包含浏览器里的实际 origin（非 443 端口要带端口号）；
- 证书路径是否挂载成功、`docker compose ps` 两个容器是否都是 `healthy`。

### 端口冲突

```bash
lsof -i :80
lsof -i :443
```

改 `.env` 的 `FRONTEND_HTTP_PORT` / `FRONTEND_HTTPS_PORT`（同步 `CORS_ORIGINS`）后
`docker compose up -d`。

### Docker 镜像构建失败（其他原因）

```bash
docker compose build --no-cache
docker compose up -d
```

---

## 卸载

仅停止并删除容器：

```bash
docker compose down
```

`down` 会一并停掉 xueqiu-collector。若宿主上还留着旧 xueqiu-timeline-archiver 的定时任务，它不归
compose 管，会继续写数据库——一并在 crontab 里停掉。

如需删除项目文件，请先确认已经备份 PostgreSQL 和 `data/` 中需要保留的原始导入文件。
