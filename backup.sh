#!/bin/bash
# 投资追踪系统 - 数据备份脚本
# 用途：快速备份数据到其他设备

set -e  # 遇到错误立即退出

# 配置
BACKUP_DIR="./backups"
DATE=$(date +%Y%m%d_%H%M%S)
PROJECT_DIR="."
APP_BASE_URL="${APP_BASE_URL:-https://app.example.local}"

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
mkdir -p $BACKUP_DIR

export_excel_backup() {
    if [ -z "$INVESTMENT_TRACKER_TOKEN" ]; then
        echo -e "${YELLOW}⚠️  Excel 导出需要设置 INVESTMENT_TRACKER_TOKEN${NC}"
        echo "   建议优先使用 PostgreSQL 备份，或登录后提供 Bearer token。"
        return 1
    fi

    curl -ks -f -H "Authorization: Bearer $INVESTMENT_TRACKER_TOKEN" \
        -o "$BACKUP_DIR/transactions_$DATE.xlsx" \
        "$APP_BASE_URL/api/export/excel"
}

# 备份选项
echo -e "${YELLOW}请选择备份方式:${NC}"
echo "1) 备份 PostgreSQL 数据库 (pg_dump)"
echo "2) 导出 Excel 文件"
echo "3) 完整备份 (数据库 + Excel)"
echo ""
read -p "请输入选项 [1-3]: " choice

case $choice in
    1)
        echo -e "${BLUE}📁 正在备份 PostgreSQL 数据库...${NC}"
        if [ -n "$DATABASE_URL" ]; then
            pg_dump "$DATABASE_URL" > "$BACKUP_DIR/investment_$DATE.sql"
            echo -e "${GREEN}✅ 数据库备份完成:${NC}"
            echo "   $BACKUP_DIR/investment_$DATE.sql"
        else
            echo -e "${YELLOW}⚠️  请先在环境中设置 DATABASE_URL${NC}"
        fi
        ;;

    2)
        echo -e "${BLUE}📊 正在导出 Excel 文件...${NC}"
        # 检查服务是否运行
        if docker compose ps | grep -q "Up"; then
            export_excel_backup

            if [ -f "$BACKUP_DIR/transactions_$DATE.xlsx" ]; then
                echo -e "${GREEN}✅ Excel 导出完成:${NC}"
                echo "   $BACKUP_DIR/transactions_$DATE.xlsx"
            else
                echo -e "${YELLOW}⚠️  Excel 导出失败，请确保服务正在运行${NC}"
            fi
        else
            echo -e "${YELLOW}⚠️  服务未运行，请先启动: docker compose up -d${NC}"
        fi
        ;;

    3)
        echo -e "${BLUE}💾 正在执行完整备份...${NC}"

        # 备份数据库
        if [ -n "$DATABASE_URL" ]; then
            pg_dump "$DATABASE_URL" > "$BACKUP_DIR/investment_$DATE.sql"
            echo -e "${GREEN}✅ 数据库备份完成${NC}"
        else
            echo -e "${YELLOW}⚠️  请先在环境中设置 DATABASE_URL${NC}"
        fi

        # 导出 Excel
        if docker compose ps | grep -q "Up"; then
            if export_excel_backup; then
                echo -e "${GREEN}✅ Excel 导出完成${NC}"
            fi
        fi

        echo -e "${GREEN}✅ 完整备份完成！${NC}"
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
ls -lh $BACKUP_DIR/ | tail -5

# 统计
BACKUP_SIZE=$(du -sh $BACKUP_DIR | cut -f1)
echo ""
echo -e "${GREEN}📊 备份统计:${NC}"
echo "   备份目录: $BACKUP_DIR"
echo "   占用空间: $BACKUP_SIZE"
echo "   备份时间: $(date)"

# 清理建议
BACKUP_COUNT=$(ls $BACKUP_DIR | wc -l)
if [ $BACKUP_COUNT -gt 10 ]; then
    echo ""
    echo -e "${YELLOW}💡 提示: 备份文件较多($BACKUP_COUNT个)，建议清理旧备份:${NC}"
    echo "   find $BACKUP_DIR -name '*.sql' -mtime +30 -delete"
fi

echo ""
echo -e "${GREEN}✅ 备份完成！${NC}"
echo ""
