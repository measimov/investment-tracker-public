#!/usr/bin/env python3
"""
密码重置脚本
从环境变量读取密码并更新数据库中的用户密码
"""

from app.config import settings
from app.core.security import get_password_hash
from sqlalchemy import create_engine, text

def main():
    print("=" * 60)
    print("密码重置工具")
    print("=" * 60)
    print()

    # 创建数据库连接
    engine = create_engine(settings.database_url)

    with engine.connect() as conn:
        # 检查用户是否存在
        result = conn.execute(text("SELECT username FROM users WHERE username IN ('admin', 'demo')"))
        users = [row[0] for row in result.fetchall()]

        if not users:
            print("❌ 没有找到admin或demo用户")
            print("   请先启动应用完成全新部署初始化")
            return

        print(f"找到用户: {', '.join(users)}")
        print()

        # 重置admin密码
        if 'admin' in users:
            admin_hash = get_password_hash(settings.admin_initial_password)
            conn.execute(text(
                "UPDATE users SET hashed_password = :pwd WHERE username = 'admin'"
            ), {"pwd": admin_hash})
            print("✅ Admin密码已重置")
            print()

        # 重置demo密码
        if 'demo' in users:
            demo_hash = get_password_hash(settings.demo_initial_password)
            conn.execute(text(
                "UPDATE users SET hashed_password = :pwd WHERE username = 'demo'"
            ), {"pwd": demo_hash})
            print("✅ demo密码已重置")
            print()

        conn.commit()

    print("=" * 60)
    print("密码重置完成！")
    print("请使用新密码登录系统")
    print("=" * 60)

if __name__ == "__main__":
    main()
