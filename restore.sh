#!/bin/bash
# 投资追踪系统 - 数据恢复脚本
# 用途：从备份恢复数据

set -e

# 颜色输出
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║                                                              ║${NC}"
echo -e "${BLUE}║          🔄 投资追踪系统 - 数据恢复工具 🔄                  ║${NC}"
echo -e "${BLUE}║                                                              ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""

# 检查备份目录
BACKUP_DIR="./backups"

if [ ! -d "$BACKUP_DIR" ]; then
    echo -e "${RED}❌ 备份目录不存在: $BACKUP_DIR${NC}"
    echo ""
    echo "请先运行备份脚本: ./backup.sh"
    exit 1
fi

# 列出可用的备份
echo -e "${YELLOW}📂 可用的备份文件:${NC}"
echo ""

DB_FILES=$(ls -t $BACKUP_DIR/*.sql 2>/dev/null || echo "")
EXCEL_FILES=$(ls -t $BACKUP_DIR/*.xlsx 2>/dev/null || echo "")

if [ -n "$DB_FILES" ]; then
    echo -e "${BLUE}数据库备份:${NC}"
    ls -lh $BACKUP_DIR/*.sql | awk '{print "  " NR". " $9 " (" $5 ", " $6 " " $7 " " $8 ")"}'
    echo ""
fi

if [ -n "$EXCEL_FILES" ]; then
    echo -e "${BLUE}Excel 备份:${NC}"
    ls -lh $BACKUP_DIR/*.xlsx | awk '{print "  " NR". " $9 " (" $5 ", " $6 " " $7 " " $8 ")"}'
    echo ""
fi

# 恢复选项
echo -e "${YELLOW}请选择恢复方式:${NC}"
echo "1) 从 PostgreSQL SQL 备份恢复"
echo "2) 从 Excel 文件导入"
echo "3) 取消"
echo ""
read -p "请输入选项 [1-3]: " choice

case $choice in
    1)
        # 从数据库恢复
        if [ -z "$DB_FILES" ]; then
            echo -e "${RED}❌ 没有可用的数据库备份${NC}"
            exit 1
        fi

        echo ""
        echo -e "${YELLOW}请输入要恢复的 SQL 备份路径:${NC}"
        read -p "文件路径: " db_file

        if [ ! -f "$db_file" ]; then
            echo -e "${RED}❌ 文件不存在: $db_file${NC}"
            exit 1
        fi

        echo ""
        echo -e "${RED}⚠️  警告: 这将覆盖现有数据！${NC}"
        read -p "确认恢复? (yes/no): " confirm

        if [ "$confirm" = "yes" ]; then
            if [ -z "$DATABASE_URL" ]; then
                echo -e "${RED}❌ 请先在环境中设置 DATABASE_URL${NC}"
                exit 1
            fi

            # 停止服务
            echo -e "${BLUE}🛑 正在停止服务...${NC}"
            docker compose down

            # 恢复数据
            psql "$DATABASE_URL" < "$db_file"
            echo -e "${GREEN}✅ 数据库已恢复${NC}"

            # 重启服务
            echo -e "${BLUE}🚀 正在重启服务...${NC}"
            docker compose up -d

            echo ""
            echo -e "${GREEN}✅ 恢复完成！${NC}"
            echo "   访问: https://app.example.local"
        else
            echo -e "${YELLOW}❌ 已取消恢复${NC}"
        fi
        ;;

    2)
        # 从 Excel 导入
        echo ""
        echo -e "${YELLOW}Excel 导入步骤:${NC}"
        echo "1. 访问: https://app.example.local"
        echo "2. 点击 '交易记录' 菜单"
        echo "3. 点击 '导入' 按钮"
        echo "4. 选择 Excel 备份文件"
        echo ""
        echo -e "${BLUE}💡 提示: 如果要替换所有数据，请先清空数据库${NC}"
        echo ""
        read -p "是否清空现有数据? (yes/no): " clear_data

        if [ "$clear_data" = "yes" ]; then
            docker compose down
            if [ -z "$DATABASE_URL" ]; then
                echo -e "${RED}❌ 请先在环境中设置 DATABASE_URL${NC}"
                exit 1
            fi
            psql "$DATABASE_URL" -c "TRUNCATE TABLE corporate_actions, transactions, holdings, exchange_rates RESTART IDENTITY CASCADE;"
            docker compose up -d
            echo -e "${GREEN}✅ 数据已清空，请通过 Web 界面导入 Excel${NC}"
        fi

        echo ""
        echo -e "${YELLOW}请通过浏览器完成导入操作${NC}"
        ;;

    3)
        echo -e "${YELLOW}已取消${NC}"
        exit 0
        ;;

    *)
        echo -e "${RED}❌ 无效的选项${NC}"
        exit 1
        ;;
esac

echo ""
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo ""
