from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.database import SessionLocal
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.corporate_action import CorporateAction
from app.models.exchange_rate import ExchangeRate
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.security_price import SecurityPrice
from app.models.transaction import Transaction
from app.services import market_data_service, performance_history_jobs
from app.services.market_data_service import fetch_and_store_security_price_history_incremental
from app.services.performance_history_jobs import get_history_sync_targets
from app.services.portfolio.metrics import (
    calculate_risk_metrics as _calculate_risk_metrics,
)
from app.services.statistics import (
    calculate_current_holdings_performance,
    calculate_performance_analytics,
    calculate_performance_summary,
    get_statistics_by_time,
)
from app.services.statistics.fifo_results import fifo_results_for_user
from app.services.statistics.fx import DbExchangeRateLookup
from tests.helpers import add_transaction, reset_tables


RESET_MODELS = (
    BrokerFundFlow,
    IbkrActivityFlow,
    SecurityPrice,
    Holding,
    CorporateAction,
    Transaction,
    ExchangeRate,
)


def test_exchange_rate_lookup_matches_historical_fallback_behavior():
    lookup = DbExchangeRateLookup([
        ExchangeRate(
            from_currency="USD",
            to_currency="CNY",
            rate=Decimal("7.0"),
            effective_date=date(2026, 1, 1),
            is_active=True,
        ),
        ExchangeRate(
            from_currency="USD",
            to_currency="CNY",
            rate=Decimal("7.2"),
            effective_date=date(2026, 2, 1),
            is_active=True,
        ),
        ExchangeRate(
            from_currency="CNY",
            to_currency="HKD",
            rate=Decimal("1.1"),
            effective_date=date(2026, 1, 1),
            is_active=True,
        ),
    ])

    assert lookup.get_rate_on_or_before("USD", "CNY", date(2026, 1, 15)) == Decimal("7.0")
    assert lookup.get_rate_on_or_before("USD", "CNY", date(2026, 3, 1)) == Decimal("7.2")
    assert lookup.get_rate_on_or_before("USD", "CNY", date(2025, 12, 1)) == Decimal("7.2")
    assert lookup.get_rate_on_or_before("HKD", "CNY", date(2026, 1, 15)) == Decimal("1") / Decimal("1.1")


def test_fifo_pnl_tracks_partial_lot_cost_and_remaining_cost():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        add_transaction(db, quantity=Decimal("100"), price=Decimal("10"), fee=Decimal("1"))
        add_transaction(
            db,
            quantity=Decimal("50"),
            price=Decimal("12"),
            transaction_date=date(2026, 1, 2),
        )
        add_transaction(
            db,
            transaction_type="SELL",
            quantity=Decimal("120"),
            price=Decimal("15"),
            fee=Decimal("2"),
            transaction_date=date(2026, 1, 3),
        )
        db.commit()

        result = fifo_results_for_user(db, 1, {("AAPL", "美股")})[("AAPL", "美股")]

        assert result["sold_cost"] == 1241.0
        assert result["realized_pnl"] == 557.0
        assert result["current_holdings_cost"] == 360.0
        assert result["buy_queue"] == [
            {
                "price": 12.0,
                "quantity": 30.0,
                "total_cost": 360.0,
                "date": "2026-01-02",
            }
        ]
    finally:
        db.close()


def test_performance_analytics_builds_daily_curve_and_trade_metrics():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        add_transaction(
            db,
            symbol="600000",
            name="浦发银行",
            market="A股",
            quantity=Decimal("100"),
            price=Decimal("10"),
            transaction_date=date(2026, 1, 1),
            currency="CNY",
        )
        add_transaction(
            db,
            symbol="600000",
            name="浦发银行",
            market="A股",
            transaction_type="SELL",
            quantity=Decimal("50"),
            price=Decimal("15"),
            fee=Decimal("0"),
            transaction_date=date(2026, 1, 3),
            currency="CNY",
        )
        for price_date, close_price in (
            (date(2026, 1, 1), Decimal("10")),
            (date(2026, 1, 2), Decimal("12")),
            (date(2026, 1, 3), Decimal("15")),
        ):
            db.add(
                SecurityPrice(
                    symbol="600000",
                    market="A股",
                    ts_code="600000.SH",
                    price_date=price_date,
                    currency="CNY",
                    close_price=close_price,
                    source="test",
                )
            )
        db.commit()

        result = calculate_performance_analytics(
            db,
            1,
            {"600000": 15},
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 3),
        )

        assert result["calculation_level"] == "daily_price_history"
        assert len(result["curve"]) == 3
        assert result["curve"][-1]["equity_cny"] == 750.0
        assert result["curve"][-1]["market_value_cny"] == 750.0
        assert result["curve"][-1]["net_invested_principal_cny"] == 250.0
        assert result["curve"][-1]["cash_out_cny"] == 750.0
        assert result["curve"][-1]["daily_return_rate"] == 25.0
        assert result["curve"][-1]["cumulative_return_rate"] == 50.0
        assert result["metrics"]["total_return_rate"] == 50.0
        assert result["methodology"]["status"] == "experimental"
        assert result["methodology"]["scope"] == "invested_securities_only"
        assert result["trade_skill"]["sample_count"] == 1
        assert result["trade_skill"]["win_rate"] == 100.0
        assert result["trade_skill"]["sample_unit"] == "closed_trade"
        assert result["trade_skill"]["status"] == "experimental"
        assert result["data_quality"]["warnings"] == []
    finally:
        db.close()


def test_performance_analytics_empty_result_exposes_methodology():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        result = calculate_performance_analytics(db, 1, {})

        assert result["calculation_level"] == "empty"
        assert result["methodology"]["status"] == "experimental"
        assert result["methodology"]["return_method"] == "ttwr_proxy"
        assert result["trade_skill"]["status"] == "experimental"
    finally:
        db.close()


def test_performance_analytics_custom_range_replays_opening_position():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        add_transaction(
            db,
            symbol="600000",
            name="浦发银行",
            market="A股",
            quantity=Decimal("100"),
            price=Decimal("10"),
            transaction_date=date(2026, 1, 1),
            currency="CNY",
        )
        add_transaction(
            db,
            symbol="600000",
            name="浦发银行",
            market="A股",
            transaction_type="SELL",
            quantity=Decimal("50"),
            price=Decimal("12"),
            transaction_date=date(2026, 1, 3),
            currency="CNY",
        )
        for price_date, close_price in (
            (date(2026, 1, 1), Decimal("10")),
            (date(2026, 1, 2), Decimal("11")),
            (date(2026, 1, 3), Decimal("12")),
        ):
            db.add(
                SecurityPrice(
                    symbol="600000",
                    market="A股",
                    ts_code="600000.SH",
                    price_date=price_date,
                    currency="CNY",
                    close_price=close_price,
                    source="test",
                )
            )
        db.commit()

        result = calculate_performance_analytics(
            db,
            1,
            {},
            start_date=date(2026, 1, 2),
            end_date=date(2026, 1, 3),
        )

        assert [point["date"] for point in result["curve"]] == ["2026-01-02", "2026-01-03"]
        assert result["curve"][0]["begin_market_value_cny"] == 1000.0
        assert result["curve"][0]["market_value_cny"] == 1100.0
        assert result["curve"][0]["cumulative_return_rate"] == 10.0
        assert result["curve"][-1]["market_value_cny"] == 600.0
        assert result["curve"][-1]["cash_out_cny"] == 600.0
        assert result["curve"][-1]["cumulative_return_rate"] == 20.0
        assert result["curve"][-1]["net_invested_principal_cny"] == 400.0
        assert result["data_quality"]["opening_market_value_cny"] == 1000.0
        assert result["data_quality"]["opening_positions"] == [
            {"symbol": "600000", "market": "A股", "quantity": 100.0}
        ]
        assert result["data_quality"]["invalid_position_events"] == []
        assert result["data_quality"]["curve_terminal_positions"] == [
            {"symbol": "600000", "market": "A股", "quantity": 50.0}
        ]
    finally:
        db.close()


def test_performance_analytics_custom_range_replays_prior_split():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        add_transaction(
            db,
            symbol="SPLIT",
            name="Split Test",
            market="美股",
            quantity=Decimal("100"),
            price=Decimal("10"),
            transaction_date=date(2026, 1, 1),
            currency="CNY",
        )
        db.add(
            CorporateAction(
                user_id=1,
                symbol="SPLIT",
                name="Split Test",
                market="美股",
                action_type="STOCK_SPLIT",
                ex_date=date(2026, 1, 2),
                split_ratio="1:2",
                currency="CNY",
            )
        )
        for price_date, close_price in (
            (date(2026, 1, 2), Decimal("5")),
            (date(2026, 1, 4), Decimal("6")),
        ):
            db.add(
                SecurityPrice(
                    symbol="SPLIT",
                    market="美股",
                    ts_code="SPLIT",
                    price_date=price_date,
                    currency="CNY",
                    close_price=close_price,
                    source="test",
                )
            )
        db.commit()

        result = calculate_performance_analytics(
            db,
            1,
            {},
            start_date=date(2026, 1, 3),
            end_date=date(2026, 1, 4),
        )

        assert result["data_quality"]["opening_market_value_cny"] == 1000.0
        assert result["data_quality"]["opening_positions"] == [
            {"symbol": "SPLIT", "market": "美股", "quantity": 200.0}
        ]
        assert result["curve"][0]["cumulative_return_rate"] == 0.0
        assert result["curve"][-1]["market_value_cny"] == 1200.0
        assert result["curve"][-1]["cumulative_return_rate"] == 20.0
    finally:
        db.close()


def test_performance_analytics_warns_when_opening_value_uses_transaction_price():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        add_transaction(
            db,
            symbol="NOHISTORY",
            name="No History",
            market="美股",
            quantity=Decimal("10"),
            price=Decimal("20"),
            transaction_date=date(2026, 1, 1),
            currency="CNY",
        )
        db.commit()

        result = calculate_performance_analytics(
            db,
            1,
            {},
            start_date=date(2026, 1, 2),
            end_date=date(2026, 1, 3),
        )

        assert result["data_quality"]["opening_market_value_cny"] == 200.0
        assert result["data_quality"]["opening_estimated_positions"] == [
            {"symbol": "NOHISTORY", "market": "美股"}
        ]
        assert any("最近交易价估算" in warning for warning in result["data_quality"]["warnings"])
    finally:
        db.close()


def test_performance_analytics_rejects_reversed_date_range():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        with pytest.raises(ValueError, match="end_date must be on or after start_date"):
            calculate_performance_analytics(
                db,
                1,
                {},
                start_date=date(2026, 1, 2),
                end_date=date(2026, 1, 1),
            )
    finally:
        db.close()


def test_performance_analytics_ttwr_is_distinct_from_money_weighted_summary():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        add_transaction(
            db,
            symbol="600000",
            name="浦发银行",
            market="A股",
            quantity=Decimal("100"),
            price=Decimal("10"),
            transaction_date=date(2026, 1, 1),
            currency="CNY",
        )
        add_transaction(
            db,
            symbol="600000",
            name="浦发银行",
            market="A股",
            transaction_type="SELL",
            quantity=Decimal("50"),
            price=Decimal("15"),
            fee=Decimal("0"),
            transaction_date=date(2026, 1, 3),
            currency="CNY",
        )
        db.add(
            Holding(
                user_id=1,
                symbol="600000",
                name="浦发银行",
                market="A股",
                quantity=Decimal("50"),
                avg_cost=Decimal("10"),
                total_cost=Decimal("500"),
                currency="CNY",
                current_price=Decimal("15"),
            )
        )
        db.commit()

        current_prices = {"600000": 15}
        summary = calculate_performance_summary(db, 1, current_prices)
        analytics = calculate_performance_analytics(
            db,
            1,
            current_prices,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 3),
        )

        assert summary["account_return"]["net_invested_principal_cny"] == 250.0
        assert summary["account_return"]["total_return_rate"] == 200.0
        assert summary["account_return"]["calculation_status"] == "exact"
        assert summary["account_return"]["calculation_scope"] == "invested_securities_only"
        assert analytics["curve"][-1]["net_invested_principal_cny"] == 250.0
        assert analytics["curve"][-1]["cumulative_return_rate"] == 50.0
        assert analytics["metrics"]["total_return_rate"] != summary["account_return"]["total_return_rate"]
        assert analytics["data_quality"]["return_method"] == "ttwr"
    finally:
        db.close()


def test_account_xirr_uses_transaction_date_fx():
    """Issue #42: XIRR converts each flow at its own-date FX, so a pure-FX gain
    on a flat USD position still shows a positive money-weighted return."""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        db.add(ExchangeRate(from_currency="USD", to_currency="CNY", rate=Decimal("6"),
                            effective_date=date(2020, 1, 1), is_active=True))
        db.add(ExchangeRate(from_currency="USD", to_currency="CNY", rate=Decimal("8"),
                            effective_date=date(2026, 1, 1), is_active=True))
        add_transaction(db, symbol="AAPL", market="美股", transaction_type="BUY",
                        quantity=Decimal("100"), price=Decimal("10"),
                        transaction_date=date(2020, 1, 1), currency="USD")
        add_transaction(db, symbol="AAPL", market="美股", transaction_type="SELL",
                        quantity=Decimal("100"), price=Decimal("10"),
                        transaction_date=date(2026, 1, 1), currency="USD")
        db.commit()

        account = calculate_performance_summary(db, 1, {})["account_return"]

        assert account["fx_basis"] == "transaction_date"
        # -6000 CNY in (2020 @6) then +8000 CNY out (2026 @8): flat in USD but a
        # real CNY gain, so the annualized (XIRR) return is clearly positive.
        # At today's flat rate it would have been ~0.
        assert account["annualized_return_rate"] is not None
        assert account["annualized_return_rate"] > 3.0
    finally:
        db.close()


def test_trade_skill_metrics_are_per_closing_trade():
    """Issue #43: win rate counts closing trades, not per-symbol net results."""
    from app.services.portfolio.metrics import (
        calculate_trade_skill_metrics as _calculate_trade_skill_metrics,
    )
    from app.services.statistics import calculate_realized_pnl_fifo

    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        d = 1
        for _ in range(10):  # 10 winning round-trips on ONE symbol
            add_transaction(db, symbol="600000", market="A股", transaction_type="BUY",
                            quantity=Decimal("10"), price=Decimal("10"),
                            transaction_date=date(2026, 1, d), currency="CNY")
            d += 1
            add_transaction(db, symbol="600000", market="A股", transaction_type="SELL",
                            quantity=Decimal("10"), price=Decimal("11"),
                            transaction_date=date(2026, 1, d), currency="CNY")
            d += 1
        # one big losing round-trip -> symbol net is negative
        add_transaction(db, symbol="600000", market="A股", transaction_type="BUY",
                        quantity=Decimal("100"), price=Decimal("10"),
                        transaction_date=date(2026, 2, 1), currency="CNY")
        add_transaction(db, symbol="600000", market="A股", transaction_type="SELL",
                        quantity=Decimal("100"), price=Decimal("7"),
                        transaction_date=date(2026, 2, 2), currency="CNY")
        db.commit()

        realized = calculate_realized_pnl_fifo(db, 1)
        skill = _calculate_trade_skill_metrics(realized)

        assert skill["sample_unit"] == "closed_trade"
        assert skill["sample_count"] == 11          # 11 closing trades, not 1 symbol
        assert skill["winning_count"] == 10
        assert skill["losing_count"] == 1
        assert round(skill["win_rate"], 2) == round(1000 / 11, 2)  # ~90.91%
        assert skill["has_losses"] is True
    finally:
        db.close()


def test_trade_skill_profit_factor_none_without_losses():
    """Issue #43: profit_factor is None but has_losses distinguishes it from no data."""
    from app.services.portfolio.metrics import (
        calculate_trade_skill_metrics as _calculate_trade_skill_metrics,
    )

    only_wins = _calculate_trade_skill_metrics({"closed_trades": [
        {"realized_pnl_cny": 100.0, "matched_cost_cny": 500.0},
        {"realized_pnl_cny": 50.0, "matched_cost_cny": 300.0},
    ]})
    assert only_wins["profit_factor"] is None
    assert only_wins["has_losses"] is False
    assert only_wins["sample_count"] == 2

    no_data = _calculate_trade_skill_metrics({"closed_trades": []})
    assert no_data["profit_factor"] is None
    assert no_data["has_losses"] is False
    assert no_data["sample_count"] == 0


def test_build_price_maps_uses_constant_query_count():
    """Issue #49: price maps load in 2 batched queries regardless of symbol count."""
    from sqlalchemy import event

    from app.database import engine
    from app.services.statistics.analytics import build_price_maps

    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        symbols = []
        for i in range(8):
            symbol = f"60000{i}"
            symbols.append((symbol, "A股", "CNY"))
            db.add(SecurityPrice(symbol=symbol, market="A股",
                                 price_date=date(2025, 12, 20),
                                 close_price=Decimal("9"), currency="CNY"))
            db.add(SecurityPrice(symbol=symbol, market="A股",
                                 price_date=date(2026, 1, 5),
                                 close_price=Decimal("10"), currency="CNY"))
        db.commit()

        statements = []

        def _track(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", _track)
        try:
            price_maps, counts = build_price_maps(
                db, symbols, date(2026, 1, 1), date(2026, 1, 31)
            )
        finally:
            event.remove(engine, "before_cursor_execute", _track)

        assert len(statements) == 2  # one range query + one opening-price query
        assert counts[("600000", "A股")] == 1
        # Opening price before the range is present for back-fill.
        assert date(2025, 12, 20) in price_maps[("600003", "A股")]
    finally:
        db.close()


def test_resolve_server_prices_prefers_holding_then_history():
    """Issue #46: valuation prices come from server authority, market-qualified."""
    from app.services.statistics import resolve_server_prices

    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        db.add(Holding(user_id=1, symbol="AAPL", name="Apple", market="美股",
                       quantity=Decimal("10"), avg_cost=Decimal("10"),
                       total_cost=Decimal("100"), currency="USD",
                       current_price=Decimal("15")))
        db.add(Holding(user_id=1, symbol="00700", name="Tencent", market="港股",
                       quantity=Decimal("5"), avg_cost=Decimal("20"),
                       total_cost=Decimal("100"), currency="HKD",
                       current_price=None))
        db.add(SecurityPrice(symbol="00700", market="港股",
                             price_date=date(2026, 1, 10),
                             close_price=Decimal("21"), currency="HKD"))
        db.add(SecurityPrice(symbol="00700", market="港股",
                             price_date=date(2026, 1, 12),
                             close_price=Decimal("22"), currency="HKD"))
        db.commit()

        prices, sources, freshness = resolve_server_prices(db, 1)

        assert prices["AAPL:美股"] == 15.0
        assert sources["AAPL:美股"] == "holding"
        # Falls back to the LATEST cached close for the missing one.
        assert prices["00700:港股"] == 22.0
        assert sources["00700:港股"] == "latest_history"
    finally:
        db.close()


def test_current_holdings_unpriced_positions_surfaced():
    """Issue #45: unpriced holdings are excluded but surfaced, and market-keyed
    prices resolve correctly."""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        add_transaction(db, symbol="AAPL", market="美股", transaction_type="BUY",
                        quantity=Decimal("100"), price=Decimal("10"),
                        transaction_date=date(2026, 1, 1), currency="CNY")
        db.add(Holding(user_id=1, symbol="AAPL", name="Apple", market="美股",
                       quantity=Decimal("100"), avg_cost=Decimal("10"),
                       total_cost=Decimal("1000"), currency="CNY"))
        add_transaction(db, symbol="00700", market="港股", transaction_type="BUY",
                        quantity=Decimal("50"), price=Decimal("20"),
                        transaction_date=date(2026, 1, 1), currency="CNY")
        db.add(Holding(user_id=1, symbol="00700", name="Tencent", market="港股",
                       quantity=Decimal("50"), avg_cost=Decimal("20"),
                       total_cost=Decimal("1000"), currency="CNY"))
        db.commit()

        # Price supplied only for AAPL, via the market-qualified key form.
        perf = calculate_current_holdings_performance(db, 1, {"AAPL:美股": 12})

        # Totals reflect only the priced holding (self-consistent cost vs value).
        assert perf["current_holdings_cost_cny"] == 1000.0
        assert perf["current_market_value_cny"] == 1200.0
        assert len(perf["holdings_detail"]) == 1
        # The unpriced holding is surfaced, not silently dropped.
        assert [p["symbol"] for p in perf["unpriced_positions"]] == ["00700"]
        assert perf["data_quality"]["unpriced_position_count"] == 1
        assert any("估值价格" in w for w in perf["data_quality"]["warnings"])
    finally:
        db.close()


def test_statistics_by_time_year_and_cny_conversion():
    """Issue #44: 'year' grouping works and amounts are CNY-converted."""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        db.add(ExchangeRate(
            from_currency="USD", to_currency="CNY", rate=Decimal("7"),
            effective_date=date(2025, 1, 1), is_active=True,
        ))
        # 2025: one CNY buy (100*10=1000) and one USD buy (10*20=200 USD -> 1400 CNY)
        add_transaction(db, symbol="600000", market="A股", transaction_type="BUY",
                        quantity=Decimal("100"), price=Decimal("10"),
                        transaction_date=date(2025, 3, 1), currency="CNY")
        add_transaction(db, symbol="AAPL", market="美股", transaction_type="BUY",
                        quantity=Decimal("10"), price=Decimal("20"),
                        transaction_date=date(2025, 6, 1), currency="USD")
        # 2026: two sells same year -> counts must accumulate, not overwrite
        add_transaction(db, symbol="600000", market="A股", transaction_type="SELL",
                        quantity=Decimal("50"), price=Decimal("12"),
                        transaction_date=date(2026, 2, 1), currency="CNY")
        add_transaction(db, symbol="600000", market="A股", transaction_type="SELL",
                        quantity=Decimal("50"), price=Decimal("13"),
                        transaction_date=date(2026, 4, 1), currency="CNY")
        db.commit()

        by_year = get_statistics_by_time(db, 1, group_by="year")
        assert [b["period"] for b in by_year] == ["2025", "2026"]

        y2025 = by_year[0]
        assert y2025["buy_count"] == 2
        assert y2025["buy_amount_cny"] == 2400.0  # 1000 CNY + 200 USD * 7
        assert y2025["buy_amount"] == 2400.0

        y2026 = by_year[1]
        assert y2026["sell_count"] == 2  # accumulated, not overwritten
        assert y2026["sell_amount_cny"] == 1250.0  # 600 + 650

        by_month = get_statistics_by_time(db, 1, group_by="month")
        assert {b["period"] for b in by_month} == {"2025-03", "2025-06", "2026-02", "2026-04"}
    finally:
        db.close()


def test_risk_metrics_annualize_by_calendar_time_not_sample_count():
    """Issue #40: annualization uses elapsed calendar days, not a fixed 252/N."""
    curve = [
        {"date": "2025-01-01", "daily_return_rate": None, "cumulative_return_rate": 0.0, "drawdown_rate": 0.0},
        {"date": "2025-07-02", "daily_return_rate": 5.0, "cumulative_return_rate": 5.0, "drawdown_rate": 0.0},
        {"date": "2026-01-01", "daily_return_rate": 4.7619, "cumulative_return_rate": 10.0, "drawdown_rate": 0.0},
    ]
    metrics = _calculate_risk_metrics(curve, Decimal("0"), "daily_price_history")

    assert metrics["annualization_basis"] == "calendar_days"
    assert metrics["observation_span_days"] == 365
    # 10% earned over one calendar year annualizes to ~10%, not a sample-count blowup.
    # 年化基准是 365.25（含闰年补偿，与 XIRR 统一，见 issue #138），故 365 天
    # 跨度的结果是 10.007% 而非恰好 10%——这里断言的是量级而非第三位小数。
    assert metrics["annualized_return_rate"] == pytest.approx(10.0, abs=0.02)


def test_risk_metrics_event_level_omits_annualized_figures():
    """Issue #40: event-level curves report only cumulative return and drawdown."""
    curve = [
        {"date": "2025-01-01", "daily_return_rate": None, "cumulative_return_rate": 0.0, "drawdown_rate": 0.0},
        {"date": "2025-06-01", "daily_return_rate": 3.0, "cumulative_return_rate": 3.0, "drawdown_rate": 0.0},
        {"date": "2026-01-01", "daily_return_rate": 3.0, "cumulative_return_rate": 6.09, "drawdown_rate": 0.0},
    ]
    metrics = _calculate_risk_metrics(curve, Decimal("0"), "event_level")

    assert metrics["annualization_basis"] == "none"
    assert metrics["annualized_return_rate"] is None
    assert metrics["annualized_volatility"] is None
    assert metrics["sharpe_ratio"] is None
    assert metrics["sortino_ratio"] is None


def test_risk_metrics_sortino_downside_denominator_is_total_n():
    """Issue #41: downside deviation divides by N (all obs), not the downside count."""
    # 5 daily observations over 5 calendar days; risk-free 0.
    rets = [2.0, -1.0, 3.0, -2.0, 4.0]
    curve = [{"date": "2026-01-01", "daily_return_rate": None,
              "cumulative_return_rate": 0.0, "drawdown_rate": 0.0}]
    cum = Decimal("1")
    for i, r in enumerate(rets):
        cum *= Decimal("1") + Decimal(str(r)) / Decimal("100")
        curve.append({
            "date": f"2026-01-0{i + 2}",
            "daily_return_rate": r,
            "cumulative_return_rate": float((cum - 1) * 100),
            "drawdown_rate": 0.0,
        })
    metrics = _calculate_risk_metrics(curve, Decimal("0"), "daily_price_history")

    # mean excess = 0.012; downside sum-of-squares = 0.0005; /N=5 -> dd = 0.01
    # period sortino = 1.2; annual factor = sqrt(365*5/5) = sqrt(365)
    import math
    expected = 1.2 * math.sqrt(365)
    assert round(metrics["sortino_ratio"], 2) == round(expected, 2)


def test_account_return_fully_exited_profit_uses_peak_principal():
    """Issue #39: a fully-sold profitable account must not report 0% return."""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        add_transaction(
            db,
            symbol="600000",
            name="浦发银行",
            market="A股",
            transaction_type="BUY",
            quantity=Decimal("100"),
            price=Decimal("10"),
            transaction_date=date(2026, 1, 1),
            currency="CNY",
        )
        add_transaction(
            db,
            symbol="600000",
            name="浦发银行",
            market="A股",
            transaction_type="SELL",
            quantity=Decimal("100"),
            price=Decimal("15"),
            transaction_date=date(2026, 2, 1),
            currency="CNY",
        )
        db.commit()

        account = calculate_performance_summary(db, 1, {})["account_return"]

        # Bought 1000, sold 1500 -> +500 realized on 1000 of deployed capital = 50%.
        assert account["total_return_cny"] == 500.0
        assert account["net_invested_principal_cny"] == -500.0
        assert account["peak_invested_principal_cny"] == 1000.0
        assert account["total_return_rate"] == 50.0
        assert account["rate_denominator"] == "peak_invested_principal_cny"
    finally:
        db.close()


def test_performance_analytics_ttwr_neutralizes_large_sell_cash_flow():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        add_transaction(
            db,
            symbol="600000",
            name="浦发银行",
            market="A股",
            quantity=Decimal("100"),
            price=Decimal("10"),
            transaction_date=date(2026, 1, 1),
            currency="CNY",
        )
        add_transaction(
            db,
            symbol="600000",
            name="浦发银行",
            market="A股",
            transaction_type="SELL",
            quantity=Decimal("99"),
            price=Decimal("10"),
            fee=Decimal("0"),
            transaction_date=date(2026, 1, 2),
            currency="CNY",
        )
        for price_date, close_price in (
            (date(2026, 1, 1), Decimal("10")),
            (date(2026, 1, 2), Decimal("100")),
        ):
            db.add(
                SecurityPrice(
                    symbol="600000",
                    market="A股",
                    ts_code="600000.SH",
                    price_date=price_date,
                    currency="CNY",
                    close_price=close_price,
                    source="test",
                )
            )
        db.commit()

        analytics = calculate_performance_analytics(
            db,
            1,
            {"600000": 100},
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 2),
        )

        assert analytics["curve"][-1]["market_value_cny"] == 100.0
        assert analytics["curve"][-1]["cash_out_cny"] == 990.0
        assert analytics["curve"][-1]["cumulative_return_rate"] == 9.0
        assert analytics["metrics"]["max_drawdown_rate"] == 0.0
    finally:
        db.close()


def test_performance_analytics_replays_same_day_buys_before_sells():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        add_transaction(
            db,
            symbol="513010",
            name="恒生科技ETF易方达",
            market="A股",
            quantity=Decimal("90"),
            price=Decimal("10"),
            transaction_date=date(2026, 1, 1),
            currency="CNY",
        )
        add_transaction(
            db,
            symbol="513010",
            name="恒生科技ETF易方达",
            market="A股",
            transaction_type="SELL",
            quantity=Decimal("100"),
            price=Decimal("11"),
            fee=Decimal("0"),
            transaction_date=date(2026, 1, 2),
            currency="CNY",
        )
        add_transaction(
            db,
            symbol="513010",
            name="恒生科技ETF易方达",
            market="A股",
            quantity=Decimal("10"),
            price=Decimal("10"),
            transaction_date=date(2026, 1, 2),
            currency="CNY",
        )
        for price_date, close_price in (
            (date(2026, 1, 1), Decimal("10")),
            (date(2026, 1, 2), Decimal("11")),
        ):
            db.add(
                SecurityPrice(
                    symbol="513010",
                    market="A股",
                    ts_code="513010.SH",
                    price_date=price_date,
                    currency="CNY",
                    close_price=close_price,
                    source="test",
                )
            )
        db.commit()

        analytics = calculate_performance_analytics(
            db,
            1,
            {},
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 2),
        )

        assert analytics["curve"][-1]["market_value_cny"] == 0.0
        assert analytics["curve"][-1]["cash_in_cny"] == 100.0
        assert analytics["curve"][-1]["cash_out_cny"] == 1100.0
        assert analytics["curve"][-1]["cumulative_return_rate"] == 10.0
        assert analytics["data_quality"]["invalid_position_events"] == []
        assert analytics["data_quality"]["curve_terminal_positions"] == []
    finally:
        db.close()


def test_performance_analytics_applies_reverse_split_to_curve_positions():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        # 必须显式给 USD 汇率：本用例测的是拆股因子，不是缺汇率兜底。
        # 此前没有这一行也能过，靠的正是"缺汇率就按 1:1 当成 CNY"的静默兜底
        # ——该兜底已按 issue #129 改为剔除并记录。
        db.add(ExchangeRate(
            from_currency="USD", to_currency="CNY", rate=Decimal("1"),
            effective_date=date(2024, 1, 1), source="test", is_active=True,
        ))
        add_transaction(
            db,
            symbol="FFIE",
            name="Faraday Future",
            market="美股",
            quantity=Decimal("1000"),
            price=Decimal("1"),
            transaction_date=date(2024, 1, 1),
            currency="USD",
        )
        db.add(
            CorporateAction(
                user_id=1,
                symbol="FFIE",
                name="Faraday Future",
                market="美股",
                action_type="REVERSE_SPLIT",
                ex_date=date(2024, 8, 19),
                split_ratio="40:1",
                new_shares=Decimal("25"),
                currency="USD",
            )
        )
        db.add(
            Holding(
                user_id=1,
                symbol="FFIE",
                name="Faraday Future",
                market="美股",
                quantity=Decimal("25"),
                avg_cost=Decimal("40"),
                total_cost=Decimal("1000"),
                currency="USD",
                current_price=Decimal("2"),
            )
        )
        db.add(
            SecurityPrice(
                symbol="FFIE",
                market="美股",
                ts_code="FFIE",
                price_date=date(2024, 8, 20),
                currency="USD",
                close_price=Decimal("2"),
                source="test",
            )
        )
        db.commit()

        current_prices = {"FFIE": 2}
        summary = calculate_performance_summary(db, 1, current_prices)
        analytics = calculate_performance_analytics(
            db,
            1,
            current_prices,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 8, 20),
        )

        assert summary["current_performance"]["current_market_value_cny"] == 50.0
        assert analytics["curve"][-1]["market_value_cny"] == 50.0
        assert analytics["curve"][-1]["cumulative_return_rate"] == -95.0
        assert analytics["metrics"]["total_return_rate"] == summary["account_return"]["total_return_rate"]
    finally:
        db.close()


def test_performance_analytics_terminal_point_uses_latest_history_when_current_price_missing():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        today = date.today()
        add_transaction(
            db,
            symbol="01093",
            name="石药集团",
            market="港股",
            quantity=Decimal("100"),
            price=Decimal("5"),
            transaction_date=today - timedelta(days=2),
            currency="HKD",
        )
        db.add(
            SecurityPrice(
                symbol="01093",
                market="港股",
                ts_code="01093.HK",
                price_date=today - timedelta(days=1),
                currency="HKD",
                close_price=Decimal("7.5"),
                source="test",
            )
        )
        db.add(
            Holding(
                user_id=1,
                symbol="01093",
                name="石药集团",
                market="港股",
                quantity=Decimal("100"),
                avg_cost=Decimal("5"),
                total_cost=Decimal("500"),
                currency="HKD",
                current_price=None,
            )
        )
        db.commit()

        result = calculate_performance_analytics(
            db,
            1,
            {},
            start_date=today - timedelta(days=2),
            end_date=today,
        )

        assert result["calculation_level"] == "daily_price_history"
        assert result["curve"][-1]["market_value_cny"] == 750.0
        assert result["curve"][-1]["stale_price_positions"] == [{"symbol": "01093", "market": "港股"}]
        assert result["data_quality"]["terminal_stale_price_positions"] == [{"symbol": "01093", "market": "港股"}]
        assert any("当前持仓缺少当前价格" in warning for warning in result["data_quality"]["warnings"])
    finally:
        db.close()


def test_performance_analytics_uses_record_currency_for_market_value():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        db.add(
            ExchangeRate(
                from_currency="USD",
                to_currency="CNY",
                rate=Decimal("7"),
                effective_date=date(2026, 1, 1),
                is_active=True,
            )
        )
        add_transaction(
            db,
            symbol="900926",
            name="宝信B",
            market="B股",
            quantity=Decimal("10"),
            price=Decimal("10"),
            transaction_date=date(2026, 1, 2),
            currency="USD",
        )
        db.add(
            Holding(
                user_id=1,
                symbol="900926",
                name="宝信B",
                market="B股",
                quantity=Decimal("10"),
                avg_cost=Decimal("10"),
                total_cost=Decimal("100"),
                currency="USD",
                current_price=Decimal("10"),
            )
        )
        db.commit()

        current_prices = {"900926": 10}
        summary = calculate_performance_summary(db, 1, current_prices)
        analytics = calculate_performance_analytics(
            db,
            1,
            current_prices,
            start_date=date(2026, 1, 2),
            end_date=date.today(),
        )

        assert summary["current_performance"]["current_market_value_cny"] == 700.0
        assert analytics["curve"][-1]["market_value_cny"] == 700.0
    finally:
        db.close()


def test_performance_analytics_clips_oversell_cash_flow_for_ttwr():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        add_transaction(
            db,
            symbol="600000",
            name="浦发银行",
            market="A股",
            quantity=Decimal("100"),
            price=Decimal("10"),
            transaction_date=date(2026, 1, 1),
            currency="CNY",
        )
        add_transaction(
            db,
            symbol="600000",
            name="浦发银行",
            market="A股",
            transaction_type="SELL",
            quantity=Decimal("150"),
            price=Decimal("10"),
            transaction_date=date(2026, 1, 2),
            currency="CNY",
        )
        db.add(
            Holding(
                user_id=1,
                symbol="600000",
                name="浦发银行",
                market="A股",
                quantity=Decimal("100"),
                avg_cost=Decimal("10"),
                total_cost=Decimal("1000"),
                currency="CNY",
                current_price=Decimal("10"),
            )
        )
        db.commit()

        current_prices = {"600000": 10}
        summary = calculate_performance_summary(db, 1, current_prices)
        analytics = calculate_performance_analytics(
            db,
            1,
            current_prices,
            start_date=date(2026, 1, 1),
            end_date=date.today(),
        )

        assert summary["account_return"]["total_return_rate"] == 0.0
        assert analytics["curve"][1]["cash_out_cny"] == 1000.0
        assert analytics["curve"][-1]["cumulative_return_rate"] == 0.0
        assert analytics["data_quality"]["invalid_position_events"] == [
            {
                "date": "2026-01-02",
                "symbol": "600000",
                "market": "A股",
                "transaction_type": "SELL",
                "quantity": 150.0,
                "available_quantity": 100.0,
            }
        ]
    finally:
        db.close()


def test_history_sync_default_end_date_uses_previous_day(monkeypatch):
    class FakeDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 6, 3)

    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        add_transaction(
            db,
            symbol="600000",
            market="A股",
            transaction_date=date(2026, 1, 1),
            currency="CNY",
        )
        db.commit()
        monkeypatch.setattr(performance_history_jobs, "date", FakeDate)

        result = get_history_sync_targets(db, 1)

        assert result["start_date"] == date(2026, 1, 1)
        assert result["end_date"] == date(2026, 6, 2)
    finally:
        db.close()


def test_incremental_history_sync_skips_when_cache_covers_range():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        for price_date, close_price in (
            (date(2026, 1, 1), Decimal("10")),
            (date(2026, 1, 2), Decimal("11")),
        ):
            db.add(
                SecurityPrice(
                    symbol="600000",
                    market="A股",
                    ts_code="600000.SH",
                    price_date=price_date,
                    currency="CNY",
                    close_price=close_price,
                    source="test",
                )
            )
        db.commit()

        result = fetch_and_store_security_price_history_incremental(
            db,
            symbol="600000",
            market="A股",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 2),
            currency="CNY",
        )

        assert result["success"] is True
        assert result["skipped"] is True
        assert result["rows"] == 0
        assert result["message"] == "历史行情缓存已覆盖当前区间"
    finally:
        db.close()


def test_yahoo_history_request_uses_exact_period_bounds(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "chart": {
                    "error": None,
                    "result": [{"timestamp": [], "indicators": {}}],
                }
            }

    def fake_get(url, *, params, headers, timeout):
        captured.update({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(market_data_service.requests, "get", fake_get)

    market_data_service._fetch_yahoo_chart(
        "PCT.SI",
        date(2014, 1, 2),
        date(2026, 1, 3),
    )

    assert "range" not in captured["params"]
    assert captured["params"]["period1"] == int(
        datetime(2014, 1, 2, tzinfo=timezone.utc).timestamp()
    )
    assert captured["params"]["period2"] == int(
        datetime(2026, 1, 4, tzinfo=timezone.utc).timestamp()
    )


def test_yahoo_long_range_without_data_is_reported_as_uncovered(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        monkeypatch.setattr(market_data_service, "_fetch_yahoo_chart", lambda *args: {})

        result = market_data_service.fetch_and_store_yahoo_price_history(
            db,
            symbol="PCT",
            market="新加坡股",
            start_date=date(2014, 1, 2),
            end_date=date(2026, 1, 3),
            currency="SGD",
        )

        assert result["success"] is False
        assert result["rows"] == 0
        assert result["coverage_status"] == "uncovered"
        assert result["requested_coverage"] == {
            "start_date": "2014-01-02",
            "end_date": "2026-01-03",
        }
        assert result["actual_coverage"] is None
        assert "未返回请求区间" in result["error"]
    finally:
        db.close()


def test_yahoo_short_range_without_data_is_explicit_no_data(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        monkeypatch.setattr(market_data_service, "_fetch_yahoo_chart", lambda *args: {})

        result = market_data_service.fetch_and_store_yahoo_price_history(
            db,
            symbol="PCT",
            market="新加坡股",
            start_date=date(2026, 1, 3),
            end_date=date(2026, 1, 4),
            currency="SGD",
        )

        assert result["success"] is True
        assert result["coverage_status"] == "no_data"
        assert "没有可用交易日数据" in result["message"]
    finally:
        db.close()


def test_incremental_history_sync_uses_yahoo_for_singapore_stock(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        def fake_fetch_yahoo_chart(symbol, start_date, end_date):
            assert symbol == "PCT.SI"
            assert start_date == date(2026, 1, 1)
            assert end_date == date(2026, 1, 3)
            return {
                "timestamp": [
                    int(datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp()),
                    int(datetime(2026, 1, 3, tzinfo=timezone.utc).timestamp()),
                ],
                "indicators": {
                    "quote": [{
                        "open": [1.0, 1.1],
                        "high": [1.2, 1.3],
                        "low": [0.9, 1.0],
                        "close": [1.05, 1.15],
                    }],
                    "adjclose": [{"adjclose": [1.04, 1.14]}],
                },
            }

        monkeypatch.setattr(market_data_service, "_fetch_yahoo_chart", fake_fetch_yahoo_chart)

        result = fetch_and_store_security_price_history_incremental(
            db,
            symbol="PCT",
            market="新加坡股",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 3),
            currency="SGD",
        )
        rows = db.query(SecurityPrice).filter(
            SecurityPrice.symbol == "PCT",
            SecurityPrice.market == "新加坡股",
        ).order_by(SecurityPrice.price_date).all()

        assert result["success"] is True
        assert result["rows"] == 2
        assert result["range_results"][0]["source"] == "yahoo-finance"
        assert result["range_results"][0]["coverage_status"] == "partial"
        assert result["coverage_complete"] is False
        assert result["remaining_edge_ranges"] == [
            {"start_date": "2026-01-01", "end_date": "2026-01-01"}
        ]
        assert [row.ts_code for row in rows] == ["PCT.SI", "PCT.SI"]
        assert [row.source for row in rows] == ["yahoo-finance", "yahoo-finance"]
        assert rows[0].currency == "SGD"
        assert rows[0].close_price == Decimal("1.05000000")
        assert rows[0].adj_close_price == Decimal("1.04000000")
    finally:
        db.close()


def test_incremental_history_sync_falls_back_to_stockanalysis(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        def fake_fetch_yahoo_chart(symbol, start_date, end_date):
            raise RuntimeError("Yahoo rate limited")

        def fake_fetch_stockanalysis_history(symbol):
            assert symbol == "PCT"
            return """
                <table>
                    <thead><tr><th>Date</th><th>Open</th><th>High</th><th>Low</th>
                    <th>Close</th><th>Adj. Close</th><th>Change</th><th>Volume</th></tr></thead>
                    <tbody>
                        <tr><td>Jan 3, 2026</td><td>1.10</td><td>1.30</td><td>1.00</td>
                        <td>1.15</td><td>1.14</td><td>9.52%</td><td>100,000</td></tr>
                        <tr><td>Jan 2, 2026</td><td>1.00</td><td>1.20</td><td>0.90</td>
                        <td>1.05</td><td>1.04</td><td>0.00%</td><td>90,000</td></tr>
                    </tbody>
                </table>
            """

        monkeypatch.setattr(market_data_service, "_fetch_yahoo_chart", fake_fetch_yahoo_chart)
        monkeypatch.setattr(
            market_data_service,
            "_fetch_stockanalysis_history",
            fake_fetch_stockanalysis_history,
        )

        result = fetch_and_store_security_price_history_incremental(
            db,
            symbol="PCT",
            market="新加坡股",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 3),
            currency="SGD",
        )
        rows = db.query(SecurityPrice).filter(
            SecurityPrice.symbol == "PCT",
            SecurityPrice.market == "新加坡股",
        ).order_by(SecurityPrice.price_date).all()

        assert result["success"] is True
        assert result["rows"] == 2
        assert result["range_results"][0]["source"] == "stockanalysis"
        assert result["range_results"][0]["fallback_from"] == "yahoo-finance"
        assert [row.source for row in rows] == ["stockanalysis", "stockanalysis"]
        assert rows[0].price_date == date(2026, 1, 2)
        assert rows[1].pre_close_price == Decimal("1.05000000")
    finally:
        db.close()


def _add_price(db, symbol, price_date, close_price, market="A股"):
    db.add(
        SecurityPrice(
            symbol=symbol,
            market=market,
            ts_code=f"{symbol}.SH",
            price_date=price_date,
            currency="CNY",
            close_price=close_price,
            source="test",
        )
    )


def test_trade_skill_and_range_summary_respect_selected_range():
    """交易能力指标与区间汇总必须遵守所选区间：只统计平仓日在区间内的交易、
    支付日在区间内的股息；区间 XIRR 以期初/期末市值为合成流。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        # 第一笔平仓（盈利）：1/2 买 → 1/5 卖
        add_transaction(db, symbol="600000", name="甲", market="A股",
                        quantity=Decimal("100"), price=Decimal("10"),
                        transaction_date=date(2026, 1, 2), currency="CNY")
        add_transaction(db, symbol="600000", name="甲", market="A股",
                        transaction_type="SELL", quantity=Decimal("100"),
                        price=Decimal("12"), fee=Decimal("0"),
                        transaction_date=date(2026, 1, 5), currency="CNY")
        # 第二笔平仓（亏损）：2/2 买 → 2/5 卖
        add_transaction(db, symbol="000001", name="乙", market="A股",
                        quantity=Decimal("100"), price=Decimal("10"),
                        transaction_date=date(2026, 2, 2), currency="CNY")
        add_transaction(db, symbol="000001", name="乙", market="A股",
                        transaction_type="SELL", quantity=Decimal("100"),
                        price=Decimal("9"), fee=Decimal("0"),
                        transaction_date=date(2026, 2, 5), currency="CNY")
        # 股息：一笔在 2 月区间内、一笔在 1 月
        for pay_date, sym in ((date(2026, 1, 20), "600000"), (date(2026, 2, 3), "000001")):
            db.add(CorporateAction(
                user_id=1, symbol=sym, name=sym, market="A股",
                action_type="CASH_DIVIDEND", ex_date=pay_date, payment_date=pay_date,
                total_dividend=Decimal("30"), tax_withheld=Decimal("0"),
                net_dividend=Decimal("30"), currency="CNY",
            ))
        for d, p1, p2 in (
            (date(2026, 2, 1), Decimal("12"), Decimal("10")),
            (date(2026, 2, 5), Decimal("12"), Decimal("9")),
        ):
            _add_price(db, "600000", d, p1)
            _add_price(db, "000001", d, p2)
        db.commit()

        # 只选 2 月：仅第二笔平仓与第二笔股息计入
        february = calculate_performance_analytics(
            db, 1, {"000001": 9},
            start_date=date(2026, 2, 1), end_date=date(2026, 2, 5),
        )
        assert february["trade_skill"]["sample_count"] == 1
        assert february["trade_skill"]["win_rate"] == 0.0
        summary = february["range_summary"]
        assert summary["realized_pnl_cny"] == -100.0
        assert summary["closed_trade_count"] == 1
        assert summary["dividend_net_cny"] == 30.0
        assert summary["dividend_count"] == 1
        assert summary["xirr_annualized_rate"] is not None

        # 全历史：两笔平仓、两笔股息
        full = calculate_performance_analytics(db, 1, {"000001": 9})
        assert full["trade_skill"]["sample_count"] == 2
        assert full["trade_skill"]["win_rate"] == 50.0
        assert full["range_summary"]["closed_trade_count"] == 2
        assert full["range_summary"]["realized_pnl_cny"] == 100.0
        assert full["range_summary"]["dividend_count"] == 2
    finally:
        db.close()


def test_range_is_clamped_to_history_and_echoed():
    """请求区间越界时钳制到 [首笔交易日, 最后事件日]，并回显 requested/effective。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        add_transaction(db, symbol="600000", name="甲", market="A股",
                        quantity=Decimal("100"), price=Decimal("10"),
                        transaction_date=date(2026, 1, 2), currency="CNY")
        db.commit()

        result = calculate_performance_analytics(
            db, 1, {"600000": 10},
            start_date=date(2010, 1, 1), end_date=date(2026, 1, 3),
        )
        assert result["date_range"]["start_date"] == "2026-01-02"
        assert result["date_range"]["requested_start_date"] == "2010-01-01"
        assert result["date_range"]["clamped"] is True

        default = calculate_performance_analytics(db, 1, {"600000": 10})
        assert default["date_range"]["clamped"] is False
        assert default["date_range"]["requested_start_date"] is None
    finally:
        db.close()


def test_range_xirr_uses_opening_and_closing_market_values():
    """期中起点的区间 XIRR：期初市值作合成投入流，期末市值作回收流。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        add_transaction(db, symbol="600000", name="甲", market="A股",
                        quantity=Decimal("100"), price=Decimal("10"),
                        transaction_date=date(2026, 1, 2), currency="CNY")
        for d, p in ((date(2026, 1, 2), Decimal("10")),
                     (date(2026, 2, 1), Decimal("11")),
                     (date(2026, 2, 10), Decimal("12"))):
            _add_price(db, "600000", d, p)
        db.commit()

        # 区间 2/1-2/10：期初市值 1100（2/1 前最近收盘 = 1/2? 应取 2/1 当日前）
        result = calculate_performance_analytics(
            db, 1, {"600000": 12},
            start_date=date(2026, 2, 1), end_date=date(2026, 2, 10),
        )
        summary = result["range_summary"]
        assert summary["opening_market_value_cny"] > 0
        assert summary["closing_market_value_cny"] == 1200.0
        # 区间内无买卖：XIRR 完全由市值变化决定，应为正
        assert summary["xirr_annualized_rate"] is not None
        assert summary["xirr_annualized_rate"] > 0
        assert summary["closed_trade_count"] == 0
    finally:
        db.close()


def test_disjoint_range_clamps_to_nearest_boundary_day():
    """请求区间与历史完全无交集：钳制到最近边界单日并置 clamped，
    不得在 API 预校验之后抛错变成 500。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        add_transaction(db, symbol="600000", name="甲", market="A股",
                        quantity=Decimal("100"), price=Decimal("10"),
                        transaction_date=date(2026, 1, 2), currency="CNY")
        db.commit()

        # 全部早于首笔交易
        before = calculate_performance_analytics(
            db, 1, {"600000": 10},
            start_date=date(2020, 1, 1), end_date=date(2020, 12, 31),
        )
        assert before["date_range"]["start_date"] == "2026-01-02"
        assert before["date_range"]["end_date"] == "2026-01-02"
        assert before["date_range"]["clamped"] is True
        assert before["range_summary"]["closed_trade_count"] == 0

        # 全部晚于最后事件（last_event_date 含今天，取遥远未来）
        after = calculate_performance_analytics(
            db, 1, {"600000": 10},
            start_date=date(2100, 1, 1), end_date=date(2100, 12, 31),
        )
        assert after["date_range"]["start_date"] == after["date_range"]["end_date"]
        assert after["date_range"]["clamped"] is True
    finally:
        db.close()
