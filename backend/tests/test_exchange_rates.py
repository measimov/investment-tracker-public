#!/usr/bin/env python3
"""
汇率系统功能测试脚本
"""
import sys
import os
from datetime import date

# 添加父目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.database import SessionLocal
from app.models.exchange_rate import ExchangeRate
from app.services import exchange_rate_service
from decimal import Decimal


def seed_exchange_rates(db):
    db.query(ExchangeRate).delete()
    db.add_all([
        ExchangeRate(
            from_currency="USD",
            to_currency="CNY",
            rate=Decimal("7.20"),
            effective_date=date(2026, 1, 1),
            source="test",
            is_active=True,
        ),
        ExchangeRate(
            from_currency="HKD",
            to_currency="CNY",
            rate=Decimal("0.92"),
            effective_date=date(2026, 1, 1),
            source="test",
            is_active=True,
        ),
        ExchangeRate(
            from_currency="SGD",
            to_currency="CNY",
            rate=Decimal("5.35"),
            effective_date=date(2026, 1, 1),
            source="test",
            is_active=True,
        ),
    ])
    db.commit()

def test_exchange_rates():
    """测试汇率功能"""
    print("=" * 60)
    print("🧪 汇率系统功能测试")
    print("=" * 60)

    db = SessionLocal()
    seed_exchange_rates(db)

    # 1. 测试获取最新汇率
    print("\n1️⃣  测试获取最新汇率")
    print("-" * 60)
    rates = exchange_rate_service.get_all_latest_rates(db, "CNY")
    for currency, rate in rates.items():
        print(f"  {currency}: {float(rate):.4f}")
    assert rates["CNY"] == Decimal("1.0")
    assert rates["USD"] == Decimal("7.20")
    assert rates["HKD"] == Decimal("0.92")
    assert rates["SGD"] == Decimal("5.35")

    # 2. 测试货币转换
    print("\n2️⃣  测试货币转换")
    print("-" * 60)

    test_cases = [
        (1000, "USD", "CNY"),
        (5000, "HKD", "CNY"),
        (1000, "SGD", "CNY"),
        (10000, "CNY", "CNY"),
    ]

    for amount, from_curr, to_curr in test_cases:
        try:
            converted = exchange_rate_service.convert_amount(
                db, Decimal(str(amount)), from_curr, to_curr
            )
            rate = exchange_rate_service.get_latest_rate(db, from_curr, to_curr)
            print(f"  {from_curr} {amount:,} -> {to_curr} {float(converted):,.2f} @ {float(rate):.4f}")
        except Exception as e:
            print(f"  ❌ {from_curr} -> {to_curr}: {e}")

    # 3. 测试CNY转USD
    print("\n3️⃣  测试CNY转USD")
    print("-" * 60)

    cny_amounts = [10000, 50000, 100000, 1000000]
    for cny_amount in cny_amounts:
        try:
            usd_amount = exchange_rate_service.convert_to_usd(db, Decimal(str(cny_amount)))
            print(f"  ¥{cny_amount:,} -> ${float(usd_amount):,.2f}")
        except Exception as e:
            print(f"  ❌ ¥{cny_amount}: {e}")

    # 5. 测试汇率详细信息
    print("\n5️⃣  测试汇率详细信息")
    print("-" * 60)

    for currency in ["USD", "HKD", "SGD"]:
        try:
            info = exchange_rate_service.get_rate_info(db, currency, "CNY")
            if info:
                print(f"  {currency} -> CNY:")
                print(f"    汇率: {float(info['rate']):.4f}")
                print(f"    日期: {info['effective_date']}")
                print(f"    来源: {info['source']}")
                assert info["source"] == "test"
        except Exception as e:
            print(f"  ❌ {currency}: {e}")

    print("\n" + "=" * 60)
    print("✅ 测试完成！")
    print("=" * 60)
    db.close()

if __name__ == '__main__':
    test_exchange_rates()
