#!/usr/bin/env bash
# 投资追踪系统 - 数据备份脚本
# 用途：生成经过完整读检的 PostgreSQL 备份，并可选导出 Excel

set -Eeuo pipefail
umask 077

# 配置
BACKUP_DIR="${BACKUP_DIR:-./backups}"
DATE=$(date +%Y%m%d_%H%M%S)
APP_BASE_URL="${APP_BASE_URL:-https://localhost}"

# 颜色输出
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${BLUE}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║                                                              ║${NC}"
echo -e "${BLUE}║          📦 投资追踪系统 - 数据备份工具 📦                  ║${NC}"
echo -e "${BLUE}║                                                              ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""

# 创建备份目录
mkdir -p "$BACKUP_DIR"

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
    printf '%s  %s\n' "$checksum" "$final_path" > "$checksum_partial"

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
    if [ -n "${DATABASE_URL:-}" ] \
        && command -v pg_dump >/dev/null 2>&1 \
        && command -v pg_restore >/dev/null 2>&1; then
        echo "local"
        return 0
    fi

    if command -v docker >/dev/null 2>&1 \
        && docker compose exec -T backend sh -c \
            'test -n "${DATABASE_URL:-}" && command -v pg_dump >/dev/null && command -v pg_restore >/dev/null' \
            >/dev/null 2>&1; then
        echo "docker"
        return 0
    fi

    echo -e "${YELLOW}⚠️  无法找到可用的 PostgreSQL 备份客户端:${NC}" >&2
    echo "   请设置 DATABASE_URL 并安装 pg_dump/pg_restore，或先启动 backend 容器。" >&2
    return 1
}

create_postgres_backup() {
    local final_path="$BACKUP_DIR/investment_$DATE.dump"
    local partial_path="${final_path}.partial"
    local tool_mode

    if ! tool_mode=$(select_pg_tool_mode); then
        return 1
    fi

    ensure_new_path "$final_path"

    echo -e "${BLUE}📁 正在同步备份 PostgreSQL 数据库...${NC}"
    if [ "$tool_mode" = "local" ]; then
        if ! pg_dump --format=custom --file="$partial_path" "$DATABASE_URL"; then
            echo -e "${YELLOW}⚠️  pg_dump 失败；未完成文件保留为:${NC} $partial_path" >&2
            return 1
        fi
    elif ! docker compose exec -T backend sh -c \
        'exec pg_dump --format=custom "$DATABASE_URL"' > "$partial_path"; then
        echo -e "${YELLOW}⚠️  pg_dump 失败；未完成文件保留为:${NC} $partial_path" >&2
        return 1
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
    elif ! docker compose exec -T backend \
        pg_restore --exit-on-error --file=/dev/null < "$partial_path"; then
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

    curl --silent --show-error --fail "${curl_tls_args[@]}" \
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
    read -p "请输入选项 [1-3]: " choice
fi

case $choice in
    1)
        create_postgres_backup
        ;;

    2)
        echo -e "${BLUE}📊 正在导出 Excel 文件...${NC}"
        # 检查服务是否运行
        if docker compose ps | grep -q "Up"; then
            export_excel_backup
        else
            echo -e "${YELLOW}⚠️  服务未运行，请先启动: docker compose up -d${NC}" >&2
            exit 1
        fi
        ;;

    3)
        echo -e "${BLUE}💾 正在执行完整备份...${NC}"

        create_postgres_backup

        # 导出 Excel
        if docker compose ps | grep -q "Up"; then
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
BACKUP_COUNT=$(find "$BACKUP_DIR" -maxdepth 1 -type f ! -name '*.partial' | wc -l | tr -d ' ')
if [ "$BACKUP_COUNT" -gt 10 ]; then
    echo ""
    echo -e "${YELLOW}💡 提示: 备份文件较多($BACKUP_COUNT个)，建议清理旧备份:${NC}"
    echo "   请在核对 SHA256 和保留策略后人工清理旧的 .dump/.xlsx 及对应 .sha256 文件。"
fi

echo ""
echo -e "${GREEN}✅ 备份完成！${NC}"
echo ""
