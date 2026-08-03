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

## 首次部署

```bash
docker compose build
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
