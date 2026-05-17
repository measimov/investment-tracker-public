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
DATABASE_URL=postgresql://app_user:<postgres-password>@db.example.local:5432/appdb
CORS_ORIGINS=https://app.example.local,https://app.example.local:443,http://app.example.local,http://app.example.local:80
SECRET_KEY=<openssl-rand-hex-32>
ADMIN_INITIAL_PASSWORD=<strong-admin-initial-password>
DEMO_INITIAL_PASSWORD=<strong-user-initial-password>
TUSHARE_TOKEN=<tushare-api-token>
NGINX_SERVER_NAME=app.example.local
SSL_CERT_FULLCHAIN=/opt/investment-tracker/certs/lan/fullchain.pem
SSL_CERT_PRIVKEY=/opt/investment-tracker/certs/lan/privkey.pem
ENABLE_DOCS=false
REQUIRE_HTTPS=true
PRICE_REFRESH_MAX_WORKERS=4
```

可用以下命令生成 LAN 自签证书：

```bash
mkdir -p certs/lan
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout certs/lan/privkey.pem \
  -out certs/lan/fullchain.pem \
  -days 825 \
  -subj /CN=app.example.local \
  -addext subjectAltName=DNS:app.example.local,DNS:localhost,IP:127.0.0.1
```

## 首次部署

```bash
docker compose build
docker compose run --rm backend alembic upgrade head
docker compose up -d
docker compose ps
```

访问：

- 前端：`https://app.example.local`
- 健康检查：`https://app.example.local/health`

首次启动会初始化 `admin` / `demo` 两个用户；密码来自 `.env`。应用启动不会自动创建数据库表，表结构必须由 Alembic 管理。

旧版文件数据库和自动建表部署路径已经废弃。当前部署以 PostgreSQL + `alembic upgrade head` 为准。

## 群晖 NAS

1. 安装 Container Manager。
2. 上传项目到例如 `/volume1/docker/investment-tracker`。
3. 准备根目录 `.env` 和证书文件。
4. 通过 Container Manager 创建项目，或 SSH 进入目录执行：

```bash
sudo docker compose build
sudo docker compose run --rm backend alembic upgrade head
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

PostgreSQL 备份：

```bash
mkdir -p backups
pg_dump "$DATABASE_URL" > backups/investment_$(date +%Y%m%d_%H%M%S).sql
```

PostgreSQL 恢复：

```bash
psql "$DATABASE_URL" < backups/investment_YYYYMMDD_HHMMSS.sql
```

项目根目录的 `backup.sh` 提供交互式备份入口，可选择 `pg_dump`、登录态 Excel 导出或项目归档。Excel 导出需要运行中的服务和 `INVESTMENT_TRACKER_TOKEN`。

定时任务示例：

```bash
0 2 * * * cd /path/to/investment-tracker && pg_dump "$DATABASE_URL" > backups/investment_$(date +\%Y\%m\%d_\%H\%M\%S).sql
```

## 升级

推荐顺序：

```bash
mkdir -p backups
pg_dump "$DATABASE_URL" > backups/investment_before_upgrade_$(date +%Y%m%d_%H%M%S).sql

git pull
docker compose build
docker compose run --rm backend alembic upgrade head
docker compose up -d
docker compose logs -f
```

确认健康检查：

```bash
curl -k https://app.example.local/health
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
