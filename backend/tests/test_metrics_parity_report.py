"""收益口径双跑脚本：两种估值口径都能出快照，「今天」可固定，取价差异会被比出来。"""

import importlib.util
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.database import SessionLocal
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.security_price import SecurityPrice
from app.models.transaction import Transaction
from app.services.holding_service import recalculate_holdings
from tests.helpers import reset_tables

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "metrics_parity_report.py"
RESET_MODELS = (Holding, CorporateAction, Transaction, SecurityPrice)


def _load_script():
    spec = importlib.util.spec_from_file_location("metrics_parity_report", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed(db):
    db.add(
        Transaction(
            user_id=1,
            symbol="600000",
            name="浦发银行",
            market="A股",
            transaction_type="BUY",
            quantity=Decimal("100"),
            price=Decimal("10"),
            fee=Decimal("0"),
            transaction_date=date(2026, 1, 5),
            currency="CNY",
        )
    )
    db.commit()
    recalculate_holdings(db, 1, "600000", "A股")
    holding = db.query(Holding).filter(Holding.user_id == 1).one()
    holding.current_price = Decimal("11")
    holding.price_updated_at = datetime(2026, 3, 1, 7, 0, tzinfo=timezone.utc)
    db.add(
        SecurityPrice(
            symbol="600000",
            market="A股",
            price_date=date(2026, 3, 2),
            close_price=Decimal("12"),
            currency="CNY",
        )
    )
    db.commit()


def test_snapshot_both_price_modes_and_fixed_today(tmp_path):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        _seed(db)
        script = _load_script()
        today = date(2026, 3, 3)

        holding_snapshot = script.build_snapshot(1, prices_mode="holding", today=today)
        server_snapshot = script.build_snapshot(1, prices_mode="server", today=today)

        for snapshot in (holding_snapshot, server_snapshot):
            assert {"price_inputs", "period_pnl", "performance_analytics"} <= snapshot.keys()
            assert snapshot["period_pnl"]["as_of"] == today.isoformat()
            assert "generated_at" not in snapshot["performance_analytics"]
        assert holding_snapshot["price_inputs"]["prices"] == {"600000:A股": 11.0}
        assert server_snapshot["price_inputs"]["mode"] == "server"

        # 同一天两次运行逐字节一致：固定 today 后快照是确定的
        again = script.build_snapshot(1, prices_mode="holding", today=today)
        assert json.dumps(again, sort_keys=True) == json.dumps(holding_snapshot, sort_keys=True)

        # 取价输入不同 → compare 报出数值变化（返回 1）
        old_path, new_path = tmp_path / "old.json", tmp_path / "new.json"
        old_path.write_text(json.dumps(holding_snapshot, default=str))
        modified = json.loads(json.dumps(holding_snapshot, default=str))
        modified["price_inputs"]["prices"]["600000:A股"] = 12.0
        new_path.write_text(json.dumps(modified))
        assert script.compare(str(old_path), str(old_path)) == 0
        assert script.compare(str(old_path), str(new_path)) == 1
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_fixed_today_is_independent_of_the_wall_clock(monkeypatch):
    """PR #292 评审：--today 要一路传到 XIRR 终值日、区间钳制、曲线末点与终点对账。

    周一跑基线、周二用同一个 --today 跑改动后的快照，两份应逐字节一致；此前 performance
    summary/analytics 内部仍读 date.today()，跨天会冒出与被测改动无关的差异。
    """
    import app.services.statistics.aggregates as aggregates
    import app.services.statistics.analytics as analytics

    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        _seed(db)
        script = _load_script()
        today = date(2026, 3, 3)
        baseline = script.build_snapshot(1, prices_mode="holding", today=today)

        class NextWeek(date):
            @classmethod
            def today(cls):
                return date(2026, 3, 10)

        monkeypatch.setattr(aggregates, "date", NextWeek)
        monkeypatch.setattr(analytics, "date", NextWeek)
        later = script.build_snapshot(1, prices_mode="holding", today=today)
        assert json.dumps(later, sort_keys=True, default=str) == json.dumps(
            baseline, sort_keys=True, default=str
        )
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()
