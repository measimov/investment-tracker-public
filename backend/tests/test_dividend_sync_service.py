"""分红公告同步服务：div_proc 过滤、登记日推算、判重、幂等 upsert、事件落库。

全部 mock fetch_* 层（monkeypatch），不打真实 Tushare。
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.database import SessionLocal
from app.models.background_job import BackgroundJob
from app.models.corporate_action import CorporateAction
from app.models.corporate_action_suggestion import CorporateActionSuggestion
from app.models.holding import Holding
from app.models.security_event import SecurityEvent
from app.models.security_rule import SecurityRule
from app.models.transaction import Transaction
from app.services import dividend_sync_service as svc
from app.services.portfolio.semantics import bonus_share_factor

from .helpers import add_transaction, make_account, reset_tables

RESET_MODELS = [
    CorporateActionSuggestion,
    SecurityEvent,
    CorporateAction,
    Holding,
    Transaction,
    SecurityRule,
]

TODAY = date.today()
RECENT_EX = TODAY - timedelta(days=30)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        reset_tables(session, RESET_MODELS)
        session.query(BackgroundJob).filter(
            BackgroundJob.job_type == "dividend_sync"
        ).delete()
        session.commit()
        yield session
        session.rollback()
        reset_tables(session, RESET_MODELS)
    finally:
        session.close()


def _announcement(**overrides):
    row = {
        "end_date": date(2025, 12, 31),
        "ann_date": RECENT_EX - timedelta(days=20),
        "div_proc": "实施",
        "stk_div": None,
        "cash_div": Decimal("0.9"),
        "cash_div_tax": Decimal("1.0"),
        "record_date": RECENT_EX - timedelta(days=1),
        "ex_date": RECENT_EX,
        "pay_date": RECENT_EX + timedelta(days=1),
    }
    row.update(overrides)
    return row


def _seed_holding(db, symbol="600036", market="A股", quantity=Decimal("1000")):
    db.add(Holding(
        user_id=1, symbol=symbol, name="招商银行", market=market,
        quantity=quantity, avg_cost=Decimal("30"), total_cost=quantity * 30,
        currency="CNY",
    ))
    db.commit()


def _patch_fetchers(monkeypatch, dividends=None, disclosures=None, floats=None):
    monkeypatch.setattr(
        svc, "fetch_dividend_announcements", lambda s, m: list(dividends or [])
    )
    monkeypatch.setattr(svc, "fetch_disclosure_dates", lambda s, m: list(disclosures or []))
    monkeypatch.setattr(svc, "fetch_share_floats", lambda s, m: list(floats or []))


# ---------------------------------------------------------------------------
# div_proc 过滤与登记日持仓
# ---------------------------------------------------------------------------


def test_only_implemented_announcements_become_suggestions(db, monkeypatch):
    """预案/股东提议不产生建议；只有"实施"且有除权日的行入选。"""
    _seed_holding(db)
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_date=date(2024, 1, 10), quantity=Decimal("1000"))
    db.commit()
    _patch_fetchers(monkeypatch, dividends=[
        _announcement(),
        _announcement(div_proc="预案", ex_date=RECENT_EX + timedelta(days=60)),
        _announcement(div_proc="股东提议", ex_date=None),
        _announcement(div_proc="实施", ex_date=None),  # 无除权日的实施行也跳过
    ])

    result = svc.sync_dividends_for_user(db, 1)

    assert result["announcements"] == 1
    suggestions = db.query(CorporateActionSuggestion).all()
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s.status == "NEW"
    assert s.quantity_basis == "per_account"
    # 登记日持仓 1000 股 × 每股税前 1.0
    assert Decimal(str(s.record_date_quantity)) == Decimal("1000")
    assert Decimal(str(s.estimated_total_dividend)) == Decimal("1000")


def test_position_bought_after_record_date_is_skipped(db, monkeypatch):
    _seed_holding(db)
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_date=RECENT_EX + timedelta(days=5))
    db.commit()
    _patch_fetchers(monkeypatch, dividends=[_announcement()])

    result = svc.sync_dividends_for_user(db, 1)

    assert result["skipped_no_position"] == 1
    assert db.query(CorporateActionSuggestion).count() == 0


def test_record_date_quantity_keeps_account_breakdown(db, monkeypatch):
    """多账户分桶保留归属；record_date 缺失时回退 ex_date−1。"""
    _seed_holding(db)
    a1 = make_account(db, "券商A")
    a2 = make_account(db, "券商B")
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    broker_account_id=a1.id, quantity=Decimal("600"),
                    transaction_date=date(2024, 1, 10))
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    broker_account_id=a2.id, quantity=Decimal("400"),
                    transaction_date=date(2024, 2, 10))
    db.commit()

    breakdown, basis = svc.quantity_on_record_date(
        db, 1, "600036", "A股", RECENT_EX - timedelta(days=1)
    )
    assert basis == "per_account"
    assert breakdown == {a1.id: Decimal("600"), a2.id: Decimal("400")}


def test_account_replay_error_degrades_to_merged(db, monkeypatch):
    """归属矛盾（跨账户超卖）降级 merged，数量总和仍可信（NULL 单桶）。"""
    _seed_holding(db)
    a1 = make_account(db, "券商A")
    a2 = make_account(db, "券商B")
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    broker_account_id=a1.id, quantity=Decimal("1000"),
                    transaction_date=date(2024, 1, 10))
    # 账户 B 无持仓却卖出 → per-account 重放矛盾
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    broker_account_id=a2.id, transaction_type="SELL",
                    quantity=Decimal("200"), transaction_date=date(2024, 3, 1))
    db.commit()

    breakdown, basis = svc.quantity_on_record_date(
        db, 1, "600036", "A股", RECENT_EX - timedelta(days=1)
    )
    assert basis == "merged"
    assert breakdown == {None: Decimal("800")}


def test_sold_out_position_still_generates_suggestion(db, monkeypatch):
    """[评审回归] 登记日持有、随后卖清、当前持仓为零 → 仍生成建议。"""
    # 无 Holding 行：买入在登记日前、清仓卖出在除权日后
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_date=date(2024, 1, 10), quantity=Decimal("1000"))
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_type="SELL", quantity=Decimal("1000"),
                    transaction_date=RECENT_EX + timedelta(days=3))
    db.commit()
    _patch_fetchers(monkeypatch, dividends=[_announcement()])

    result = svc.sync_dividends_for_user(db, 1)

    assert result["symbols_scanned"] == 1
    s = db.query(CorporateActionSuggestion).one()
    assert s.status == "NEW"
    assert Decimal(str(s.record_date_quantity)) == Decimal("1000")


def test_multi_account_entitlement_splits_into_per_account_suggestions(db, monkeypatch):
    """[评审回归] 多账户权益 → 每账户一条建议；接受后归属与金额之和正确。"""
    from app.models.user import User

    _seed_holding(db)
    a1 = make_account(db, "券商A", commit=True)
    a2 = make_account(db, "券商B", commit=True)
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    broker_account_id=a1.id, quantity=Decimal("600"),
                    transaction_date=date(2024, 1, 10))
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    broker_account_id=a2.id, quantity=Decimal("400"),
                    transaction_date=date(2024, 2, 10))
    db.commit()
    _patch_fetchers(monkeypatch, dividends=[_announcement()])

    svc.sync_dividends_for_user(db, 1)

    suggestions = db.query(CorporateActionSuggestion).order_by(
        CorporateActionSuggestion.broker_account_id
    ).all()
    assert [(s.broker_account_id, Decimal(str(s.record_date_quantity)))
            for s in suggestions] == [(a1.id, Decimal("600")), (a2.id, Decimal("400"))]

    user = db.query(User).filter(User.id == 1).one()
    actions = [svc.accept_suggestion(db, user, s.id, {}) for s in suggestions]
    assert [(a.broker_account_id, Decimal(str(a.total_dividend))) for a in actions] == [
        (a1.id, Decimal("600")),
        (a2.id, Decimal("400")),
    ]
    total = sum(Decimal(str(a.total_dividend)) for a in actions)
    assert total == Decimal("1000")  # 金额之和 = 总权益 × 每股税前 1.0


# ---------------------------------------------------------------------------
# 判重（宽窗：导入器 ex_date 实为到账日）
# ---------------------------------------------------------------------------


def _importer_style_dividend(db, *, lag_days=10, total=Decimal("1000"), tax=Decimal("0")):
    """模拟导入器写入的 CASH_DIVIDEND：ex_date=到账日（滞后），税前全额入账。"""
    action = CorporateAction(
        user_id=1, symbol="600036", market="A股", action_type="CASH_DIVIDEND",
        ex_date=RECENT_EX + timedelta(days=lag_days),
        payment_date=RECENT_EX + timedelta(days=lag_days),
        total_dividend=total, tax_withheld=tax, net_dividend=total - tax,
        currency="CNY",
    )
    db.add(action)
    db.commit()
    return action


def test_importer_recorded_dividend_matches_within_window(db, monkeypatch):
    _seed_holding(db)
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_date=date(2024, 1, 10), quantity=Decimal("1000"))
    recorded = _importer_style_dividend(db, lag_days=10, total=Decimal("1000"))
    _patch_fetchers(monkeypatch, dividends=[_announcement()])

    result = svc.sync_dividends_for_user(db, 1)

    assert result["matched"] == 1 and result["new"] == 0
    s = db.query(CorporateActionSuggestion).one()
    assert s.status == "MATCHED"
    assert s.matched_corporate_action_id == recorded.id
    assert "amount_diff" not in (s.match_detail or {})


def test_amount_over_tolerance_is_matched_with_diff(db, monkeypatch):
    _seed_holding(db)
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_date=date(2024, 1, 10), quantity=Decimal("1000"))
    _importer_style_dividend(db, lag_days=10, total=Decimal("900"))  # 差 10% > 容差
    _patch_fetchers(monkeypatch, dividends=[_announcement()])

    svc.sync_dividends_for_user(db, 1)

    s = db.query(CorporateActionSuggestion).one()
    assert s.status == "MATCHED"
    assert s.match_detail["amount_diff"] == pytest.approx(100.0)


def test_match_is_scoped_to_account(db, monkeypatch):
    """账户 A 已录分红不代表账户 B 已入账：A=MATCHED、B=NEW。"""
    _seed_holding(db)
    a1 = make_account(db, "券商A", commit=True)
    a2 = make_account(db, "券商B", commit=True)
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    broker_account_id=a1.id, quantity=Decimal("600"),
                    transaction_date=date(2024, 1, 10))
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    broker_account_id=a2.id, quantity=Decimal("400"),
                    transaction_date=date(2024, 2, 10))
    # 仅账户 A 的分红已由导入器入账
    db.add(CorporateAction(
        user_id=1, symbol="600036", market="A股", action_type="CASH_DIVIDEND",
        broker_account_id=a1.id, ex_date=RECENT_EX + timedelta(days=10),
        payment_date=RECENT_EX + timedelta(days=10),
        total_dividend=Decimal("600"), tax_withheld=Decimal("0"),
        net_dividend=Decimal("600"), currency="CNY",
    ))
    db.commit()
    _patch_fetchers(monkeypatch, dividends=[_announcement()])

    svc.sync_dividends_for_user(db, 1)

    by_account = {
        s.broker_account_id: s.status
        for s in db.query(CorporateActionSuggestion).all()
    }
    assert by_account == {a1.id: "MATCHED", a2.id: "NEW"}


def test_record_outside_window_stays_new(db, monkeypatch):
    _seed_holding(db)
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_date=date(2024, 1, 10), quantity=Decimal("1000"))
    # 到账日滞后超出窗口（pay_date+30d 之外）
    _importer_style_dividend(db, lag_days=45)
    _patch_fetchers(monkeypatch, dividends=[_announcement()])

    result = svc.sync_dividends_for_user(db, 1)

    assert result["new"] == 1 and result["matched"] == 0
    assert db.query(CorporateActionSuggestion).one().status == "NEW"


# ---------------------------------------------------------------------------
# 送转与金额语义
# ---------------------------------------------------------------------------


def test_stock_dividend_ratio_is_parseable_by_semantics(db, monkeypatch):
    """送转建议的 distribution_ratio 必须能被 bonus_share_factor 解析。"""
    _seed_holding(db)
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_date=date(2024, 1, 10), quantity=Decimal("1000"))
    db.commit()
    _patch_fetchers(monkeypatch, dividends=[
        _announcement(cash_div=None, cash_div_tax=None, stk_div=Decimal("0.35")),
    ])

    svc.sync_dividends_for_user(db, 1)
    s = db.query(CorporateActionSuggestion).one()
    assert s.action_type == "STOCK_DIVIDEND"
    # 送转是比例行动（作用于所有账户桶）→ 账户无关单条建议
    assert s.broker_account_id is None

    from app.models.user import User
    user = db.query(User).filter(User.id == 1).one()
    action = svc.accept_suggestion(db, user, s.id, {})
    # 10:3.5 → 每股因子 1.35
    factor = bonus_share_factor(action, Decimal("1000"))
    assert factor == Decimal("1.35")
    # 同事务已重算持仓：1000 → 1350
    holding = db.query(Holding).filter(
        Holding.symbol == "600036", Holding.market == "A股"
    ).all()
    assert sum(Decimal(str(h.quantity)) for h in holding) == Decimal("1350")


def test_cash_and_stock_in_one_announcement_split_into_two(db, monkeypatch):
    _seed_holding(db)
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_date=date(2024, 1, 10), quantity=Decimal("1000"))
    db.commit()
    _patch_fetchers(monkeypatch, dividends=[
        _announcement(stk_div=Decimal("0.2")),
    ])

    svc.sync_dividends_for_user(db, 1)
    types = {s.action_type for s in db.query(CorporateActionSuggestion).all()}
    assert types == {"CASH_DIVIDEND", "STOCK_DIVIDEND"}


def test_accept_amounts_align_with_cash_dividend_amounts(db, monkeypatch):
    """接受后 gross/tax/net 与统计层 cash_dividend_amounts 口径闭合。"""
    from app.models.user import User
    from app.services.statistics_service import cash_dividend_amounts

    _seed_holding(db)
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_date=date(2024, 1, 10), quantity=Decimal("1000"))
    db.commit()
    _patch_fetchers(monkeypatch, dividends=[_announcement()])
    svc.sync_dividends_for_user(db, 1)

    s = db.query(CorporateActionSuggestion).one()
    user = db.query(User).filter(User.id == 1).one()
    # override：按券商实际到账改税额
    action = svc.accept_suggestion(db, user, s.id, {"tax_withheld": Decimal("100")})

    gross, tax, net = cash_dividend_amounts(action)
    assert gross == Decimal("1000")
    assert tax == Decimal("100")
    assert net == Decimal("900")
    db.refresh(s)
    assert s.status == "ACCEPTED"
    assert s.created_corporate_action_id == action.id

    # 重复接受 → SuggestionStateError
    with pytest.raises(svc.SuggestionStateError):
        svc.accept_suggestion(db, user, s.id, {})


def test_matched_suggestion_cannot_be_accepted(db, monkeypatch):
    """[评审回归] MATCHED 已有账本记录，接受即双计 → 拒绝（精确与金额差匹配同理）。"""
    from app.models.user import User

    _seed_holding(db)
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_date=date(2024, 1, 10), quantity=Decimal("1000"))
    _importer_style_dividend(db, lag_days=10, total=Decimal("900"))  # 金额差匹配
    _patch_fetchers(monkeypatch, dividends=[_announcement()])
    svc.sync_dividends_for_user(db, 1)

    s = db.query(CorporateActionSuggestion).one()
    assert s.status == "MATCHED"
    user = db.query(User).filter(User.id == 1).one()
    before = db.query(CorporateAction).count()
    with pytest.raises(svc.SuggestionStateError):
        svc.accept_suggestion(db, user, s.id, {})
    assert db.query(CorporateAction).count() == before


def test_tax_exceeding_gross_is_rejected(db, monkeypatch):
    """[评审回归] 税额 > 总额 → 拒绝，且不落任何 CorporateAction。"""
    from app.models.user import User

    _seed_holding(db)
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_date=date(2024, 1, 10), quantity=Decimal("1000"))
    db.commit()
    _patch_fetchers(monkeypatch, dividends=[_announcement()])
    svc.sync_dividends_for_user(db, 1)

    s = db.query(CorporateActionSuggestion).one()
    user = db.query(User).filter(User.id == 1).one()
    with pytest.raises(svc.SuggestionStateError):
        svc.accept_suggestion(db, user, s.id, {"tax_withheld": Decimal("1200")})
    db.rollback()
    assert db.query(CorporateAction).count() == 0
    assert db.query(CorporateActionSuggestion).one().status == "NEW"


def test_concurrent_accept_creates_single_action(db, monkeypatch):
    """[评审回归] 双会话并发接受：恰好一条入账、另一方 409 级错误。"""
    import threading

    from app.database import SessionLocal
    from app.models.user import User

    _seed_holding(db)
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_date=date(2024, 1, 10), quantity=Decimal("1000"))
    db.commit()
    _patch_fetchers(monkeypatch, dividends=[_announcement()])
    svc.sync_dividends_for_user(db, 1)
    suggestion_id = db.query(CorporateActionSuggestion).one().id

    outcomes: list = []
    barrier = threading.Barrier(2)

    def worker():
        session = SessionLocal()
        try:
            user = session.query(User).filter(User.id == 1).one()
            barrier.wait(timeout=10)
            action = svc.accept_suggestion(session, user, suggestion_id, {})
            outcomes.append(("ok", action.id))
        except svc.SuggestionStateError as exc:
            session.rollback()
            outcomes.append(("conflict", str(exc)))
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(kind for kind, _ in outcomes) == ["conflict", "ok"]
    assert (
        db.query(CorporateAction).filter(CorporateAction.symbol == "600036").count() == 1
    )


def test_concurrent_accept_vs_ignore_never_leaves_inconsistent_state(db, monkeypatch):
    """[评审回归] accept 与 ignore 竞态：恰一方成功，绝不出现
    "已创建 action 却仍可接受/已忽略"的矛盾态。"""
    import threading

    from app.database import SessionLocal
    from app.models.user import User

    _seed_holding(db)
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_date=date(2024, 1, 10), quantity=Decimal("1000"))
    db.commit()
    _patch_fetchers(monkeypatch, dividends=[_announcement()])
    svc.sync_dividends_for_user(db, 1)
    suggestion_id = db.query(CorporateActionSuggestion).one().id

    outcomes: list = []
    barrier = threading.Barrier(2)

    def accept_worker():
        session = SessionLocal()
        try:
            user = session.query(User).filter(User.id == 1).one()
            barrier.wait(timeout=10)
            svc.accept_suggestion(session, user, suggestion_id, {})
            outcomes.append("accept_ok")
        except svc.SuggestionStateError:
            session.rollback()
            outcomes.append("accept_conflict")
        finally:
            session.close()

    def ignore_worker():
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            svc.ignore_suggestion(session, 1, suggestion_id)
            outcomes.append("ignore_ok")
        except svc.SuggestionStateError:
            session.rollback()
            outcomes.append("ignore_conflict")
        finally:
            session.close()

    threads = [threading.Thread(target=accept_worker), threading.Thread(target=ignore_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(outcomes) in (
        ["accept_ok", "ignore_conflict"],
        ["accept_conflict", "ignore_ok"],
    )
    s = db.query(CorporateActionSuggestion).one()
    action_count = db.query(CorporateAction).filter(
        CorporateAction.symbol == "600036"
    ).count()
    if s.status == "ACCEPTED":
        assert action_count == 1 and s.created_corporate_action_id is not None
    else:
        assert s.status == "IGNORED"
        assert action_count == 0 and s.created_corporate_action_id is None


def test_accept_rechecks_ledger_inside_timeline_lock(db, monkeypatch):
    """[评审回归] 建议生成后账本新录入匹配分红 → accept 拒绝并转 MATCHED，
    不重复入账。"""
    from app.models.user import User

    _seed_holding(db)
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_date=date(2024, 1, 10), quantity=Decimal("1000"))
    db.commit()
    _patch_fetchers(monkeypatch, dividends=[_announcement()])
    svc.sync_dividends_for_user(db, 1)
    s = db.query(CorporateActionSuggestion).one()
    assert s.status == "NEW"

    # 建议生成之后，同一笔分红被券商导入入账（ex_date=到账日、税前全额）
    recorded = _importer_style_dividend(db, lag_days=10, total=Decimal("1000"))

    user = db.query(User).filter(User.id == 1).one()
    with pytest.raises(svc.SuggestionStateError, match="未重复入账"):
        svc.accept_suggestion(db, user, s.id, {})

    db.expire_all()
    s = db.query(CorporateActionSuggestion).one()
    assert s.status == "MATCHED"
    assert s.matched_corporate_action_id == recorded.id
    # 账本仍只有导入的那一条
    assert db.query(CorporateAction).filter(
        CorporateAction.symbol == "600036"
    ).count() == 1


def test_accept_recheck_uses_override_account(db, monkeypatch):
    """[评审回归] 重判重按最终 override 账户过滤：建议在账户 A、账本记录
    在账户 X、接受时 override 到 X → 拒绝，不在 X 上双计。"""
    from app.models.user import User

    _seed_holding(db)
    a_src = make_account(db, "券商A", commit=True)
    a_dst = make_account(db, "券商X", commit=True)
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    broker_account_id=a_src.id, quantity=Decimal("1000"),
                    transaction_date=date(2024, 1, 10))
    db.commit()
    _patch_fetchers(monkeypatch, dividends=[_announcement()])
    svc.sync_dividends_for_user(db, 1)
    s = db.query(CorporateActionSuggestion).one()
    assert s.broker_account_id == a_src.id and s.status == "NEW"

    # 账本里同笔分红已记在账户 X（导入器风格）
    recorded = CorporateAction(
        user_id=1, symbol="600036", market="A股", action_type="CASH_DIVIDEND",
        broker_account_id=a_dst.id, ex_date=RECENT_EX + timedelta(days=10),
        payment_date=RECENT_EX + timedelta(days=10),
        total_dividend=Decimal("1000"), tax_withheld=Decimal("0"),
        net_dividend=Decimal("1000"), currency="CNY",
    )
    db.add(recorded)
    db.commit()
    db.refresh(recorded)

    user = db.query(User).filter(User.id == 1).one()
    with pytest.raises(svc.SuggestionStateError, match="未重复入账"):
        svc.accept_suggestion(db, user, s.id, {"broker_account_id": a_dst.id})

    db.expire_all()
    s = db.query(CorporateActionSuggestion).one()
    assert s.status == "MATCHED"
    assert s.matched_corporate_action_id == recorded.id
    assert db.query(CorporateAction).filter(
        CorporateAction.symbol == "600036"
    ).count() == 1


def test_concurrent_accept_vs_ledger_insert(db, monkeypatch):
    """[评审回归] accept 与账本录入（导入/手工路径，持时间线锁）并发：
    录入先到 → accept 锁内重判重拒绝；accept 先到 → 正常入账。两分支
    均不出现建议状态与账本不一致。"""
    import threading

    from app.database import SessionLocal
    from app.models.user import User
    from app.services.holding_service import lock_security_timeline

    _seed_holding(db)
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_date=date(2024, 1, 10), quantity=Decimal("1000"))
    db.commit()
    _patch_fetchers(monkeypatch, dividends=[_announcement()])
    svc.sync_dividends_for_user(db, 1)
    suggestion_id = db.query(CorporateActionSuggestion).one().id

    outcomes: list = []
    barrier = threading.Barrier(2)

    def accept_worker():
        session = SessionLocal()
        try:
            user = session.query(User).filter(User.id == 1).one()
            barrier.wait(timeout=10)
            svc.accept_suggestion(session, user, suggestion_id, {})
            outcomes.append("accept_ok")
        except svc.SuggestionStateError:
            session.rollback()
            outcomes.append("accept_conflict")
        finally:
            session.close()

    def insert_worker():
        """模拟券商导入/手工录入路径：持时间线锁写入匹配分红。"""
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            lock_security_timeline(session, 1, "600036", "A股")
            session.add(CorporateAction(
                user_id=1, symbol="600036", market="A股",
                action_type="CASH_DIVIDEND",
                ex_date=RECENT_EX + timedelta(days=10),
                payment_date=RECENT_EX + timedelta(days=10),
                total_dividend=Decimal("1000"), tax_withheld=Decimal("0"),
                net_dividend=Decimal("1000"), currency="CNY",
            ))
            session.commit()
            outcomes.append("insert_ok")
        finally:
            session.close()

    threads = [threading.Thread(target=accept_worker), threading.Thread(target=insert_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert "insert_ok" in outcomes
    db.expire_all()
    s = db.query(CorporateActionSuggestion).one()
    action_count = db.query(CorporateAction).filter(
        CorporateAction.symbol == "600036"
    ).count()
    if "accept_ok" in outcomes:
        # accept 先取得时间线锁：正常入账；导入随后写入 → 两条（导入侧
        # 判重是导入器职责，非 accept 可控）
        assert s.status == "ACCEPTED" and s.created_corporate_action_id is not None
        assert action_count == 2
    else:
        # 录入先到：accept 锁内重判重命中 → MATCHED、仅导入一条
        assert s.status == "MATCHED" and s.created_corporate_action_id is None
        assert action_count == 1


def test_concurrent_event_upsert_is_atomic(db, monkeypatch):
    """[评审回归] 两个会话并发 upsert 同一全局事件：双方成功、最终一条。"""
    import threading

    from app.database import SessionLocal

    barrier = threading.Barrier(2)
    errors: list = []

    def worker():
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            svc.upsert_security_event(
                session, "600036", "A股", "SHARE_UNLOCK", TODAY + timedelta(days=30),
                "tushare-share_float", payload={"float_share": 100.0},
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 - 断言用
            session.rollback()
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert db.query(SecurityEvent).count() == 1


# ---------------------------------------------------------------------------
# 幂等重同步与规则剔除
# ---------------------------------------------------------------------------


def test_resync_is_idempotent_and_refreshes_revisions(db, monkeypatch):
    _seed_holding(db)
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_date=date(2024, 1, 10), quantity=Decimal("1000"))
    db.commit()
    _patch_fetchers(monkeypatch, dividends=[_announcement()])
    svc.sync_dividends_for_user(db, 1)
    assert db.query(CorporateActionSuggestion).count() == 1

    # 公告修订金额 → NEW 行刷新而非新增
    _patch_fetchers(monkeypatch, dividends=[_announcement(cash_div_tax=Decimal("1.2"))])
    result = svc.sync_dividends_for_user(db, 1)
    assert result["refreshed"] == 1
    s = db.query(CorporateActionSuggestion).one()
    assert Decimal(str(s.cash_div_pre_tax)) == Decimal("1.2")

    # IGNORED 不复活、不刷新
    s.status = "IGNORED"
    db.commit()
    _patch_fetchers(monkeypatch, dividends=[_announcement(cash_div_tax=Decimal("1.5"))])
    svc.sync_dividends_for_user(db, 1)
    s = db.query(CorporateActionSuggestion).one()
    assert s.status == "IGNORED"
    assert Decimal(str(s.cash_div_pre_tax)) == Decimal("1.2")

    # [评审回归] ACCEPTED 同样不被重同步覆盖回 NEW/MATCHED
    from app.models.user import User
    svc.restore_suggestion(db, 1, s.id)
    user = db.query(User).filter(User.id == 1).one()
    action = svc.accept_suggestion(db, user, s.id, {})
    _patch_fetchers(monkeypatch, dividends=[_announcement(cash_div_tax=Decimal("2.0"))])
    svc.sync_dividends_for_user(db, 1)
    s = db.query(CorporateActionSuggestion).one()
    assert s.status == "ACCEPTED"
    assert s.created_corporate_action_id == action.id
    assert Decimal(str(s.cash_div_pre_tax)) == Decimal("1.2")  # 值未被覆盖


def test_resync_removes_stale_suggestions(db, monkeypatch):
    """[评审回归] 账户桶消失/权益归零 → 旧 NEW 建议撤销；ACCEPTED 保留。"""
    _seed_holding(db)
    a1 = make_account(db, "券商A", commit=True)
    a2 = make_account(db, "券商B", commit=True)
    txn_b = add_transaction(db, symbol="600036", market="A股", currency="CNY",
                            broker_account_id=a2.id, quantity=Decimal("400"),
                            transaction_date=date(2024, 2, 10))
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    broker_account_id=a1.id, quantity=Decimal("600"),
                    transaction_date=date(2024, 1, 10))
    db.commit()
    txn_b_id = txn_b.id
    _patch_fetchers(monkeypatch, dividends=[_announcement()])
    svc.sync_dividends_for_user(db, 1)
    assert db.query(CorporateActionSuggestion).count() == 2

    # 用户纠正账本：删除 B 的登记日前买入 → B 不再享有权益
    db.query(Transaction).filter(Transaction.id == txn_b_id).delete()
    db.commit()
    result = svc.sync_dividends_for_user(db, 1)
    assert result["stale_removed"] == 1
    remaining = db.query(CorporateActionSuggestion).all()
    assert [(s.broker_account_id, s.status) for s in remaining] == [(a1.id, "NEW")]

    # 全部权益归零 → 剩余 NEW 也撤销，skipped 计数
    db.query(Transaction).delete()
    db.commit()
    result = svc.sync_dividends_for_user(db, 1)
    assert result["skipped_no_position"] == 1
    assert result["stale_removed"] == 1
    assert db.query(CorporateActionSuggestion).count() == 0


def test_resync_keeps_accepted_when_equity_disappears(db, monkeypatch):
    """已接受的建议是既成账本事实，权益归零重同步也不撤销。"""
    from app.models.user import User

    _seed_holding(db)
    txn = add_transaction(db, symbol="600036", market="A股", currency="CNY",
                          transaction_date=date(2024, 1, 10), quantity=Decimal("1000"))
    db.commit()
    txn_id = txn.id
    _patch_fetchers(monkeypatch, dividends=[_announcement()])
    svc.sync_dividends_for_user(db, 1)
    s = db.query(CorporateActionSuggestion).one()
    user = db.query(User).filter(User.id == 1).one()
    svc.accept_suggestion(db, user, s.id, {})

    db.query(Transaction).filter(Transaction.id == txn_id).delete()
    db.commit()
    result = svc.sync_dividends_for_user(db, 1)
    assert result["stale_removed"] == 0
    assert db.query(CorporateActionSuggestion).one().status == "ACCEPTED"


def test_excluded_and_cash_management_symbols_are_skipped(db, monkeypatch):
    _seed_holding(db, symbol="511880")
    _seed_holding(db, symbol="600036")
    db.add(SecurityRule(user_id=1, rule_type="EXCLUDE", symbol="600036", market="A股"))
    db.add(SecurityRule(user_id=1, rule_type="CASH_MANAGEMENT", symbol="511880",
                        market="A股"))
    db.commit()
    calls = []
    monkeypatch.setattr(
        svc, "fetch_dividend_announcements",
        lambda s, m: calls.append(s) or [],
    )

    result = svc.sync_dividends_for_user(db, 1)
    assert calls == []
    assert result["symbols_scanned"] == 0


def test_non_a_share_markets_are_reported_unsupported(db, monkeypatch):
    _seed_holding(db, symbol="00700", market="港股")
    _patch_fetchers(monkeypatch)

    result = svc.sync_dividends_for_user(db, 1)
    assert result["unsupported_markets"] == ["港股"]
    assert result["symbols_scanned"] == 0


def test_single_symbol_failure_does_not_abort(db, monkeypatch):
    _seed_holding(db, symbol="600036")
    _seed_holding(db, symbol="600519")

    def fetch(symbol, market):
        if symbol == "600036":
            raise RuntimeError("抱歉，您每分钟最多访问该接口1次")
        return [_announcement()]

    monkeypatch.setattr(svc, "fetch_dividend_announcements", fetch)
    monkeypatch.setattr(svc, "fetch_disclosure_dates", lambda s, m: [])
    monkeypatch.setattr(svc, "fetch_share_floats", lambda s, m: [])
    add_transaction(db, symbol="600519", market="A股", currency="CNY",
                    transaction_date=date(2024, 1, 10), quantity=Decimal("1000"))
    db.commit()

    result = svc.sync_dividends_for_user(db, 1)
    assert len(result["failed"]) == 1
    assert result["failed"][0]["symbol"] == "600036"
    assert db.query(CorporateActionSuggestion).count() == 1


# ---------------------------------------------------------------------------
# 事件落库
# ---------------------------------------------------------------------------


def test_events_upsert_and_plan_to_implemented_transition(db, monkeypatch):
    _seed_holding(db)
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_date=date(2024, 1, 10), quantity=Decimal("1000"))
    db.commit()
    future_ex = TODAY + timedelta(days=20)
    disclosure = TODAY + timedelta(days=40)
    unlock = TODAY + timedelta(days=60)

    plan_row = _announcement(
        div_proc="预案", ex_date=future_ex, record_date=None, pay_date=None
    )
    _patch_fetchers(
        monkeypatch,
        dividends=[plan_row],
        disclosures=[
            {"end_date": date(2026, 6, 30), "pre_date": disclosure, "actual_date": None},
            # 已实际披露的行不再是未来事件
            {"end_date": date(2026, 3, 31), "pre_date": TODAY - timedelta(days=10),
             "actual_date": TODAY - timedelta(days=8)},
        ],
        floats=[
            {"float_date": unlock, "float_share": Decimal("100"), "float_ratio": Decimal("1.5")},
            {"float_date": unlock, "float_share": Decimal("200"), "float_ratio": Decimal("3.0")},
        ],
    )

    result = svc.sync_dividends_for_user(db, 1)
    assert result["events_upserted"] == 3
    events = {e.event_type: e for e in db.query(SecurityEvent).all()}
    assert set(events) == {"DIVIDEND_PLAN", "EARNINGS_DISCLOSURE", "SHARE_UNLOCK"}
    assert events["SHARE_UNLOCK"].payload["float_share"] == 300.0
    assert events["SHARE_UNLOCK"].payload["batches"] == 2

    # 预案 → 实施：同除权日再同步，事件刷新不重复，且开始产生建议
    _patch_fetchers(
        monkeypatch,
        dividends=[_announcement(ex_date=future_ex,
                                 record_date=future_ex - timedelta(days=1))],
    )
    result2 = svc.sync_dividends_for_user(db, 1)
    assert result2["events_upserted"] == 0
    assert db.query(SecurityEvent).filter_by(event_type="DIVIDEND_PLAN").count() == 1
    assert db.query(CorporateActionSuggestion).count() == 1


# ---------------------------------------------------------------------------
# 周期入口
# ---------------------------------------------------------------------------


def test_periodic_entry_silent_without_flag_or_token(db, monkeypatch):
    from app.services import dividend_sync_jobs as jobs

    _seed_holding(db)
    monkeypatch.setattr(jobs.settings, "dividend_sync_periodic_enabled", False)
    assert jobs.enqueue_periodic_dividend_sync() == 0

    monkeypatch.setattr(jobs.settings, "dividend_sync_periodic_enabled", True)
    monkeypatch.setattr(jobs.settings, "tushare_token", "")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    assert jobs.enqueue_periodic_dividend_sync() == 0

    monkeypatch.setattr(jobs.settings, "tushare_token", "fake-token")
    assert jobs.enqueue_periodic_dividend_sync() == 1
    job = db.query(BackgroundJob).filter(
        BackgroundJob.job_type == "dividend_sync"
    ).one()
    assert job.status == "queued"


def test_periodic_entry_includes_sold_out_users(db, monkeypatch):
    """[评审回归] 无 Holding、仅有 lookback 内清仓交易的用户也会入队。"""
    from app.services import dividend_sync_jobs as jobs

    # 无任何 Holding 行：登记日前买入 + 近期清仓卖出
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_date=date(2024, 1, 10), quantity=Decimal("1000"))
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_type="SELL", quantity=Decimal("1000"),
                    transaction_date=TODAY - timedelta(days=10))
    db.commit()
    monkeypatch.setattr(jobs.settings, "dividend_sync_periodic_enabled", True)
    monkeypatch.setattr(jobs.settings, "tushare_token", "fake-token")

    assert jobs.enqueue_periodic_dividend_sync() == 1
    job = db.query(BackgroundJob).filter(
        BackgroundJob.job_type == "dividend_sync"
    ).one()
    assert job.user_id == 1
