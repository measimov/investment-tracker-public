# 部署指南

本文档覆盖生产/LAN Docker 部署、升级、备份恢复和常见排障。

当前运行数据库统一为 **PostgreSQL**。不要再按 SQLite 文件数据库方式部署或排障；`data/` 目录只保留原始导入文件和历史文件。

## 前置要求

- Docker 和 Docker Compose
- 可访问的 PostgreSQL 实例
- 80/443 端口可用，或在 `docker-compose.yml` 中调整端口映射
- TLS 证书文件，路径通过 `.env` 提供

## 环境变量

从模板创建根目录 `.env`：

```bash
cp .env.example .env
```

保留并填写 Docker 构建/运行必需变量：

```env
DATABASE_URL=postgresql://<db-user>:<db-password>@<db-host>:5432/<db-name>
CORS_ORIGINS=https://<app-host>,https://<app-host>:443
SECRET_KEY=<openssl-rand-hex-32>
ADMIN_INITIAL_PASSWORD=<strong-admin-initial-password>
DEMO_INITIAL_PASSWORD=<strong-user-initial-password>
TUSHARE_TOKEN=
NGINX_SERVER_NAME=<app-host>
SSL_CERT_FULLCHAIN=./certs/lan/fullchain.pem
SSL_CERT_PRIVKEY=./certs/lan/privkey.pem
FRONTEND_HTTP_PORT=80
FRONTEND_HTTPS_PORT=443
BACKEND_LOG_DIR=./backend/logs
NGINX_LOG_DIR=./logs/nginx
APP_BASE_URL=https://<app-host>
ENABLE_DOCS=false
REQUIRE_HTTPS=true
TRUST_PROXY_HEADERS=true
SESSION_ABSOLUTE_MAX_HOURS=168
PRICE_REFRESH_MAX_WORKERS=4
APP_VERSION=1.0.0
BUILD_SHA=unknown

# 后台任务与 AI 复盘（不配置 LLM_REPORT_* 时定期复盘静默不跑）
BACKGROUND_WORKER_ENABLED=true
LLM_REPORT_API_KEY=
LLM_REPORT_BASE_URL=https://api.deepseek.com
LLM_REPORT_MODEL=deepseek-v4-pro
```

其余可调参数（Tushare 限速、价格新鲜度窗口、`BACKGROUND_JOB_*`）见 `.env.example`。

`ENABLE_DOCS` / `REQUIRE_HTTPS` 的**代码默认值就是上面这两个值**（fail-closed），
写在这里只为显式可见，漏配不会退回不安全的一侧。

`TRUST_PROXY_HEADERS` 决定后端是否采信反代请求头：`X-Forwarded-Proto` 判断请求
是不是 HTTPS，`X-Forwarded-For` 取审计日志用的客户端 IP。这两个头客户端都可任意
伪造，只有在**后端端口不直接对外**、且反代确实会覆写/追加它们时才可信——本仓的
nginx 用 `$scheme` 覆写、compose 也不发布 8000，故默认拓扑下置 `true`。若改成直接
暴露后端端口，必须同时改回 `false`，否则一个请求头就能绕过 `REQUIRE_HTTPS`。

这个开关成立的前提是 **uvicorn 自己不处理这些头**：它的 `ProxyHeadersMiddleware`
默认开启且信任 `127.0.0.1`，会抢先把 `scope.scheme` 改写成 `https`，那样应用层的
开关就成了摆设。因此所有启动命令（Dockerfile、开发命令、E2E）都带 `--no-proxy-headers`，
由应用层独占解析；`backend/tests/test_proxy_header_trust.py` 起真实 uvicorn 守着这一点。

`SESSION_ABSOLUTE_MAX_HOURS` 是会话自**首次登录**起的绝对上限。滑动续期共享同一个
jti，只看有效期的话被窃 cookie 按时刷新即可永久续命；到顶即吊销、强制重新登录。
签发与续期的有效期都会被钳到这个截止点之内，普通请求校验也兜底检查它。

可用以下命令生成 LAN 自签证书：

```bash
mkdir -p certs/lan
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout certs/lan/privkey.pem \
  -out certs/lan/fullchain.pem \
  -days 825 \
  -subj /CN=<app-host> \
  -addext subjectAltName=DNS:<app-host>,DNS:localhost,IP:127.0.0.1
```

### 日志目录权限（后端容器已非 root）

后端容器以固定 uid **10001** 运行（#143），挂载的日志目录必须可被其写入，
否则后端启动时日志初始化会因权限被拒直接失败——这是 fail-fast，不是静默
丢日志。

授权命令**让 Compose 自己解析路径**，一次性起个 root 容器 chown 挂载点：

```bash
docker compose run --rm --user root backend chown -R 10001:10001 /app/logs
```

首次部署与升级都是这一条，首次部署也不必先建目录——bind mount 会自动创建
宿主目录（root 所有），这条命令紧接着把它改对。

**不要在宿主上拼 `${BACKEND_LOG_DIR}` 路径**，两个坑：

1. 普通 shell 不读 Compose 的 `.env`（Compose 只在自己执行时读），配了自定义
   路径的部署会授权到默认目录，而 Compose 挂的是另一个；
2. 更不要 `. ./.env`——Compose 的 dotenv 有自己的语法（`VAR: VAL`、`VAR = VAL`、
   自己的引号/转义/插值规则），不保证能被 POSIX shell 解析。本仓还要求口令含
   特殊字符，`&`、`$()`、空格括号会被 shell 分隔、展开甚至执行；而 `set -a`
   导出的错误值随后**以更高优先级覆盖** Compose 自己对 `.env` 的正确解析，
   等于主动把部署改坏。真要在宿主取值只能走 `docker compose config --environment`。

升级路径必须**先停掉旧后端再改权限**（见下方「升级」一节的完整序列），否则
仍在运行的 root 容器会在日志轮转时重新建出 root-owned 文件。

## 首次部署

```bash
docker compose build
docker compose run --rm --user root backend chown -R 10001:10001 /app/logs
docker compose run --rm backend alembic upgrade head
docker compose run --rm backend python manage.py seed
docker compose up -d
docker compose ps
```

`python manage.py rebuild-holdings` 可随时从交易与公司行动全量重放持仓（幂等；
输出 Failures 列表说明存在真实超卖数据，修正后重跑）。

访问：

- 前端：`https://<app-host>`
- 健康检查：`https://<app-host>/health`

首次部署时运行 `python manage.py seed` 初始化 `admin` / `demo` 两个用户；密码来自 `.env`。应用启动不会自动创建数据库表或写入种子数据，表结构必须由 Alembic 管理。

旧版文件数据库和自动建表部署路径已经废弃。当前部署以 PostgreSQL + `alembic upgrade head` 为准。

`TUSHARE_TOKEN` 可留空；此时不能主动从 Tushare 刷新行情，分红公告与基本面档案同步也不可用。
`LLM_REPORT_API_KEY` 留空时 AI 复盘和标的分析接口保持禁用，定期计划不会调用外部模型。
启用后，生成报告、追问和标的分析会把相应的账本或公开行情输入发送给
`LLM_REPORT_BASE_URL` 指向的外部服务；上线前应确认数据范围、供应商条款和隐私要求。

## 群晖 NAS

1. 安装 Container Manager。
2. 上传项目到例如 `/volume1/docker/investment-tracker`。
3. 准备根目录 `.env` 和证书文件。
4. 通过 Container Manager 创建项目，或 SSH 进入目录执行：

```bash
sudo docker compose build
sudo docker compose run --rm --user root backend chown -R 10001:10001 /app/logs
sudo docker compose run --rm backend alembic upgrade head
sudo docker compose run --rm backend python manage.py seed
sudo docker compose up -d
sudo docker compose ps
```

如需改端口，同步调整 `docker-compose.yml` 的 `frontend.ports` 和 `.env` 中的 `CORS_ORIGINS`。

## 日志和运维

```bash
docker compose logs -f
docker compose logs -f backend
docker compose logs -f frontend
docker compose exec backend bash
```

停止服务：

```bash
docker compose down
```

不要把 `data/` 当作主数据库备份目标；PostgreSQL 数据在外部数据库实例中。

## 备份和恢复

使用项目根目录的交互式脚本生成 PostgreSQL 自定义格式备份：

```bash
./backup.sh
# 选择 1；选择 3 时还会导出登录态 Excel

# 非交互式数据库备份
BACKUP_MODE=postgres ./backup.sh
```

脚本会先写入 `.dump.partial`，同步等待 `pg_dump` 成功，再用
`pg_restore --file=/dev/null` 完整读检。只有读检通过才会原子改名为 `.dump`，并生成
配套的 `.sha256` 文件。存在 `.partial` 只表示一次未完成或未通过验证的备份，不能用于恢复。
脚本优先使用本机的 PostgreSQL 客户端和 `DATABASE_URL`；本机未安装客户端时，会使用正在运行的
`backend` 容器及其数据库连接配置，连接串不会写入备份日志。

恢复前先核验 SHA256，并优先恢复到新建的空数据库演练：

```bash
# Linux
sha256sum -c backups/investment_YYYYMMDD_HHMMSS.dump.sha256
# macOS
shasum -a 256 -c backups/investment_YYYYMMDD_HHMMSS.dump.sha256

pg_restore --exit-on-error --no-owner \
  --dbname="$RESTORE_DATABASE_URL" \
  backups/investment_YYYYMMDD_HHMMSS.dump
```

Excel 导出需要运行中的服务和 `INVESTMENT_TRACKER_TOKEN`。HTTPS 默认执行证书及主机名
校验；私有 CA 环境应设置 `APP_BASE_URL` 和 `APP_CA_CERT`，不能用跳过 TLS 校验的参数。
Excel 只是便于人工查阅的补充导出，不能替代包含完整应用数据的 PostgreSQL 备份。

定时任务应使用 `BACKUP_MODE=postgres ./backup.sh`，或实现等价的
“`.partial` → `pg_dump` 退出 0 → `pg_restore` 完整读检 → 原子改名 → SHA256”流程。
不要用后台重定向后立即宣告成功；定时任务应安全加载 `DATABASE_URL`，并以脚本退出码作为成败依据。

## 升级

推荐顺序：

```bash
# 选择 1，并确认脚本退出码为 0、生成 .dump 和 .sha256 后再继续
./backup.sh

git pull
docker compose build

# 从 root 镜像升级到非 root 镜像时的一次性权限迁移（#143）：
# 必须先停掉旧后端再 chown——仍在运行的 root 容器会在日志轮转时重新建出
# root-owned 文件，先 chown 后停机等于白做。路径交给 Compose 解析，
# 不在宿主上拼 ${BACKEND_LOG_DIR}（见上方「日志目录权限」一节）。
docker compose stop backend
docker compose run --rm --user root backend chown -R 10001:10001 /app/logs

docker compose run --rm backend alembic upgrade head
docker compose up -d
docker compose logs -f
```

v1.0 基线（`20260728_0001`）之前的迁移已压缩，**不存在从更早版本的升级路径**：
不在基线上的数据库应重建而非迁移。需要回退时，恢复升级前已读检通过的 `.dump`，
不要在生产库上执行 `alembic downgrade`。注意 `broker_fund_flows` 按券商账户去重，
两个真实账户可以合法拥有相同的 `row_hash`。

确认健康检查：

```bash
curl --cacert "$APP_CA_CERT" https://<app-host>/health
```

## 常见问题

### 后端启动失败，提示 relation/users 不存在

数据库表尚未迁移。执行：

```bash
docker compose run --rm backend alembic upgrade head
docker compose up -d
```

### 数据库连接失败

检查：

- `.env` 中 `DATABASE_URL` 是否为 PostgreSQL 连接串
- PostgreSQL 主机、端口、用户名、密码是否正确
- 部署主机是否能访问 PostgreSQL

### 前端无法连接后端

检查：

- `docker compose logs -f frontend`
- `docker compose logs -f backend`
- `.env` 中 `CORS_ORIGINS` 是否包含访问前端的实际 origin
- Nginx 证书路径是否挂载成功

### 端口冲突

```bash
lsof -i :80
lsof -i :443
```

修改 `docker-compose.yml` 中的端口映射后，重新部署：

```bash
docker compose up -d
```

### Docker 镜像构建失败

```bash
docker compose build --no-cache
docker compose up -d
```

## 卸载

仅停止并删除容器：

```bash
docker compose down
```

如需删除项目文件，请先确认已经备份 PostgreSQL 和 `data/` 中需要保留的原始导入文件。
