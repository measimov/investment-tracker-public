"""security_rules 表驱动特例规则（issue #82）：服务 getter、豁免收窄、API。"""

from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.database import SessionLocal
from app.main import app
from app.models.broker_account import BrokerAccount
from app.models.holding import Holding
from app.models.security_rule import SecurityRule
from app.models.transaction import Transaction
from app.services.performance_history_jobs import get_history_sync_targets
from app.services.security_rule_service import (
    get_cash_management_symbols,
    get_cmb_cash_business_map,
    get_excluded_keys,
    get_excluded_symbols,
    get_name_overrides,
    get_price_gap_exemptions,
    get_relistings,
)
from tests.helpers import PCT_RELISTING_PAYLOAD, reset_tables, seed_security_rule

RESET_MODELS = (SecurityRule, Holding, Transaction, BrokerAccount)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_service_getters_are_type_scoped():
    """六类 getter 各取各的类型，互不越界（排除≠现金管理是硬约束）。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        seed_security_rule(db, 1, "EXCLUDE", "511880", "A股")
        seed_security_rule(db, 1, "CASH_MANAGEMENT", "880013", "A股")
        seed_security_rule(db, 1, "RELISTING", "01263", "港股", payload=PCT_RELISTING_PAYLOAD)
        seed_security_rule(db, 1, "NAME_OVERRIDE", "PCT", "新加坡股", payload={"name": "柏能集团"})
        seed_security_rule(
            db, 1, "PRICE_GAP_EXEMPTION", "01263", "港股",
            payload={"start_date": "2026-01-09", "end_date": None},
        )
        seed_security_rule(
            db, 1, "CMB_CASH_BUSINESS", "银行转存", payload={"event_type": "DEPOSIT"}
        )

        assert get_excluded_symbols(db, 1) == {"511880"}
        assert get_excluded_keys(db, 1) == {("511880", "A股")}
        assert get_cash_management_symbols(db, 1) == {"880013"}
        relistings = get_relistings(db, 1)
        assert relistings == [
            {
                "old_symbol": "01263",
                "old_market": "港股",
                "old_currency": "HKD",
                "new_symbol": "PCT",
                "new_market": "新加坡股",
                "new_currency": "SGD",
                "name": "柏能集团",
            }
        ]
        assert get_name_overrides(db, 1) == {("PCT", "新加坡股"): "柏能集团"}
        assert get_price_gap_exemptions(db, 1) == [
            ("01263", "港股", date(2026, 1, 9), None)
        ]
        assert get_cmb_cash_business_map(db, 1) == {"银行转存": "DEPOSIT"}
        # 用户隔离
        assert get_excluded_symbols(db, 2) == set()
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def _txn(db, symbol, market, txn_date, currency="HKD"):
    txn = Transaction(
        user_id=1, broker_account_id=None, symbol=symbol, name=symbol, market=market,
        transaction_type="BUY", quantity=Decimal("100"), price=Decimal("10"),
        fee=Decimal("0"), transaction_date=txn_date, currency=currency,
    )
    db.add(txn)
    db.commit()
    return txn


def test_price_gap_exemption_clamps_sync_targets():
    """豁免收窄同步目标：尾部开放豁免钳终点；全覆盖豁免整体丢弃目标。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        _txn(db, "01263", "港股", date(2025, 10, 9))
        # 已清仓（无持仓行）→ 目标终点=最后交易日；先看无豁免时的基线
        base = get_history_sync_targets(db, 1, end_date=date(2026, 7, 1))
        assert base["targets"][0]["end_date"] == date(2025, 10, 9)

        _txn(db, "01263", "港股", date(2026, 5, 3))  # 拉长到缺口区间内
        no_exemption = get_history_sync_targets(db, 1, end_date=date(2026, 7, 1))
        assert no_exemption["targets"][0]["end_date"] == date(2026, 5, 3)

        seed_security_rule(
            db, 1, "PRICE_GAP_EXEMPTION", "01263", "港股",
            payload={"start_date": "2026-01-09", "end_date": None},
        )
        clamped = get_history_sync_targets(db, 1, end_date=date(2026, 7, 1))
        assert clamped["targets"][0]["end_date"] == date(2026, 1, 8)

        # 头部豁免钳起点
        seed_security_rule(
            db, 1, "PRICE_GAP_EXEMPTION", "123266", "A股",
            payload={"start_date": "2026-03-26", "end_date": "2026-04-06"},
        )
        _txn(db, "123266", "A股", date(2026, 3, 26), currency="CNY")
        _txn(db, "123266", "A股", date(2026, 4, 9), currency="CNY")
        result = get_history_sync_targets(db, 1, end_date=date(2026, 7, 1))
        bond = next(t for t in result["targets"] if t["symbol"] == "123266")
        assert bond["start_date"] == date(2026, 4, 7)

        # 全区间豁免 → 目标消失
        seed_security_rule(
            db, 1, "PRICE_GAP_EXEMPTION", "900926", "B股",
            payload={"start_date": "2020-01-01", "end_date": None},
        )
        _txn(db, "900926", "B股", date(2026, 6, 1), currency="USD")
        result = get_history_sync_targets(db, 1, end_date=date(2026, 7, 1))
        assert all(t["symbol"] != "900926" for t in result["targets"])
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


@pytest.mark.anyio
async def test_security_rules_api_crud_and_validation(monkeypatch):
    from app.core.security import get_password_hash
    from app.models.user import User

    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    user = db.query(User).filter(User.username == "demo").one()
    original = user.hashed_password
    user.hashed_password = get_password_hash("rules-api-password")
    db.commit()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token_resp = await client.post(
                "/api/auth/token",
                json={"username": "demo", "password": "rules-api-password"},
            )
            auth = {"Authorization": f"Bearer {token_resp.json()['access_token']}"}

            created = await client.post(
                "/api/security-rules",
                headers=auth,
                json={
                    "rule_type": "RELISTING",
                    "symbol": "01263",
                    "market": "港股",
                    "payload": PCT_RELISTING_PAYLOAD,
                },
            )
            assert created.status_code == 201

            # 同键冲突 409
            dup = await client.post(
                "/api/security-rules",
                headers=auth,
                json={
                    "rule_type": "RELISTING",
                    "symbol": "01263",
                    "market": "港股",
                    "payload": PCT_RELISTING_PAYLOAD,
                },
            )
            assert dup.status_code == 409

            # payload 判别校验：RELISTING 缺字段 422
            bad = await client.post(
                "/api/security-rules",
                headers=auth,
                json={"rule_type": "RELISTING", "symbol": "01264", "market": "港股"},
            )
            assert bad.status_code == 422
            # 未知事件类型 422
            bad2 = await client.post(
                "/api/security-rules",
                headers=auth,
                json={
                    "rule_type": "CMB_CASH_BUSINESS",
                    "symbol": "银行转存",
                    "payload": {"event_type": "NOPE"},
                },
            )
            assert bad2.status_code == 422

            listed = await client.get(
                "/api/security-rules", headers=auth, params={"rule_type": "RELISTING"}
            )
            assert listed.status_code == 200
            rows = listed.json()
            assert len(rows) == 1
            assert rows[0]["payload"]["new_symbol"] == "PCT"

            deleted = await client.delete(
                f"/api/security-rules/{rows[0]['id']}", headers=auth
            )
            assert deleted.status_code == 204

            # 兼容路由仍工作（EXCLUDE 视图）
            excluded = await client.post(
                "/api/excluded-securities",
                headers=auth,
                json={"symbol": "511880", "market": "A股"},
            )
            assert excluded.status_code == 201
            via_rules = await client.get(
                "/api/security-rules", headers=auth, params={"rule_type": "EXCLUDE"}
            )
            assert [r["symbol"] for r in via_rules.json()] == ["511880"]
    finally:
        user.hashed_password = original
        db.commit()
        reset_tables(db, RESET_MODELS)
        db.close()


def test_ibkr_import_respects_exclude_rules(monkeypatch):
    """IBKR 导入消费 EXCLUDE 规则：命中标的只归档不建交易，且既有孤儿
    来源行（owner 删除交易留下的，如 FXE）不再阻断重导。"""

    from app.models.ibkr_activity_flow import IbkrActivityFlow
    from app.services import ibkr_activity_importer as ibkr
    from app.services.ibkr_activity_importer import import_ibkr_activity

    monkeypatch.setattr(ibkr, "lookup_tushare_security_name", lambda s, m: None)
    db = SessionLocal()
    reset_tables(db, (SecurityRule, IbkrActivityFlow, Holding, Transaction, BrokerAccount))
    try:
        account = BrokerAccount(
            user_id=1, broker="IBKR", account_name="IBKR 排除测试",
            account_number_masked="****7968", base_currency="USD",
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        seed_security_rule(db, 1, "EXCLUDE", "FXE", "美股",
                           note="行权衍生持仓，只做期权不留股票")

        csv_rows = (
            "Transaction History,Data,2026-03-23,U***67968,INVESCO EURO,买,FXE,"
            "200.0,107.226,USD,-21445.2,-1.0,-21446.2",
            "Transaction History,Data,2026-05-07,U***67968,CNOOC LTD-H,买,883,"
            "1000.0,27.36,HKD,-3493.05,-2.79,-3499.52",
        )
        header = "\n".join([
            "Statement,Header,域名称,域值",
            "总结,Header,域名称,域值",
            "总结,Data,基础货币,USD",
            "Transaction History,Header,日期,账户,说明,交易类型,代码,数量,价格,"
            "Price Currency,总额,佣金,净额",
        ])
        contents = (header + "\n" + "\n".join(csv_rows) + "\n").encode()

        # 预置孤儿来源行：与上传的 FXE 行同 hash、无 canonical 链接（owner 删过交易）
        parsed, _, _, _ = ibkr.parse_rows(contents, "orphan-probe.csv")
        fxe_hash = next(f.row_hash for f in parsed if f.raw_symbol == "FXE")
        orphan = ibkr.create_ibkr_activity_flow(
            user_id=1, filename="legacy.csv",
            flow=next(f for f in parsed if f.raw_symbol == "FXE"),
            broker_account_id=account.id,
        )
        db.add(orphan)
        db.commit()

        result = import_ibkr_activity(
            db, 1, contents, "ibkr-exclude.csv", broker_account_id=account.id
        )

        assert result["errors"] == []  # 孤儿不再阻断
        assert result["skipped_excluded_rows"] == 1
        assert result["imported_transactions"] == 1  # 只有 00883
        assert db.query(Transaction).filter(Transaction.symbol == "FXE").count() == 0
        # 孤儿行按 hash 判重，不产生第二份归档
        assert (
            db.query(IbkrActivityFlow)
            .filter(IbkrActivityFlow.row_hash == fxe_hash)
            .count()
            == 1
        )
        assert result["batch_status"] == "COMPLETED"  # 排除属预期跳过
    finally:
        reset_tables(db, (SecurityRule, IbkrActivityFlow, Holding, Transaction, BrokerAccount))
        db.close()


def test_ibkr_excluded_archive_keeps_real_skip_reason(monkeypatch):
    """检视回归：排除行的归档 skip_reason 必须是 excluded 而非 option。"""
    from app.models.ibkr_activity_flow import IbkrActivityFlow
    from app.services import ibkr_activity_importer as ibkr
    from app.services.ibkr_activity_importer import import_ibkr_activity

    monkeypatch.setattr(ibkr, "lookup_tushare_security_name", lambda s, m: None)
    db = SessionLocal()
    models = (SecurityRule, IbkrActivityFlow, Holding, Transaction, BrokerAccount)
    reset_tables(db, models)
    try:
        account = BrokerAccount(
            user_id=1, broker="IBKR", account_name="IBKR 审计",
            account_number_masked="****7968", base_currency="USD",
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        seed_security_rule(db, 1, "EXCLUDE", "FXE", "美股")
        contents = (
            "Statement,Header,域名称,域值\n总结,Header,域名称,域值\n"
            "总结,Data,基础货币,USD\n"
            "Transaction History,Header,日期,账户,说明,交易类型,代码,数量,价格,"
            "Price Currency,总额,佣金,净额\n"
            "Transaction History,Data,2026-03-23,U***7968,INVESCO EURO,买,FXE,"
            "200.0,107.226,USD,-21445.2,-1.0,-21446.2\n"
        ).encode()
        import_ibkr_activity(db, 1, contents, "audit.csv", broker_account_id=account.id)
        archived = db.query(IbkrActivityFlow).one()
        assert archived.skip_reason == "excluded"
    finally:
        reset_tables(db, models)
        db.close()


@pytest.mark.anyio
async def test_cmb_rule_cannot_bypass_uniqueness_via_market():
    """检视回归：CMB 业务映射拒绝 market 字段，同业务名不能多规则并存。"""
    from app.core.security import get_password_hash
    from app.models.user import User

    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    user = db.query(User).filter(User.username == "demo").one()
    original = user.hashed_password
    user.hashed_password = get_password_hash("cmb-market-password")
    db.commit()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token_resp = await client.post(
                "/api/auth/token",
                json={"username": "demo", "password": "cmb-market-password"},
            )
            auth = {"Authorization": f"Bearer {token_resp.json()['access_token']}"}
            ok = await client.post(
                "/api/security-rules", headers=auth,
                json={"rule_type": "CMB_CASH_BUSINESS", "symbol": "银行转存",
                      "payload": {"event_type": "DEPOSIT"}},
            )
            assert ok.status_code == 201
            # 带 market 直接 422（而非借不同 market 生成第二条）
            bypass = await client.post(
                "/api/security-rules", headers=auth,
                json={"rule_type": "CMB_CASH_BUSINESS", "symbol": "银行转存",
                      "market": "A股", "payload": {"event_type": "WITHDRAWAL"}},
            )
            assert bypass.status_code == 422
            # FX_IN 不在 CMB 允许类型
            fx = await client.post(
                "/api/security-rules", headers=auth,
                json={"rule_type": "CMB_CASH_BUSINESS", "symbol": "外汇兑换",
                      "payload": {"event_type": "FX_IN"}},
            )
            assert fx.status_code == 422
            # 畸形 payload 一律 422 而非 500
            for bad_payload in (
                {"rule_type": "PRICE_GAP_EXEMPTION", "symbol": "X", "market": "A股",
                 "payload": {"start_date": 123}},
                {"rule_type": "PRICE_GAP_EXEMPTION", "symbol": "X", "market": "A股",
                 "payload": {"start_date": "not-a-date"}},
                {"rule_type": "RELISTING", "symbol": "X", "market": "A股",
                 "payload": {"new_symbol": "Y", "new_market": [], "new_currency": "SGD",
                             "old_currency": "HKD"}},
                {"rule_type": "CMB_CASH_BUSINESS", "symbol": "怪业务",
                 "payload": {"event_type": {}}},
            ):
                resp = await client.post("/api/security-rules", headers=auth, json=bad_payload)
                assert resp.status_code == 422, (bad_payload, resp.status_code, resp.text)
    finally:
        user.hashed_password = original
        db.commit()
        reset_tables(db, RESET_MODELS)
        db.close()


@pytest.mark.anyio
async def test_nested_payload_fields_are_normalized():
    """检视回归：嵌套字段与外层同规范——空白 422、小写代码入库即大写。"""
    from app.core.security import get_password_hash
    from app.models.user import User

    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    user = db.query(User).filter(User.username == "demo").one()
    original = user.hashed_password
    user.hashed_password = get_password_hash("normalize-password")
    db.commit()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token_resp = await client.post(
                "/api/auth/token",
                json={"username": "demo", "password": "normalize-password"},
            )
            auth = {"Authorization": f"Bearer {token_resp.json()['access_token']}"}

            # 空白 new_symbol / 空白覆盖名 → 422
            for bad in (
                {"rule_type": "RELISTING", "symbol": "01263", "market": "港股",
                 "payload": {"new_symbol": "   ", "new_market": "新加坡股",
                             "new_currency": "SGD", "old_currency": "HKD"}},
                {"rule_type": "NAME_OVERRIDE", "symbol": "PCT", "market": "新加坡股",
                 "payload": {"name": "   "}},
            ):
                resp = await client.post("/api/security-rules", headers=auth, json=bad)
                assert resp.status_code == 422, (bad, resp.text)

            # 小写代码/币种入库即规范化为大写；市场 strip 后校验
            created = await client.post(
                "/api/security-rules", headers=auth,
                json={"rule_type": "RELISTING", "symbol": "01263", "market": "港股",
                      "payload": {"new_symbol": " pct ", "new_market": " 新加坡股 ",
                                  "new_currency": " sgd", "old_currency": "hkd "}},
            )
            assert created.status_code == 201
            payload = created.json()["payload"]
            assert payload["new_symbol"] == "PCT"
            assert payload["new_currency"] == "SGD"
            assert payload["old_currency"] == "HKD"
            assert payload["new_market"] == "新加坡股"
    finally:
        user.hashed_password = original
        db.commit()
        reset_tables(db, RESET_MODELS)
        db.close()
