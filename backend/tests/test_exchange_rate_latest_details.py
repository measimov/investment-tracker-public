"""`GET /exchange-rates/latest` 按币种返回各自的生效日期与来源（#220）。

此前所有卡片共用全表最新一条的日期与来源：SGD 三个月没更新也显示「今天 · api」。
"""

from datetime import date
from decimal import Decimal

import pytest

from app.api.exchange_rates import get_latest_rates
from app.database import SessionLocal
from app.models.exchange_rate import ExchangeRate
from app.services import exchange_rate_service


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        session.query(ExchangeRate).filter(ExchangeRate.from_currency.in_(("ZZA", "ZZB"))).delete()
        session.commit()
        yield session
        session.rollback()
        session.query(ExchangeRate).filter(ExchangeRate.from_currency.in_(("ZZA", "ZZB"))).delete()
        session.commit()
    finally:
        session.close()


def test_latest_details_are_per_currency(db):
    db.add_all(
        [
            ExchangeRate(
                from_currency="ZZA",
                to_currency="CNY",
                rate=Decimal("7.1"),
                effective_date=date(2026, 6, 1),
                source="details-test-manual",
            ),
            ExchangeRate(
                from_currency="ZZA",
                to_currency="CNY",
                rate=Decimal("7.2"),
                effective_date=date(2026, 9, 1),
                source="details-test-api",
            ),
            ExchangeRate(
                from_currency="ZZB",
                to_currency="CNY",
                rate=Decimal("5.3"),
                effective_date=date(2026, 3, 1),
                source="details-test-manual",
            ),
            # 停用行不参与
            ExchangeRate(
                from_currency="ZZB",
                to_currency="CNY",
                rate=Decimal("9.9"),
                effective_date=date(2026, 9, 20),
                source="details-test-api",
                is_active=False,
            ),
        ]
    )
    db.commit()

    details = exchange_rate_service.get_latest_rate_details(db, "CNY")
    assert details["ZZA"] == {
        "rate": Decimal("7.2"),
        "effective_date": date(2026, 9, 1),
        "source": "details-test-api",
    }
    assert details["ZZB"] == {
        "rate": Decimal("5.3"),
        "effective_date": date(2026, 3, 1),
        "source": "details-test-manual",
    }
    assert "CNY" not in details

    rates = {
        "CNY": 1,
        **{
            c: d["rate"]
            for c, d in exchange_rate_service.get_latest_rate_details(db, "CNY").items()
        },
    }
    assert rates["CNY"] == Decimal("1.0")
    assert rates["ZZA"] == Decimal("7.2") and rates["ZZB"] == Decimal("5.3")

    response = get_latest_rates(current_user=None, db=db)
    assert response["details"]["ZZB"]["effective_date"] == date(2026, 3, 1)
    assert response["rates"]["ZZA"] == pytest.approx(7.2)
    # 兼容字段：各币种中最新的一条
    newest = max(item["effective_date"] for item in response["details"].values())
    assert response["effective_date"] == newest
