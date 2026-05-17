from app.database import SessionLocal
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.transaction import Transaction
from app.services import ibkr_activity_importer as importer
from app.services.ibkr_activity_importer import (
    import_ibkr_activity,
    is_option_symbol,
    parse_rows,
)


def reset_tables(db):
    for model in (BrokerFundFlow, IbkrActivityFlow, Holding, CorporateAction, Transaction):
        db.query(model).delete()
    db.commit()


def ibkr_csv(*data_rows: str) -> bytes:
    header = "\n".join(
        [
            "Statement,Header,域名称,域值",
            "总结,Header,域名称,域值",
            "总结,Data,基础货币,USD",
            "Transaction History,Header,日期,账户,说明,交易类型,代码,数量,价格,Price Currency,总额,佣金,净额",
        ]
    )
    return (header + "\n" + "\n".join(data_rows) + "\n").encode("utf-8")


def test_ibkr_import_resolves_name_from_symbol_lookup(monkeypatch):
    monkeypatch.setattr(
        importer,
        "lookup_tushare_security_name",
        lambda symbol, market: {
            ("00883", "港股"): "中国海洋石油",
        }.get((symbol, market)),
    )

    contents = ibkr_csv(
        "Transaction History,Data,2026-05-07,U***67968,CNOOC LTD-H,买,883,"
        "1000.0,27.36,HKD,-3493.05,-2.79,-3499.52"
    )

    rows, _, _, errors = parse_rows(contents, "ibkr.csv")

    assert errors == []
    assert rows[0].symbol == "00883"
    assert rows[0].name == "中国海洋石油"
    assert rows[0].description == "CNOOC LTD-H"


def test_ibkr_option_detection_covers_occ_and_hk_alias_formats():
    assert is_option_symbol("PYPL  260417P00040000", "PYPL 17APR26 40 P")
    assert is_option_symbol("POP APR26 155 P", "9992 29APR26 155 P")
    assert is_option_symbol("CNC JAN26 20 P", "883 29JAN26 20 P")
    assert is_option_symbol("MIU JAN26 52.5 C", "1810 29JAN26 52.5 C")
    assert not is_option_symbol("883", "CNOOC LTD-H")
    assert not is_option_symbol("PCT", "PC PARTNER GROUP LTD")


def test_ibkr_exercise_rows_are_imported_only_when_long_only_safe(monkeypatch):
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda symbol, market: None)

    contents = ibkr_csv(
        "Transaction History,Data,2026-03-20,U***67968,"
        "卖 -100 INVESCO CURRENCYSHARES EURO (行使),行权,FXE,"
        "-100.0,107.0,USD,10700.0,-0.0195,10699.9805",
        "Transaction History,Data,2025-08-28,U***67968,"
        "买 500 MEITUAN-CLASS B (转让),被行权,3690,"
        "500.0,125.0,HKD,-8125.0,-2.0,-8127.0",
    )

    rows, _, _, errors = parse_rows(contents, "ibkr-exercise.csv")

    assert errors == []
    assert len(rows) == 2
    assert rows[0].skip_reason == "option"
    assert not rows[0].is_trade
    assert rows[1].skip_reason is None
    assert rows[1].is_trade


def test_ibkr_identical_stock_fills_get_distinct_hashes(monkeypatch):
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda symbol, market: None)

    contents = ibkr_csv(
        "Transaction History,Data,2025-04-07,U***67968,WEIBO CORP-CLASS A,"
        "买,9898,40.0,67.45,HKD,-347.34051999999997,-,-347.73663920482",
        "Transaction History,Data,2025-04-07,U***67968,WEIBO CORP-CLASS A,"
        "买,9898,40.0,67.45,HKD,-347.34051999999997,-,-347.73663920482",
    )

    rows, _, _, errors = parse_rows(contents, "ibkr-identical-fills.csv")

    assert errors == []
    assert len(rows) == 2
    assert rows[0].is_trade
    assert rows[1].is_trade
    assert rows[0].row_hash != rows[1].row_hash


def test_ibkr_statement_skips_representative_option_rows(monkeypatch):
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda symbol, market: None)

    contents = ibkr_csv(
        "Transaction History,Data,2025-01-02,U***67968,CNOOC LTD-H,买,883,"
        "1000.0,27.36,HKD,-3493.05,-2.79,-3499.52",
        "Transaction History,Data,2025-01-03,U***67968,883 29JAN26 20 P,卖,CNC JAN26 20 P,"
        "-1.0,0.55,HKD,55.0,-1.0,54.0",
        "Transaction History,Data,2025-01-04,U***67968,PYPL 17APR26 40 P,买,"
        "PYPL  260417P00040000,1.0,1.25,USD,-125.0,-1.0,-126.0",
        "Transaction History,Data,2025-01-05,U***67968,"
        "卖 -100 INVESCO CURRENCYSHARES EURO (行使),行权,FXE,"
        "-100.0,107.0,USD,10700.0,-0.0195,10699.9805",
        "Transaction History,Data,2025-01-06,U***67968,PC PARTNER GROUP LTD,买,PCT,"
        "1000.0,1.91,SGD,-1910.0,-2.0,-1912.0",
    )
    rows, _, total_rows, errors = parse_rows(contents, "ibkr-representative-options.csv")

    assert errors == []
    assert total_rows == 5
    assert len([row for row in rows if row.skip_reason == "option"]) == 3

    skipped_symbols = {row.raw_symbol for row in rows if row.skip_reason == "option"}
    imported_symbols = {row.raw_symbol for row in rows if row.skip_reason is None}
    assert skipped_symbols == {"CNC JAN26 20 P", "PYPL  260417P00040000", "FXE"}
    assert imported_symbols == {"883", "PCT"}


def test_ibkr_import_handles_pc_partner_hk_to_sg_relisting(monkeypatch):
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda symbol, market: None)

    db = SessionLocal()
    reset_tables(db)
    try:
        contents = ibkr_csv(
            "Transaction History,Data,2026-05-04,U***67968,PC PARTNER GROUP LTD,"
            "卖,PCT,-1000.0,1.91,SGD,1495.3963,-1.957325,1493.438975",
            "Transaction History,Data,2025-12-09,U***67968,PC PARTNER GROUP LTD,"
            "买,1263,2000.0,5.41,HKD,-1390.3700000000001,-2.313,-1394.1361255450001",
            "Transaction History,Data,2025-10-30,U***67968,PC PARTNER GROUP LTD,"
            "买,1263,2000.0,6.31,HKD,-1624.1940000000002,-2.3166,-1628.229989529",
            "Transaction History,Data,2025-10-09,U***67968,PC PARTNER GROUP LTD,"
            "买,1263,2000.0,7.09,HKD,-1822.2718000000002,-2.31318,-1826.5645647463002",
        )

        result = import_ibkr_activity(db, 1, contents, "ibkr-pct.csv")

        old_holding = (
            db.query(Holding)
            .filter(Holding.user_id == 1, Holding.symbol == "01263", Holding.market == "港股")
            .first()
        )
        new_holding = (
            db.query(Holding)
            .filter(Holding.user_id == 1, Holding.symbol == "PCT", Holding.market == "新加坡股")
            .first()
        )
        synthetic_count = (
            db.query(Transaction)
            .filter(Transaction.notes.like("%synthetic_relisting_transfer%"))
            .count()
        )

        assert result["errors"] == []
        assert result["eligible_trade_rows"] == 4
        assert result["imported_transactions"] == 6
        assert synthetic_count == 2
        assert old_holding is None
        assert new_holding is not None
        assert new_holding.name == "柏能集团"
        assert new_holding.quantity == 5000
        assert new_holding.currency == "SGD"
        assert 1 < float(new_holding.avg_cost) < 1.1
    finally:
        db.close()
