#!/usr/bin/env bash
# 投资追踪系统 - 数据备份脚本
# 用途：生成经过完整读检的 PostgreSQL 备份，并可选导出 Excel；按保留策略清理旧备份
#
# 用法：
#   ./backup.sh                                  交互选择
#   BACKUP_MODE=postgres ./backup.sh             非交互数据库备份（定时任务用这个）
#   ./backup.sh --prune [--keep N] [--dry-run]   只清理：保留最新 N 份完整备份（默认 2）
#   BACKUP_MODE=postgres ./backup.sh --prune     先备份，成功后再清理
#   BACKUP_TABLES="t1 t2 alembic_version" BACKUP_MODE=postgres ./backup.sh
#                                                表级备份 → investment_tables_<时间>.dump
#   BACKUP_NOTIFY=1 BACKUP_MODE=postgres ./backup.sh
#                                                失败时推送告警 `backup`，成功时自动恢复
#
# 数据库备份流程（本机客户端与容器客户端同一套纪律）：
#   写 .dump.partial → pg_dump 退出 0 → 文件非空 → pg_restore --file=/dev/null 完整读检
#   → 计算 SHA256（.sha256 里记裸文件名，可在备份目录内 sha256sum -c）→ 原子改名为 .dump
#
# 客户端选择（BACKUP_PG_TOOL=local|docker 可强制）：
#   local  = 宿主机上的 pg_dump/pg_restore（主版本须 ≥ 数据库主版本；未强制 local 时，
#            pg_dump 报 server version mismatch 会自动改用 docker 重跑）
#   docker = 一次性 `docker run --rm $BACKUP_PG_IMAGE`（默认 postgres:16）；backend 镜像
#            刻意不含 pg_dump，所以不走 backend 容器
# DATABASE_URL：优先取当前 shell 环境；没有则向 compose 的 backend 服务要（运行中用
# exec，已停止用一次性 run）。脚本**不读 .env**——Compose 的 dotenv 语法不保证能被
# shell 正确解析。

set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 配置
BACKUP_DIR="${BACKUP_DIR:-./backups}"
DATE=$(date +%Y%m%d_%H%M%S)
APP_BASE_URL="${APP_BASE_URL:-https://localhost}"
BACKUP_PG_IMAGE="${BACKUP_PG_IMAGE:-postgres:16}"
BACKUP_DOCKER_NETWORK="${BACKUP_DOCKER_NETWORK:-host}"
# 表级备份：空格分隔的表名（不带 schema 时按 public），为空 = 整库
BACKUP_TABLES="${BACKUP_TABLES:-}"

# 完整备份与表级备份的文件名（清理只认这两种形状，其余文件一概不碰）
FULL_DUMP_RE='^investment_[0-9]{8}_[0-9]{6}\.dump$'
TABLE_DUMP_RE='^investment_tables_[0-9]{8}_[0-9]{6}\.dump$'
PARTIAL_RE='^investment_(tables_)?[0-9]{8}_[0-9]{6}\.dump(\.sha256)?\.partial$'

# 颜色输出
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

usage() {
    cat <<'EOF'
用法: ./backup.sh [--prune [--keep N] [--include-partial] [--dry-run]]

  (无参数)            交互选择备份方式；设置 BACKUP_MODE=postgres|excel|full 则非交互
  --prune             清理旧备份：只保留最新 N 份完整备份 investment_YYYYMMDD_HHMMSS.dump
                      （连同同名 .sha256）；与 BACKUP_MODE 同用时先备份、成功后再清理
  --keep N            保留份数，默认 2，至少 1
  --include-partial   同时清理表级备份 investment_tables_*.dump（同样保留最新 N 份），
                      以及早于最新完整备份的未完成 .partial 残留
  --dry-run           只打印将删除的文件，不删除
  -h, --help          显示本帮助

环境变量（都在 shell 中设置，脚本不读 .env）：
  BACKUP_DIR              备份目录，默认 ./backups
  BACKUP_MODE             postgres | excel | full；不设则交互选择
  BACKUP_TABLES           空格分隔的表名 → 表级备份 investment_tables_<时间>.dump
  BACKUP_PG_TOOL          local | docker；不设则本机有 pg_dump 用本机，否则用容器；
                          本机客户端主版本低于数据库时自动改用容器
  BACKUP_PG_IMAGE         容器模式的镜像，默认 postgres:16（主版本须 ≥ 数据库主版本）
  BACKUP_DOCKER_NETWORK   容器模式的网络，默认 host
  DATABASE_URL            不设则向 compose 的 backend 服务读取
  APP_BASE_URL / APP_CA_CERT / INVESTMENT_TRACKER_TOKEN   Excel 导出用
  BACKUP_NOTIFY           设为 1：备份失败时经 backend 容器推送告警 `backup`（Bark 等，见
                          DEPLOYMENT.md「告警通知」），成功时标记恢复；尽力而为，不影响退出码
EOF
}

PRUNE=0
KEEP=2
KEEP_GIVEN=0
INCLUDE_PARTIAL=0
DRY_RUN=0
while [ $# -gt 0 ]; do
    case "$1" in
        --prune) PRUNE=1 ;;
        --keep)
            if [ $# -lt 2 ]; then
                echo "--keep 需要一个数字" >&2
                exit 2
            fi
            KEEP="$2"
            KEEP_GIVEN=1
            shift
            ;;
        --keep=*) KEEP="${1#--keep=}"; KEEP_GIVEN=1 ;;
        --include-partial) INCLUDE_PARTIAL=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "未知参数: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [ "$PRUNE" -eq 0 ] && { [ "$KEEP_GIVEN" -eq 1 ] || [ "$INCLUDE_PARTIAL" -eq 1 ] || [ "$DRY_RUN" -eq 1 ]; }; then
    echo "--keep / --include-partial / --dry-run 只能与 --prune 一起使用" >&2
    exit 2
fi
case "$KEEP" in
    ''|*[!0-9]*)
        echo "--keep 必须是正整数: $KEEP" >&2
        exit 2
        ;;
esac
if [ "$KEEP" -lt 1 ]; then
    echo "--keep 至少为 1（不允许清掉全部备份）" >&2
    exit 2
fi

# docker compose（v2 插件）与 docker-compose（独立二进制）都支持；首次用到时才探测
COMPOSE_CMD=""
compose() {
    if [ -z "$COMPOSE_CMD" ]; then
        if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
            COMPOSE_CMD="docker compose"
        elif command -v docker-compose >/dev/null 2>&1; then
            COMPOSE_CMD="docker-compose"
        else
            echo -e "${YELLOW}⚠️  找不到 docker compose 或 docker-compose${NC}" >&2
            return 1
        fi
    fi
    # 在仓库根目录执行，Compose 才能找到 docker-compose.yml 与 .env
    (cd "$SCRIPT_DIR" && $COMPOSE_CMD "$@")
}

# 备份结果告警（可选，BACKUP_NOTIFY=1）：失败 → 告警 `backup`（critical），成功 → 恢复。
# 经运行中的 backend 容器调 `manage.py notify`，与周期告警同一套状态机（持续失败不刷屏、
# 每 NOTIFY_REMINDER_HOURS 提醒一次）。**尽力而为**：compose 不可用、backend 未运行、
# 推送失败都只打印一行提示，绝不改变备份本身的退出码。
BACKUP_NOTIFY="${BACKUP_NOTIFY:-0}"
NOTIFY_ARMED=0

notify_backup_result() {
    local exit_code="$1"
    local timeout_cmd=()
    local args=()

    if [ "$exit_code" -eq 0 ]; then
        args=(notify --resolve --key backup)
    else
        args=(notify --key backup --severity critical --title "数据库备份失败"
            --message "backup.sh（BACKUP_MODE=${BACKUP_MODE:-交互}）退出码 $exit_code，$(date '+%Y-%m-%d %H:%M:%S')；详见宿主机上的备份日志")
    fi
    if command -v timeout >/dev/null 2>&1; then
        timeout_cmd=(timeout 120)
    fi
    # compose() 首次调用时探测 docker compose / docker-compose 并记进 COMPOSE_CMD
    if ! compose version >/dev/null 2>&1; then
        echo -e "${YELLOW}⚠️  找不到 docker compose，告警通知未发送${NC}" >&2
        return 0
    fi
    # shellcheck disable=SC2086 # COMPOSE_CMD 是 "docker compose" 这样的两个词，刻意分词
    if ! (cd "$SCRIPT_DIR" && ${timeout_cmd[@]+"${timeout_cmd[@]}"} $COMPOSE_CMD \
        exec -T backend python manage.py "${args[@]}") >/dev/null 2>&1; then
        echo -e "${YELLOW}⚠️  告警通知未送达（backend 容器不可用？）；备份结果以本脚本退出码为准${NC}" >&2
    fi
}

on_exit() {
    local exit_code=$?
    trap - EXIT
    if [ "$BACKUP_NOTIFY" = "1" ] && [ "$NOTIFY_ARMED" = "1" ]; then
        notify_backup_result "$exit_code" || true
    fi
    exit "$exit_code"
}
trap on_exit EXIT

sha256_file() {
    local path="$1"

    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$path"
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$path"
    else
        echo -e "${YELLOW}⚠️  找不到 sha256sum 或 shasum，无法生成校验文件${NC}" >&2
        return 1
    fi
}

finalize_with_sha256() {
    local partial_path="$1"
    local final_path="$2"
    local checksum_path="${final_path}.sha256"
    local checksum_partial="${checksum_path}.partial"
    local checksum

    if ! checksum=$(sha256_file "$partial_path" | cut -d ' ' -f 1); then
        echo -e "${YELLOW}⚠️  无法计算 SHA256；未完成文件保留为:${NC} $partial_path" >&2
        return 1
    fi
    if [ -z "$checksum" ]; then
        echo -e "${YELLOW}⚠️  无法计算 SHA256；未完成文件保留为:${NC} $partial_path" >&2
        return 1
    fi
    # 记裸文件名：校验文件与备份同目录，`cd <备份目录> && sha256sum -c x.sha256`
    # 在任何机器、任何挂载路径下都成立（记相对/绝对路径则换个目录就对不上）
    printf '%s  %s\n' "$checksum" "$(basename "$final_path")" > "$checksum_partial"

    if ! mv "$checksum_partial" "$checksum_path"; then
        echo -e "${YELLOW}⚠️  无法完成校验文件改名；备份仍保留为:${NC} $partial_path" >&2
        return 1
    fi
    if ! mv "$partial_path" "$final_path"; then
        if ! mv "$checksum_path" "$checksum_partial"; then
            echo -e "${YELLOW}⚠️  备份改名失败且无法自动退回校验文件；请保留现场人工核验${NC}" >&2
            return 1
        fi
        echo -e "${YELLOW}⚠️  无法完成备份改名；文件仍处于未完成状态:${NC} $partial_path" >&2
        return 1
    fi

    echo -e "${GREEN}   SHA256:${NC} $checksum"
    echo "   校验文件: $checksum_path"
}

ensure_new_path() {
    local final_path="$1"
    local partial_path="${final_path}.partial"

    if [ -e "$final_path" ] \
        || [ -e "$partial_path" ] \
        || [ -e "${final_path}.sha256" ] \
        || [ -e "${final_path}.sha256.partial" ]; then
        echo -e "${YELLOW}⚠️  备份目标已存在，拒绝覆盖:${NC} $final_path" >&2
        return 1
    fi
}

select_pg_tool_mode() {
    case "${BACKUP_PG_TOOL:-}" in
        local|docker)
            echo "$BACKUP_PG_TOOL"
            return 0
            ;;
        '') ;;
        *)
            echo -e "${YELLOW}⚠️  BACKUP_PG_TOOL 必须是 local 或 docker${NC}" >&2
            return 1
            ;;
    esac

    if command -v pg_dump >/dev/null 2>&1 && command -v pg_restore >/dev/null 2>&1; then
        echo "local"
        return 0
    fi
    if command -v docker >/dev/null 2>&1; then
        echo "docker"
        return 0
    fi

    echo -e "${YELLOW}⚠️  无法找到可用的 PostgreSQL 备份客户端:${NC}" >&2
    echo "   请安装 pg_dump/pg_restore，或安装 docker 以使用一次性 $BACKUP_PG_IMAGE 容器。" >&2
    return 1
}

resolve_database_url() {
    local url=""

    if [ -n "${DATABASE_URL:-}" ]; then
        printf '%s' "$DATABASE_URL"
        return 0
    fi
    # 运行中的 backend 容器最可靠：它的环境就是 compose 实际下传的值
    url=$(compose exec -T backend printenv DATABASE_URL 2>/dev/null) || url=""
    if [ -z "$url" ]; then
        # backend 已停止（例如升级流程里）：用一次性容器取同一份插值结果
        url=$(compose run --rm --no-deps -T backend printenv DATABASE_URL 2>/dev/null) || url=""
    fi
    url="${url%$'\r'}"
    if [ -z "$url" ]; then
        echo -e "${YELLOW}⚠️  取不到 DATABASE_URL:${NC} 请在 shell 中设置，或确认 compose 的 backend 服务可用" >&2
        return 1
    fi
    printf '%s' "$url"
}

create_postgres_backup() {
    local final_path="$BACKUP_DIR/investment_$DATE.dump"
    local partial_path
    local final_name
    local tool_mode
    local db_url
    local abs_dir
    local table
    local err_file
    local user_args=()
    local table_args=()

    if [ -n "$BACKUP_TABLES" ]; then
        for table in $BACKUP_TABLES; do
            if ! [[ "$table" =~ ^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$ ]]; then
                echo -e "${YELLOW}⚠️  BACKUP_TABLES 里的表名不合法:${NC} $table" >&2
                return 1
            fi
            case "$table" in
                *.*) ;;
                *) table="public.$table" ;;
            esac
            table_args+=("--table=$table")
        done
        final_path="$BACKUP_DIR/investment_tables_$DATE.dump"
        echo -e "${BLUE}📋 表级备份（${#table_args[@]} 张表）:${NC} $BACKUP_TABLES"
    fi
    partial_path="${final_path}.partial"
    final_name="$(basename "$final_path")"

    if ! tool_mode=$(select_pg_tool_mode); then
        return 1
    fi
    if ! db_url=$(resolve_database_url); then
        return 1
    fi

    ensure_new_path "$final_path"

    echo -e "${BLUE}📁 正在同步备份 PostgreSQL 数据库（客户端: $tool_mode）...${NC}"
    if [ "$tool_mode" = "local" ]; then
        err_file="$(mktemp)"
        if pg_dump --format=custom --file="$partial_path" \
            ${table_args[@]+"${table_args[@]}"} "$db_url" 2>"$err_file"; then
            cat "$err_file" >&2
        else
            cat "$err_file" >&2
            # 宿主客户端主版本低于数据库时 pg_dump 直接拒跑（不产生可用内容）。没有强制
            # BACKUP_PG_TOOL=local 且有 docker 时，改用一次性容器重跑一遍，而不是让运维
            # 读完报错再手工加 BACKUP_PG_TOOL=docker
            if [ -z "${BACKUP_PG_TOOL:-}" ] \
                && grep -q "server version mismatch" "$err_file" \
                && command -v docker >/dev/null 2>&1; then
                echo -e "${YELLOW}⚠️  本机 pg_dump 主版本低于数据库，改用一次性 $BACKUP_PG_IMAGE 容器重试${NC}" >&2
                # 本次运行刚建的 .partial（ensure_new_path 保证此前不存在），不含可用内容
                rm -f "$partial_path"
                tool_mode="docker"
            else
                rm -f "$err_file"
                echo -e "${YELLOW}⚠️  pg_dump 失败；未完成文件保留为:${NC} $partial_path" >&2
                return 1
            fi
        fi
        rm -f "$err_file"
    fi
    if [ "$tool_mode" = "docker" ]; then
        abs_dir="$(cd "$BACKUP_DIR" && pwd)"
        # 以当前宿主用户写文件，产物不是 root 属主；连接串经环境变量传入，不出现在
        # 进程参数里（ps 看得到参数）
        user_args=(--user "$(id -u):$(id -g)")
        if ! DATABASE_URL="$db_url" docker run --rm "${user_args[@]}" \
            --network "$BACKUP_DOCKER_NETWORK" \
            -e DATABASE_URL \
            -v "$abs_dir:/backups" \
            "$BACKUP_PG_IMAGE" \
            sh -c 'umask 077 && out="$1" && shift && exec pg_dump --format=custom --file="/backups/$out" "$@" "$DATABASE_URL"' \
            sh "${final_name}.partial" ${table_args[@]+"${table_args[@]}"}; then
            echo -e "${YELLOW}⚠️  pg_dump 失败；未完成文件保留为:${NC} $partial_path" >&2
            return 1
        fi
    fi
    if [ ! -s "$partial_path" ]; then
        echo -e "${YELLOW}⚠️  pg_dump 未生成有效内容；文件保留为:${NC} $partial_path" >&2
        return 1
    fi

    echo -e "${BLUE}🔎 正在用 pg_restore 完整读检备份...${NC}"
    if [ "$tool_mode" = "local" ]; then
        if ! pg_restore --exit-on-error --file=/dev/null "$partial_path"; then
            echo -e "${YELLOW}⚠️  备份读检失败；未完成文件保留为:${NC} $partial_path" >&2
            return 1
        fi
    elif ! docker run --rm "${user_args[@]}" \
        -v "$abs_dir:/backups:ro" \
        "$BACKUP_PG_IMAGE" \
        pg_restore --exit-on-error --file=/dev/null "/backups/${final_name}.partial"; then
        # 读挂载的文件而不是喂 stdin：pipe 喂进去的自定义格式没有可寻址的 TOC 偏移
        echo -e "${YELLOW}⚠️  备份读检失败；未完成文件保留为:${NC} $partial_path" >&2
        return 1
    fi

    finalize_with_sha256 "$partial_path" "$final_path"
    echo -e "${GREEN}✅ 数据库备份并验证完成:${NC}"
    echo "   $final_path"
}

export_excel_backup() {
    local final_path="$BACKUP_DIR/transactions_$DATE.xlsx"
    local partial_path="${final_path}.partial"

    if [ -z "${INVESTMENT_TRACKER_TOKEN:-}" ]; then
        echo -e "${YELLOW}⚠️  Excel 导出需要设置 INVESTMENT_TRACKER_TOKEN${NC}"
        echo "   建议优先使用 PostgreSQL 备份，或登录后提供 Bearer token。"
        return 1
    fi

    ensure_new_path "$final_path"
    local curl_tls_args=()
    local ca_cert="${APP_CA_CERT:-${SSL_CERT_FULLCHAIN:-}}"
    if [ -n "$ca_cert" ]; then
        if [ ! -r "$ca_cert" ]; then
            echo -e "${YELLOW}⚠️  APP_CA_CERT/SSL_CERT_FULLCHAIN 不可读:${NC} $ca_cert" >&2
            return 1
        fi
        curl_tls_args+=(--cacert "$ca_cert")
    fi

    curl --silent --show-error --fail ${curl_tls_args[@]+"${curl_tls_args[@]}"} \
        -H "Authorization: Bearer $INVESTMENT_TRACKER_TOKEN" \
        -o "$partial_path" \
        "$APP_BASE_URL/api/export/excel"

    if [ ! -s "$partial_path" ]; then
        echo -e "${YELLOW}⚠️  Excel 导出为空；文件保留为:${NC} $partial_path" >&2
        return 1
    fi

    finalize_with_sha256 "$partial_path" "$final_path"
    echo -e "${GREEN}✅ Excel 导出完成:${NC}"
    echo "   $final_path"
}

services_running() {
    compose ps 2>/dev/null | grep -Eq "Up|running"
}

# 备份目录里匹配某个文件名形状的文件，最新在前（文件名里的时间戳可直接字典序比较）
list_matching() {
    find "$BACKUP_DIR" -maxdepth 1 -type f -print | sed 's#.*/##' | grep -E "$1" | sort -r || true
}

remove_file() {
    local name="$1"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "   将删除: $name"
    else
        rm -f -- "$BACKUP_DIR/$name"
        echo "   已删除: $name"
    fi
}

# 保留最新 KEEP 份，删除其余的 dump 及其同名 .sha256
prune_group() {
    local regex="$1"
    local label="$2"
    local count=0
    local name

    echo -e "${BLUE}🧹 $label：保留最新 $KEEP 份${NC}"
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        count=$((count + 1))
        if [ "$count" -le "$KEEP" ]; then
            echo "   保留: $name"
            continue
        fi
        remove_file "$name"
        if [ -e "$BACKUP_DIR/$name.sha256" ]; then
            remove_file "$name.sha256"
        fi
    done <<EOF
$(list_matching "$regex")
EOF
    if [ "$count" -eq 0 ]; then
        echo "   （没有匹配的文件）"
    fi
}

partial_timestamp() {
    # investment_[tables_]YYYYMMDD_HHMMSS.dump[.sha256].partial → YYYYMMDD_HHMMSS
    echo "$1" | sed -E 's/^investment_(tables_)?([0-9]{8}_[0-9]{6})\..*$/\2/'
}

prune_partials() {
    local newest_full
    local newest_ts
    local name

    newest_full=$(list_matching "$FULL_DUMP_RE" | head -n 1)
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        if [ "$INCLUDE_PARTIAL" -eq 1 ] && [ -n "$newest_full" ]; then
            newest_ts=$(partial_timestamp "$newest_full")
            # 只清理比最新完整备份更早开始的残留：更晚的可能是正在进行中的备份
            if [[ "$(partial_timestamp "$name")" < "$newest_ts" ]]; then
                remove_file "$name"
                continue
            fi
        fi
        echo -e "   ${YELLOW}未完成残留（未删除，核查后可加 --include-partial）:${NC} $name"
    done <<EOF
$(list_matching "$PARTIAL_RE")
EOF
}

prune_backups() {
    if [ ! -d "$BACKUP_DIR" ]; then
        echo -e "${YELLOW}⚠️  备份目录不存在:${NC} $BACKUP_DIR" >&2
        return 1
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        echo -e "${YELLOW}（--dry-run：只列出，不删除）${NC}"
    fi
    prune_group "$FULL_DUMP_RE" "完整备份 investment_*.dump"
    if [ "$INCLUDE_PARTIAL" -eq 1 ]; then
        prune_group "$TABLE_DUMP_RE" "表级备份 investment_tables_*.dump"
    fi
    prune_partials
}

# 只清理：不进备份菜单，不打横幅以外的统计
if [ "$PRUNE" -eq 1 ] && [ -z "${BACKUP_MODE:-}" ]; then
    prune_backups
    exit 0
fi

echo -e "${BLUE}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║                                                              ║${NC}"
echo -e "${BLUE}║          📦 投资追踪系统 - 数据备份工具 📦                  ║${NC}"
echo -e "${BLUE}║                                                              ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""

# 从这里起的退出（成功或失败）才算一次备份结果：只清理、--help、参数错误不告警
NOTIFY_ARMED=1

# 创建备份目录
mkdir -p "$BACKUP_DIR"

# 备份选项
if [ -n "${BACKUP_MODE:-}" ]; then
    case "$BACKUP_MODE" in
        postgres|database|1) choice=1 ;;
        excel|2) choice=2 ;;
        full|3) choice=3 ;;
        *)
            echo -e "${YELLOW}⚠️  BACKUP_MODE 必须是 postgres、excel 或 full${NC}" >&2
            exit 1
            ;;
    esac
else
    echo -e "${YELLOW}请选择备份方式:${NC}"
    echo "1) 备份 PostgreSQL 数据库 (pg_dump)"
    echo "2) 导出 Excel 文件"
    echo "3) 完整备份 (数据库 + Excel)"
    echo ""
    read -r -p "请输入选项 [1-3]: " choice
fi

case $choice in
    1)
        create_postgres_backup
        ;;

    2)
        echo -e "${BLUE}📊 正在导出 Excel 文件...${NC}"
        # 检查服务是否运行
        if services_running; then
            export_excel_backup
        else
            echo -e "${YELLOW}⚠️  服务未运行，请先启动: docker compose up -d（或 docker-compose up -d）${NC}" >&2
            exit 1
        fi
        ;;

    3)
        echo -e "${BLUE}💾 正在执行完整备份...${NC}"

        create_postgres_backup

        # 导出 Excel
        if services_running; then
            export_excel_backup
        else
            echo -e "${YELLOW}⚠️  服务未运行，完整备份未完成${NC}" >&2
            exit 1
        fi

        echo -e "${GREEN}✅ 数据库与 Excel 均备份完成！${NC}"
        ;;

    *)
        echo -e "${YELLOW}⚠️  无效的选项${NC}"
        exit 1
        ;;
esac

if [ "$PRUNE" -eq 1 ]; then
    echo ""
    prune_backups
fi

echo ""
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"

# 显示备份文件列表
echo -e "${YELLOW}📂 备份文件列表:${NC}"
ls -lh "$BACKUP_DIR/" | tail -5

# 统计
BACKUP_SIZE=$(du -sh "$BACKUP_DIR" | cut -f1)
echo ""
echo -e "${GREEN}📊 备份统计:${NC}"
echo "   备份目录: $BACKUP_DIR"
echo "   占用空间: $BACKUP_SIZE"
echo "   备份时间: $(date)"

# 清理建议
FULL_COUNT=$(list_matching "$FULL_DUMP_RE" | grep -c . || true)
if [ "$PRUNE" -eq 0 ] && [ "${FULL_COUNT:-0}" -gt 2 ]; then
    echo ""
    echo -e "${YELLOW}💡 提示: 已有 $FULL_COUNT 份完整备份，可按保留策略清理:${NC}"
    echo "   ./backup.sh --prune --keep 2 --dry-run   # 先看会删什么"
    echo "   ./backup.sh --prune --keep 2"
fi

echo ""
echo -e "${GREEN}✅ 备份完成！${NC}"
echo ""
