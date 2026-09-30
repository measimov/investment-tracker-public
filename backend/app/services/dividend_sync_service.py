"""分红公告同步 → 建议表 + 标的事件表（绝不自动入账）。

按市场分派数据源：
- A/B 股：Tushare `dividend`（`div_proc="实施"` 白名单），需要 TUSHARE_TOKEN；
- 港股：披露易「股票發行人現金股息公告」表格（`hkex_dividend_source`，官方、免 token）。
  未配置 TUSHARE_TOKEN 时 A/B 股整体跳过并在结果里说明，港股照常同步。
美股仍以券商对账单导入为准。
建议入账只发生在用户显式"接受"时，与对账"报告不修复"哲学一致。

判重窗口刻意放宽：三个券商导入器写入的 CASH_DIVIDEND 的 ex_date 实为资金
到账日（滞后真实除权日），且到账金额常为税前全额（tax=0），因此按
[公告除权日−3天, 派息日+match_window] 的日期窗 + 税前口径金额容差匹配。
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from sqlalchemy import literal_column
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from ..config import settings
from ..core.logging import get_app_logger
from ..core.timeutil import local_today
from ..models.corporate_action import CorporateAction
from ..models.corporate_action_suggestion import CorporateActionSuggestion
from ..models.holding import Holding
from ..models.security_event import SecurityEvent
from ..models.security_price import SecurityPrice
from ..models.transaction import Transaction
from ..models.user import User
from . import hkex_dividend_source
from .background_job_store import JobOwnershipLostError
from .hk_adjustment_factors import recompute_hk_adj_factors
from .holding_service import (
    AccountReplayError,
    lock_record,
    lock_security_timeline,
    recalculate_holdings,
    replay_transactions_merged,
    replay_transactions_per_account,
)
from .security_rule_service import get_cash_management_symbols, get_excluded_keys
from .stock_price_service import (
    TushareEmptyResult,
    to_tushare_a_code,
    tushare_configured,
    tushare_query,
)

logger = get_app_logger(__name__)

TUSHARE_MARKETS = ("A股", "B股")
HKEX_MARKETS = ("港股",)
SUPPORTED_MARKETS = TUSHARE_MARKETS + HKEX_MARKETS

# 判重日期窗：既有记录 ex_date（实为到账日）允许早于公告除权日的回拨天数
MATCH_WINDOW_BEFORE_DAYS = 3
# 金额相对容差（1%）与绝对容差（1 元）：取较宽者
AMOUNT_RELATIVE_TOLERANCE = Decimal("0.01")
AMOUNT_ABSOLUTE_TOLERANCE = Decimal("1")

# 送转判重窗（送转记录通常按真实除权日录入）
STOCK_MATCH_WINDOW_DAYS = 7

# 港股表格清单检索起点：公告日早于除净日（末期股息约提前 2-3 个月），向前多看半年；
# 复权因子要全部历史，所以还会延伸到该标的最早一根价格，但不早于 EF001 表格出现之前
HK_FORM_LIST_LEAD_DAYS = 180
HK_FORM_EARLIEST = date(2021, 1, 1)


def _parse_ts_date(value: Any) -> Optional[date]:
    """Tushare 日期字符串（YYYYMMDD）→ date；NaN/None/空串 → None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except (ValueError, IndexError):
        return None


def _parse_ts_number(value: Any) -> Optional[Decimal]:
    """Tushare 数值 → Decimal；NaN（自身不等）/None → None。"""
    if value is None or value != value:
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def fetch_dividend_announcements(symbol: str, market: str) -> List[Dict[str, Any]]:
    """拉取单标的分红送股公告（含全部 div_proc 阶段），归一为 dict 列表。

    测试通过 monkeypatch 本函数注入构造数据；空数据的 ValueError 归一为空列表。
    """
    try:
        df = tushare_query("dividend", ts_code=to_tushare_a_code(symbol))
    except TushareEmptyResult:
        return []
    rows = []
    for raw in df.to_dict("records"):
        rows.append(
            {
                "end_date": _parse_ts_date(raw.get("end_date")),
                "ann_date": _parse_ts_date(raw.get("ann_date")),
                "div_proc": str(raw.get("div_proc") or "").strip(),
                "stk_div": _parse_ts_number(raw.get("stk_div")),
                "cash_div": _parse_ts_number(raw.get("cash_div")),
                "cash_div_tax": _parse_ts_number(raw.get("cash_div_tax")),
                "record_date": _parse_ts_date(raw.get("record_date")),
                "ex_date": _parse_ts_date(raw.get("ex_date")),
                "pay_date": _parse_ts_date(raw.get("pay_date")),
            }
        )
    return rows


def fetch_disclosure_dates(symbol: str, market: str) -> List[Dict[str, Any]]:
    """财报披露计划：未实际披露（actual_date 为空）的 pre_date 即未来事件。"""
    try:
        df = tushare_query("disclosure_date", ts_code=to_tushare_a_code(symbol))
    except TushareEmptyResult:
        return []
    rows = []
    for raw in df.to_dict("records"):
        rows.append(
            {
                "end_date": _parse_ts_date(raw.get("end_date")),
                "pre_date": _parse_ts_date(raw.get("pre_date")),
                "actual_date": _parse_ts_date(raw.get("actual_date")),
            }
        )
    return rows


def fetch_share_floats(symbol: str, market: str) -> List[Dict[str, Any]]:
    """限售解禁：按解禁日聚合（同日多股东合并为一条事件）。"""
    try:
        df = tushare_query("share_float", ts_code=to_tushare_a_code(symbol))
    except TushareEmptyResult:
        return []
    rows = []
    for raw in df.to_dict("records"):
        rows.append(
            {
                "float_date": _parse_ts_date(raw.get("float_date")),
                "float_share": _parse_ts_number(raw.get("float_share")),
                "float_ratio": _parse_ts_number(raw.get("float_ratio")),
            }
        )
    return rows


def quantity_on_record_date(
    db: Session,
    user_id: int,
    symbol: str,
    market: str,
    entitle_date: date,
) -> Tuple[Dict[Optional[int], Decimal], str]:
    """登记日（含当日）持仓推算：按账户桶返回 {broker_account_id: quantity}。

    归属矛盾降级为合并口径（quantity_basis='merged'，返回 {None: 总量}），
    与持仓重算的降级语义一致——数量总和仍然可信，只是无法按账户拆分。
    只返回数量 > 0 的桶。
    """
    transactions = (
        db.query(Transaction)
        .filter(
            Transaction.user_id == user_id,
            Transaction.symbol == symbol,
            Transaction.market == market,
            Transaction.transaction_date <= entitle_date,
        )
        .all()
    )
    corporate_actions = (
        db.query(CorporateAction)
        .filter(
            CorporateAction.user_id == user_id,
            CorporateAction.symbol == symbol,
            CorporateAction.market == market,
            CorporateAction.ex_date <= entitle_date,
        )
        .all()
    )
    try:
        buckets = replay_transactions_per_account(transactions, corporate_actions, symbol, market)
        basis = "per_account"
    except AccountReplayError:
        buckets = replay_transactions_merged(transactions, corporate_actions, symbol, market)
        basis = "merged"
    breakdown = {
        account_id: state["quantity"]
        for account_id, state in buckets.items()
        if state["quantity"] > 0
    }
    return breakdown, basis


def match_existing_action(
    announcement: Dict[str, Any],
    action_type: str,
    estimated_total: Optional[Decimal],
    existing_actions: List[CorporateAction],
    *,
    match_window_days: int,
    broker_account_id: Optional[int] = None,
    currency: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """纯函数判重：公告 vs 既有账本记录（按建议的账户归属过滤候选）。

    候选 = 同账户记录 + 未归属（NULL 账户，手工录入常见）记录；其他账户的
    记录不算命中——账户 A 已录不代表账户 B 的权益也已入账。
    现金分红：日期窗 [ex_date−3d, (pay_date or ex_date)+window] 命中即 MATCHED；
    金额超容差仍 MATCHED 但携带 amount_diff（前端标黄提示核对）。
    送转：±7 天窗口、STOCK_DIVIDEND/BONUS_ISSUE 均视为已录。

    `currency`（港股路径传入；A/B 股不传、口径不变）：
    - 账本记录币种与建议币种不同（券商按 USD/CNY 入账港股股息）→ 仍按日期 MATCHED，
      但金额不可比，记 currency_mismatch 而不是 amount_diff；
    - 同币种时金额按「与命中记录同一 ex_date 的全部同币种候选」合计比较——港股同一
      除净日常有末期 + 特別两笔，券商分两行入账，建议按除净日合并成一条。
    """
    ex_date = announcement["ex_date"]
    if action_type == "CASH_DIVIDEND":
        window_start = ex_date - timedelta(days=MATCH_WINDOW_BEFORE_DAYS)
        window_end = (announcement.get("pay_date") or ex_date) + timedelta(days=match_window_days)
        candidate_types = {"CASH_DIVIDEND"}
    else:
        window_start = ex_date - timedelta(days=STOCK_MATCH_WINDOW_DAYS)
        window_end = ex_date + timedelta(days=STOCK_MATCH_WINDOW_DAYS)
        candidate_types = {"STOCK_DIVIDEND", "BONUS_ISSUE"}

    best: Optional[Dict[str, Any]] = None
    in_window: List[CorporateAction] = []
    for action in existing_actions:
        if action.action_type not in candidate_types:
            continue
        if (
            action.broker_account_id is not None
            and broker_account_id is not None
            and action.broker_account_id != broker_account_id
        ):
            continue
        if action.ex_date is None or not (window_start <= action.ex_date <= window_end):
            continue
        in_window.append(action)
        detail: Dict[str, Any] = {
            "matched_by": "date_window",
            "matched_action_id": action.id,
            "date_gap_days": abs((action.ex_date - ex_date).days),
        }
        if action_type == "CASH_DIVIDEND" and estimated_total is not None and currency:
            recorded_currency = (action.currency or "").upper()
            if recorded_currency != currency.upper():
                detail["currency_mismatch"] = {
                    "recorded_currency": recorded_currency or None,
                    "suggested_currency": currency,
                    "recorded_total": float(Decimal(str(action.total_dividend or 0))),
                }
            else:
                detail["_same_currency"] = True
        elif action_type == "CASH_DIVIDEND" and estimated_total is not None:
            recorded_total = Decimal(str(action.total_dividend or 0))
            diff = abs(recorded_total - estimated_total)
            tolerance = max(estimated_total * AMOUNT_RELATIVE_TOLERANCE, AMOUNT_ABSOLUTE_TOLERANCE)
            if diff > tolerance:
                detail["amount_diff"] = float(diff)
                detail["recorded_total"] = float(recorded_total)
                detail["estimated_total"] = float(estimated_total)
        # 取日期差最小的候选
        if best is None or detail["date_gap_days"] < best["date_gap_days"]:
            best = detail
    if best is not None and best.pop("_same_currency", False) and estimated_total is not None:
        # 港股：与命中记录同一入账日、同币种的全部候选合计比较（末期 + 特別分两行入账）
        anchor = next(a for a in in_window if a.id == best["matched_action_id"])
        siblings = [
            a
            for a in in_window
            if a.ex_date == anchor.ex_date
            and (a.currency or "").upper() == (anchor.currency or "").upper()
        ]
        recorded_total = sum((Decimal(str(a.total_dividend or 0)) for a in siblings), Decimal("0"))
        if len(siblings) > 1:
            best["matched_action_ids"] = sorted(a.id for a in siblings)
        diff = abs(recorded_total - estimated_total)
        tolerance = max(estimated_total * AMOUNT_RELATIVE_TOLERANCE, AMOUNT_ABSOLUTE_TOLERANCE)
        if diff > tolerance:
            best["amount_diff"] = float(diff)
            best["recorded_total"] = float(recorded_total)
            best["estimated_total"] = float(estimated_total)
    return best


def _upsert_suggestion(
    db: Session,
    user_id: int,
    identity: Dict[str, Any],
    values: Dict[str, Any],
) -> str:
    """按幂等键 upsert：ACCEPTED/IGNORED 不动；NEW/MATCHED 刷新（公告可能修订）。

    状态判定在记录锁内重读后进行——与 accept/ignore/restore 共用同一把
    `ca-suggestion-record` 锁，否则重同步可能拿旧 ORM 状态把并发提交的
    ACCEPTED 覆盖回 NEW/MATCHED。返回 'new' / 'refreshed' / 'kept'。
    """
    existing = db.query(CorporateActionSuggestion).filter_by(user_id=user_id, **identity).first()
    if existing is None:
        db.add(CorporateActionSuggestion(user_id=user_id, **identity, **values))
        return "new"
    lock_record(db, "ca-suggestion-record", existing.id)
    db.refresh(existing)
    if existing.status in ("ACCEPTED", "IGNORED"):
        return "kept"
    for field, value in values.items():
        setattr(existing, field, value)
    return "refreshed"


def _remove_stale_suggestions(
    db: Session,
    user_id: int,
    symbol: str,
    market: str,
    ex_date: date,
    valid_identities: Set[Tuple[str, Optional[int]]],
) -> int:
    """重同步 reconciliation：撤销该公告下已失效的 NEW/MATCHED 建议。

    最新重放中不再持有权益的账户桶（或整条公告权益归零）对应的旧建议若
    保留为可接受状态，会把已不存在的权益写入账本。ACCEPTED/IGNORED 保留
    （前者是既成账本事实，后者是用户显式决定）；删除同样在记录锁内重读
    后进行，避免撤销与并发接受竞态。
    """
    removed = 0
    candidates = (
        db.query(CorporateActionSuggestion)
        .filter(
            CorporateActionSuggestion.user_id == user_id,
            CorporateActionSuggestion.symbol == symbol,
            CorporateActionSuggestion.market == market,
            CorporateActionSuggestion.ex_date == ex_date,
            CorporateActionSuggestion.status.in_(("NEW", "MATCHED")),
        )
        .all()
    )
    for row in candidates:
        if (row.action_type, row.broker_account_id) in valid_identities:
            continue
        lock_record(db, "ca-suggestion-record", row.id)
        db.refresh(row)
        if row.status not in ("NEW", "MATCHED"):
            continue  # 锁内重读发现已被接受/忽略：保留
        db.delete(row)
        removed += 1
    return removed


def upsert_security_event(
    db: Session,
    symbol: str,
    market: str,
    event_type: str,
    event_date: date,
    source: str,
    payload: Optional[Dict[str, Any]] = None,
) -> bool:
    """全局事件表原子 upsert（INSERT ... ON CONFLICT DO UPDATE）。返回是否新增。

    security_events 跨用户共享：两个用户同时同步同一标的时，query-then-insert
    会让后提交者撞唯一键并回滚整个标的批次，因此必须用数据库原子 upsert。
    `xmax = 0` 是 PostgreSQL 判定"本语句是插入而非更新"的标准技巧。
    """
    stmt = (
        pg_insert(SecurityEvent)
        .values(
            symbol=symbol,
            market=market,
            event_type=event_type,
            event_date=event_date,
            source=source,
            payload=payload,
        )
        .on_conflict_do_update(
            constraint="uq_security_events_identity",
            # core 语句不触发 ORM 的 onupdate，updated_at 需显式刷新
            set_={"payload": payload, "source": source, "updated_at": func.now()},
        )
        .returning(literal_column("(xmax = 0)").label("inserted"))
    )
    return bool(db.execute(stmt).scalar_one())


def _sync_symbol_events(
    db: Session,
    symbol: str,
    market: str,
    dividend_rows: List[Dict[str, Any]],
    *,
    lookback_start: date,
) -> int:
    """单标的事件落库：分红预案 + 财报披露计划 + 限售解禁。"""
    events_upserted = 0

    # 分红预案/股东大会通过（尚未实施）且已知除权日 → DIVIDEND_PLAN
    for row in dividend_rows:
        if row["div_proc"] not in ("预案", "股东大会通过"):
            continue
        if row["ex_date"] is None or row["ex_date"] < lookback_start:
            continue
        if upsert_security_event(
            db,
            symbol,
            market,
            "DIVIDEND_PLAN",
            row["ex_date"],
            "tushare-dividend",
            payload={
                "div_proc": row["div_proc"],
                "cash_div_tax": float(row["cash_div_tax"]) if row["cash_div_tax"] else None,
                "stk_div": float(row["stk_div"]) if row["stk_div"] else None,
                "pay_date": row["pay_date"].isoformat() if row["pay_date"] else None,
            },
        ):
            events_upserted += 1

    # 财报披露计划：未实际披露的计划日
    for row in fetch_disclosure_dates(symbol, market):
        if row["pre_date"] is None or row["actual_date"] is not None:
            continue
        if row["pre_date"] < lookback_start:
            continue
        if upsert_security_event(
            db,
            symbol,
            market,
            "EARNINGS_DISCLOSURE",
            row["pre_date"],
            "tushare-disclosure_date",
            payload={"period": row["end_date"].isoformat() if row["end_date"] else None},
        ):
            events_upserted += 1

    # 限售解禁：按解禁日聚合
    floats_by_date: Dict[date, Dict[str, Any]] = {}
    for row in fetch_share_floats(symbol, market):
        float_date = row["float_date"]
        if float_date is None or float_date < lookback_start:
            continue
        bucket = floats_by_date.setdefault(
            float_date, {"float_share": Decimal("0"), "batches": 0, "float_ratio": Decimal("0")}
        )
        bucket["float_share"] += row["float_share"] or Decimal("0")
        bucket["float_ratio"] += row["float_ratio"] or Decimal("0")
        bucket["batches"] += 1
    for float_date, bucket in floats_by_date.items():
        if upsert_security_event(
            db,
            symbol,
            market,
            "SHARE_UNLOCK",
            float_date,
            "tushare-share_float",
            payload={
                "float_share": float(bucket["float_share"]),
                "float_ratio_pct": float(bucket["float_ratio"]),
                "batches": bucket["batches"],
            },
        ):
            events_upserted += 1

    return events_upserted


def _cash_suggestion_values(
    announcement: Dict[str, Any],
    *,
    currency: str,
    per_share_pre_tax: Decimal,
    per_share_after_tax: Optional[Decimal],
    extra_values: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    values = {
        "ann_date": announcement["ann_date"],
        "record_date": announcement["record_date"],
        "pay_date": announcement["pay_date"],
        "currency": currency,
        "cash_div_pre_tax": per_share_pre_tax,
        "cash_div_after_tax": per_share_after_tax,
    }
    values.update(extra_values or {})
    return values


def _upsert_cash_suggestions(
    db: Session,
    user_id: int,
    symbol: str,
    market: str,
    announcement: Dict[str, Any],
    breakdown: Dict[Optional[int], Decimal],
    basis: str,
    existing_actions: List[CorporateAction],
    result: Dict[str, Any],
    *,
    base_values: Dict[str, Any],
    per_share_pre_tax: Decimal,
    match_currency: Optional[str] = None,
) -> Set[Tuple[str, Optional[int]]]:
    """现金分红是账户域收入 → 每账户一条建议（NULL 桶 = 未指定账户或合并降级）。

    返回本轮仍然有效的建议身份，供公告级 reconciliation 撤销失效的旧建议。
    """
    valid_identities: Set[Tuple[str, Optional[int]]] = set()
    for account_id, quantity in sorted(
        breakdown.items(),
        key=lambda item: (item[0] is None, item[0] or 0),
    ):
        valid_identities.add(("CASH_DIVIDEND", account_id))
        estimated_total = per_share_pre_tax * quantity
        match = match_existing_action(
            announcement,
            "CASH_DIVIDEND",
            estimated_total,
            existing_actions,
            match_window_days=settings.dividend_sync_match_window_days,
            broker_account_id=account_id,
            currency=match_currency,
        )
        outcome = _upsert_suggestion(
            db,
            user_id,
            identity={
                "symbol": symbol,
                "market": market,
                "action_type": "CASH_DIVIDEND",
                "ex_date": announcement["ex_date"],
                "broker_account_id": account_id,
            },
            values={
                **base_values,
                "record_date_quantity": quantity,
                "quantity_basis": basis,
                "estimated_total_dividend": estimated_total,
                "status": "MATCHED" if match else "NEW",
                "matched_corporate_action_id": (match["matched_action_id"] if match else None),
                "match_detail": match,
            },
        )
        _count_outcome(result, outcome, match)
    return valid_identities


def _sync_tushare_symbol(
    db: Session,
    user_id: int,
    symbol: str,
    market: str,
    *,
    lookback_start: date,
    result: Dict[str, Any],
) -> None:
    """A/B 股：Tushare dividend（div_proc="实施"）→ 建议；非实施阶段 → 事件。"""
    rows = fetch_dividend_announcements(symbol, market)
    result["symbols_scanned"] += 1

    existing_actions = (
        db.query(CorporateAction)
        .filter(
            CorporateAction.user_id == user_id,
            CorporateAction.symbol == symbol,
            CorporateAction.market == market,
        )
        .all()
    )

    for row in rows:
        if row["div_proc"] != "实施" or row["ex_date"] is None:
            continue
        if row["ex_date"] < lookback_start:
            continue
        result["announcements"] += 1

        entitle_date = row["record_date"] or (row["ex_date"] - timedelta(days=1))
        breakdown, basis = quantity_on_record_date(db, user_id, symbol, market, entitle_date)
        if not breakdown:
            result["skipped_no_position"] += 1

        # 现金分红每账户一条建议；送转是比例行动、重放时作用于所有账户桶
        # → 每公告仅一条账户无关建议，避免因子按账户重复应用。
        # valid_identities 记录本轮仍然有效的建议身份，处理完公告后
        # 撤销已失效的旧 NEW/MATCHED（如账户桶消失、权益归零）。
        valid_identities: Set[Tuple[str, Optional[int]]] = set()
        per_share_pre_tax = row["cash_div_tax"] or row["cash_div"]
        if breakdown and per_share_pre_tax and per_share_pre_tax > 0:
            valid_identities |= _upsert_cash_suggestions(
                db,
                user_id,
                symbol,
                market,
                row,
                breakdown,
                basis,
                existing_actions,
                result,
                base_values=_cash_suggestion_values(
                    row,
                    currency="CNY",
                    per_share_pre_tax=per_share_pre_tax,
                    per_share_after_tax=row["cash_div"],
                ),
                per_share_pre_tax=per_share_pre_tax,
            )

        if breakdown and row["stk_div"] and row["stk_div"] > 0:
            valid_identities.add(("STOCK_DIVIDEND", None))
            total_quantity = sum(breakdown.values(), Decimal("0"))
            match = match_existing_action(
                row,
                "STOCK_DIVIDEND",
                None,
                existing_actions,
                match_window_days=settings.dividend_sync_match_window_days,
                broker_account_id=None,
            )
            outcome = _upsert_suggestion(
                db,
                user_id,
                identity={
                    "symbol": symbol,
                    "market": market,
                    "action_type": "STOCK_DIVIDEND",
                    "ex_date": row["ex_date"],
                    "broker_account_id": None,
                },
                values={
                    "ann_date": row["ann_date"],
                    "record_date": row["record_date"],
                    "pay_date": row["pay_date"],
                    "currency": "CNY",
                    "stk_div_per_share": row["stk_div"],
                    "record_date_quantity": total_quantity,
                    "quantity_basis": basis,
                    "status": "MATCHED" if match else "NEW",
                    "matched_corporate_action_id": (match["matched_action_id"] if match else None),
                    "match_detail": match,
                },
            )
            _count_outcome(result, outcome, match)

        # 公告级 reconciliation：本轮无效的旧 NEW/MATCHED 建议撤销
        result["stale_removed"] += _remove_stale_suggestions(
            db, user_id, symbol, market, row["ex_date"], valid_identities
        )

    result["events_upserted"] += _sync_symbol_events(
        db, symbol, market, rows, lookback_start=lookback_start
    )


# ---------------------------------------------------------------------------
# 港股（披露易现金股息公告表格）
# ---------------------------------------------------------------------------


def _decimal_text(value: Optional[Decimal]) -> Optional[str]:
    return format(value.normalize(), "f") if value is not None else None


def _component_detail(entry: Dict[str, Any]) -> Dict[str, Any]:
    form = entry["form"]
    financial_year_end = form.get("financial_year_end")
    return {
        "dividend_type": form["dividend_type"],
        "dividend_nature": form["dividend_nature"],
        "period_end": form["period_end"].isoformat() if form["period_end"] else None,
        # 報告期末「不適用」时的身份锚点来源（financial_year_end / none），见 dividend_identity
        "period_basis": hkex_dividend_source.period_basis(form),
        "financial_year_end": financial_year_end.isoformat() if financial_year_end else None,
        "template": form.get("template"),
        "amount": _decimal_text(form["payment"]["amount"]),
        "currency": form["payment"]["currency"],
        "declared_amount": _decimal_text((form.get("declared") or {}).get("amount")),
        "declared_currency": (form.get("declared") or {}).get("currency"),
        "exchange_rate": (
            {**form["exchange_rate"], "rate": _decimal_text(form["exchange_rate"]["rate"])}
            if form.get("exchange_rate")
            else None
        ),
        "status": form["status"],
        "announcement_date": form["announcement_date"].isoformat(),
        "record_date": form["record_date"].isoformat() if form["record_date"] else None,
        "pay_date": form["pay_date"].isoformat() if form["pay_date"] else None,
        "url": entry.get("url"),
        "withholding": {
            "applicable": form["withholding"].get("applicable"),
            "rates_percent": [
                _decimal_text(rate) for rate in form["withholding"].get("rates_percent") or []
            ],
            "non_resident_enterprise_percent": _decimal_text(
                form["withholding"].get("non_resident_enterprise_percent")
            ),
            "southbound_individual_percent": _decimal_text(
                form["withholding"].get("southbound_individual_percent")
            ),
        },
        "scrip_option": form["scrip_option"],
        "currency_election": form["currency_election"],
        # EF003 预设选项/代息股份价格、EF002 可选货币金额：只作展示，建议金额仍是预设现金
        "scrip": form.get("scrip"),
        "currency_options": form.get("currency_options"),
    }


def group_hk_dividends_by_ex_date(
    current: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """现行股息按除净日合并（建议与事件的身份键都含除净日，末期 + 特別同日即一条）。

    返回 (groups, conflicts)：同一除净日的派发币种不一致时无法相加，整组进
    conflicts 不生成建议（不猜汇率）。group = 公告形状 dict + per_share/currency/detail。
    """
    by_ex_date: Dict[date, List[Dict[str, Any]]] = {}
    for entry in current:
        by_ex_date.setdefault(entry["form"]["ex_date"], []).append(entry)
    groups: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    for ex_date in sorted(by_ex_date):
        entries = sorted(by_ex_date[ex_date], key=lambda e: e["sort_key"])
        currencies = {e["form"]["payment"]["currency"] for e in entries}
        if len(currencies) != 1:
            conflicts.append(
                {
                    "ex_date": ex_date.isoformat(),
                    "reason": f"同一除净日派发币种不一致：{'/'.join(sorted(currencies))}",
                }
            )
            continue
        forms = [e["form"] for e in entries]
        record_dates = sorted({f["record_date"] for f in forms if f["record_date"]})
        pay_dates = sorted({f["pay_date"] for f in forms if f["pay_date"]})
        components = [_component_detail(e) for e in entries]
        groups.append(
            {
                "ex_date": ex_date,
                "ann_date": max(f["announcement_date"] for f in forms),
                "record_date": record_dates[-1] if record_dates else None,
                # 判重窗口右端用最晚的派息日（两笔股息派息日不同时覆盖两者）
                "pay_date": pay_dates[-1] if pay_dates else None,
                "per_share": sum((f["payment"]["amount"] for f in forms), Decimal("0")),
                "currency": currencies.pop(),
                "detail": {
                    "source": "hkexnews",
                    "components": components,
                    "scrip_option": any(f["scrip_option"] for f in forms),
                    "currency_election": any(f["currency_election"] for f in forms),
                    "withholding_applicable": (
                        True
                        if any(c["withholding"]["applicable"] for c in components)
                        else False
                        if all(c["withholding"]["applicable"] is False for c in components)
                        else None
                    ),
                    "entitlement_basis": "ex_date_minus_1",
                    "after_tax_policy": "unknown_depends_on_holder_channel",
                },
            }
        )
    return groups, conflicts


def _hk_form_list_start(db: Session, symbol: str, market: str, lookback_start: date) -> date:
    earliest_price = (
        db.query(func.min(SecurityPrice.price_date))
        .filter(SecurityPrice.symbol == symbol, SecurityPrice.market == market)
        .scalar()
    )
    start = lookback_start - timedelta(days=HK_FORM_LIST_LEAD_DAYS)
    if earliest_price is not None:
        start = min(start, earliest_price)
    return max(start, HK_FORM_EARLIEST)


def _remove_superseded_hk_rows(
    db: Session,
    user_id: int,
    symbol: str,
    market: str,
    keep_ex_dates: Set[date],
    lookback_start: date,
) -> Tuple[int, int]:
    """更新公告改了除净日 / 撤回公告：该标的窗口内不再对应任何现行股息的
    NEW/MATCHED 建议与本源事件撤销（ACCEPTED/IGNORED 保留，与重同步口径一致）。"""
    removed = 0
    stale_dates = {
        row[0]
        for row in db.query(CorporateActionSuggestion.ex_date)
        .filter(
            CorporateActionSuggestion.user_id == user_id,
            CorporateActionSuggestion.symbol == symbol,
            CorporateActionSuggestion.market == market,
            CorporateActionSuggestion.source == hkex_dividend_source.SOURCE,
            CorporateActionSuggestion.ex_date >= lookback_start,
            CorporateActionSuggestion.status.in_(("NEW", "MATCHED")),
        )
        .distinct()
        .all()
    } - keep_ex_dates
    for ex_date in sorted(stale_dates):
        removed += _remove_stale_suggestions(db, user_id, symbol, market, ex_date, set())
    event_query = db.query(SecurityEvent).filter(
        SecurityEvent.symbol == symbol,
        SecurityEvent.market == market,
        SecurityEvent.event_type == "DIVIDEND_PLAN",
        SecurityEvent.source == hkex_dividend_source.SOURCE,
        SecurityEvent.event_date >= lookback_start,
    )
    if keep_ex_dates:
        event_query = event_query.filter(SecurityEvent.event_date.notin_(keep_ex_dates))
    events_removed = event_query.delete(synchronize_session=False)
    return removed, events_removed


def _sync_hkex_symbol(
    db: Session,
    user_id: int,
    symbol: str,
    market: str,
    *,
    lookback_start: date,
    result: Dict[str, Any],
    on_download: Optional[Callable[[], None]] = None,
) -> None:
    """港股：披露易现金股息公告表格 → 建议 + DIVIDEND_PLAN 事件 + 复权因子。

    - 登记日权益按**除净日前一天（交易日口径）**推算，不按记录日期：港股 T+2 交收，
      除净日当天及之后的买入即使在记录日期前交收也不享有股息，而本账本按成交日记录；
    - 每股金额 = 预设派发货币金额（宣派 RMB、派发 HKD 的取 HKD 金额），建议币种即派发
      币种；税后金额一律留空：代扣税取决于持有渠道（H 股/红筹经 HKSCC 代理人按非居民
      企业 10%、港股通个人 20%、直接登记的个人可能不扣），公告只能提示、不能定论，
      入账时由用户按券商到账填写，公告的代扣信息放在 announcement_detail；
    - 同一除净日的多笔（末期 + 特別）合并成一条建议，组成见 announcement_detail.components。
    """
    fetched = hkex_dividend_source.ensure_dividend_forms(
        db,
        symbol,
        market,
        from_date=_hk_form_list_start(db, symbol, market, lookback_start),
        to_date=local_today() + timedelta(days=1),
        on_download=on_download,
    )
    result["symbols_scanned"] += 1
    result["hk_forms_downloaded"] += fetched["downloaded"]
    # 告警只报回看窗口内的公告（缓存里更早的历史表格照样参与复权，但不每次重复告警）
    for item in fetched["unparsed"]:
        if (item.get("listed_at") or "") >= lookback_start.isoformat():
            result["hk_unparsed_forms"].append({"symbol": symbol, **item})

    resolution = hkex_dividend_source.resolve_dividend_resolution(fetched["entries"])
    if resolution.unscoped:
        # 有公告认不出、也认不出是哪一笔股息（可能是任一笔的更新或撤回）：本标的这次
        # 不写建议/事件/复权因子，也不删旧行，等解析器修好或人工核对（PR #249 评审 P2）
        result["hk_blocked"].append(
            {
                "symbol": symbol,
                "scope": "symbol",
                "forms": [
                    {"doc_id": e.get("doc_id"), "url": e.get("url"), "reason": e.get("reason")}
                    for e in resolution.unscoped
                ],
            }
        )
        return
    for item in resolution.blocked:
        result["hk_blocked"].append(
            {
                "symbol": symbol,
                "scope": "dividend",
                **hkex_dividend_source.describe_identity(
                    item["identity"], item.get("period_basis")
                ),
                "ex_dates": [day.isoformat() for day in item["ex_dates"]],
                "doc_id": item["entry"].get("doc_id"),
                "url": item["entry"].get("url"),
                "reason": item["entry"].get("reason"),
            }
        )
    # 挂起股息出现过的除净日：既不改写也不删除该日已有的建议与事件（同日另一笔现行股息
    # 也跳过——只写一半会让合并后的金额变小）
    protected = resolution.protected_ex_dates
    current, pending = resolution.current, resolution.pending
    for entry in pending:
        form = entry["form"]
        if form["announcement_date"] < lookback_start:
            continue
        result["hk_pending"].append(
            {
                "symbol": symbol,
                "dividend_type": form["dividend_type"],
                "dividend_nature": form["dividend_nature"],
                "period_end": form["period_end"].isoformat() if form["period_end"] else None,
                "period_basis": hkex_dividend_source.period_basis(form),
                "announcement_date": form["announcement_date"].isoformat(),
            }
        )
    groups, conflicts = group_hk_dividends_by_ex_date(current)
    for conflict in conflicts:
        result["hk_conflicts"].append({"symbol": symbol, **conflict})

    existing_actions = (
        db.query(CorporateAction)
        .filter(
            CorporateAction.user_id == user_id,
            CorporateAction.symbol == symbol,
            CorporateAction.market == market,
        )
        .all()
    )

    in_window = [
        group
        for group in groups
        if group["ex_date"] >= lookback_start and group["ex_date"] not in protected
    ]
    for group in in_window:
        result["announcements"] += 1
        breakdown, basis = quantity_on_record_date(
            db, user_id, symbol, market, group["ex_date"] - timedelta(days=1)
        )
        if not breakdown:
            result["skipped_no_position"] += 1
        valid_identities: Set[Tuple[str, Optional[int]]] = set()
        if breakdown:
            valid_identities = _upsert_cash_suggestions(
                db,
                user_id,
                symbol,
                market,
                group,
                breakdown,
                basis,
                existing_actions,
                result,
                base_values=_cash_suggestion_values(
                    group,
                    currency=group["currency"],
                    per_share_pre_tax=group["per_share"],
                    per_share_after_tax=None,
                    extra_values={
                        "source": hkex_dividend_source.SOURCE,
                        "announcement_detail": group["detail"],
                    },
                ),
                per_share_pre_tax=group["per_share"],
                match_currency=group["currency"],
            )
        result["stale_removed"] += _remove_stale_suggestions(
            db, user_id, symbol, market, group["ex_date"], valid_identities
        )
        if upsert_security_event(
            db,
            symbol,
            market,
            "DIVIDEND_PLAN",
            group["ex_date"],
            hkex_dividend_source.SOURCE,
            payload={
                "div_proc": "披露易公告",
                "cash_div_tax": float(group["per_share"]),
                "currency": group["currency"],
                "record_date": group["record_date"].isoformat() if group["record_date"] else None,
                "pay_date": group["pay_date"].isoformat() if group["pay_date"] else None,
                "dividend_types": [
                    " ".join(filter(None, [c["dividend_type"], c["dividend_nature"]]))
                    for c in group["detail"]["components"]
                ],
            },
        ):
            result["events_upserted"] += 1

    stale_removed, events_removed = _remove_superseded_hk_rows(
        db,
        user_id,
        symbol,
        market,
        {g["ex_date"] for g in in_window} | protected,
        lookback_start,
    )
    result["stale_removed"] += stale_removed
    result["events_removed"] += events_removed

    # 复权因子是预留数据（当前无读取方）：重算失败只记日志，放在 savepoint 里，
    # 不回滚本标的已写入的建议与事件（#276）
    try:
        with db.begin_nested():
            adj = recompute_hk_adj_factors(db, symbol)
        result["hk_adj_rows_updated"] += adj["updated"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("港股复权因子重算失败（不影响分红同步）%s: %s", symbol, str(exc)[:200])


def sync_dividends_for_user(
    db: Session,
    user_id: int,
    *,
    progress: Optional[Callable[..., None]] = None,
) -> Dict[str, Any]:
    """主流程：持仓收集 → 按市场拉公告 → 建议判重 upsert + 事件落库。

    单 symbol 失败只记入 failed_list，不中断整个 job；配额类错误由
    tushare_query 抛出后同样按单标的失败处理（下一标的会再次触发并快速失败）。
    未配置 TUSHARE_TOKEN 时 A/B 股整体跳过（`tushare_unavailable` + 跳过数），
    港股不依赖 Tushare 照常同步。`progress(completed=, total=, current_symbol=)`
    每个标的前后及每下载一份港股表格回调一次（job 续租）。
    """
    lookback_start = local_today() - timedelta(days=settings.dividend_sync_lookback_days)
    excluded = get_excluded_keys(db, user_id)
    cash_management = get_cash_management_symbols(db, user_id)

    # 目标全集 = 当前持仓 ∪ lookback 内交易过的标的（有界并集）。
    # 只看当前持仓会漏掉"登记日持有、随后卖清"的应收分红：登记日 ≥ lookback
    # 起点时，清仓卖出必然也落在窗口内，因此交易并集覆盖全部应享权益标的。
    holding_keys = (
        db.query(Holding.symbol, Holding.market)
        .filter(Holding.user_id == user_id, Holding.quantity > 0)
        .distinct()
        .all()
    )
    traded_keys = (
        db.query(Transaction.symbol, Transaction.market)
        .filter(
            Transaction.user_id == user_id,
            Transaction.transaction_date >= lookback_start,
        )
        .distinct()
        .all()
    )
    candidate_keys = set(holding_keys) | set(traded_keys)
    eligible = sorted(
        {
            (symbol, market)
            for symbol, market in candidate_keys
            if market in SUPPORTED_MARKETS
            and (symbol, market) not in excluded
            and symbol not in cash_management
        }
    )
    has_tushare = tushare_configured()
    targets = [key for key in eligible if has_tushare or key[1] not in TUSHARE_MARKETS]
    unsupported_markets = sorted(
        {market for _, market in candidate_keys if market not in SUPPORTED_MARKETS}
    )

    result: Dict[str, Any] = {
        "symbols_scanned": 0,
        "announcements": 0,
        "new": 0,
        "matched": 0,
        "refreshed": 0,
        "skipped_no_position": 0,
        "stale_removed": 0,
        "events_upserted": 0,
        "events_removed": 0,
        "failed": [],
        "unsupported_markets": unsupported_markets,
        "tushare_unavailable": not has_tushare,
        "skipped_no_tushare": len(eligible) - len(targets),
        "hk_forms_downloaded": 0,
        "hk_unparsed_forms": [],
        "hk_conflicts": [],
        "hk_pending": [],
        # 最新公告无法解析而挂起的股息（scope=dividend）或整个标的（scope=symbol）
        "hk_blocked": [],
        "hk_adj_rows_updated": 0,
    }

    def report(**fields: Any) -> None:
        if progress:
            progress(total=len(targets), **fields)

    for index, (symbol, market) in enumerate(targets):
        report(completed=index, current_symbol=symbol)
        try:
            if market in HKEX_MARKETS:
                _sync_hkex_symbol(
                    db,
                    user_id,
                    symbol,
                    market,
                    lookback_start=lookback_start,
                    result=result,
                    on_download=lambda: report(completed=index, current_symbol=symbol),
                )
            else:
                _sync_tushare_symbol(
                    db,
                    user_id,
                    symbol,
                    market,
                    lookback_start=lookback_start,
                    result=result,
                )
            db.commit()
        except JobOwnershipLostError:
            db.rollback()  # 被接管：整个同步立即停手，不算单标的失败
            raise
        except Exception as exc:  # 单标的失败不中断
            db.rollback()
            logger.warning("Dividend sync failed for %s (%s): %s", symbol, market, exc)
            result["failed"].append({"symbol": symbol, "market": market, "error": str(exc)[:200]})
    report(completed=len(targets), current_symbol=None)

    return result


def _count_outcome(result: Dict[str, Any], outcome: str, match: Optional[Dict]) -> None:
    if outcome == "new":
        result["matched" if match else "new"] += 1
    elif outcome == "refreshed":
        result["refreshed"] += 1


class SuggestionStateError(ValueError):
    """建议状态不允许该操作（映射为 409）。"""


def accept_suggestion(
    db: Session,
    user: User,
    suggestion_id: int,
    overrides: Dict[str, Any],
) -> CorporateAction:
    """接受建议 → 创建正式 CorporateAction（与手工创建同一事务模式）。

    锁序遵循现有纪律：先按建议 id 取记录锁并在锁内重读（并发接受的第二个
    会话在此看到 ACCEPTED → 409），再校验状态、取时间线锁、写入。

    仅 NEW 可接受——MATCHED 表示账本已有命中记录，再入账即股息双计/持仓
    因子重复应用。建议行携带的判重结论是同步时刻的快照：建议生成后券商
    导入或手工录入可能已把同一笔分红写入账本，因此取得时间线锁后必须对
    当前账本**重新判重**——命中则不插入，把建议转为 MATCHED 并拒绝。

    账户归属取建议行自身的 broker_account_id（每账户一条建议），overrides
    仅允许纠正归属与税额；税额不得超过总额。STOCK_DIVIDEND 同事务重算持仓。
    """
    lock_record(db, "ca-suggestion-record", suggestion_id)
    suggestion = (
        db.query(CorporateActionSuggestion)
        .filter(
            CorporateActionSuggestion.id == suggestion_id,
            CorporateActionSuggestion.user_id == user.id,
        )
        .first()
    )
    if suggestion is None:
        raise LookupError("分红建议不存在")
    db.refresh(suggestion)

    if suggestion.status != "NEW":
        if suggestion.status == "MATCHED":
            raise SuggestionStateError(
                "该建议已匹配到账本既有记录"
                f"（公司行动 #{suggestion.matched_corporate_action_id}），"
                "再次入账会导致股息双计；如确需入账请先核对并处理既有记录。"
            )
        raise SuggestionStateError(f"建议已处于 {suggestion.status} 状态，不能接受")

    lock_security_timeline(db, user.id, suggestion.symbol, suggestion.market)

    # 最终归属账户 = override 优先；重判重必须按它过滤候选——用户把入账
    # 改到账户 X 时，账本里 X 上已有的匹配记录才是双计风险所在。
    broker_account_id = (
        overrides["broker_account_id"]
        if "broker_account_id" in overrides
        else suggestion.broker_account_id
    )

    # 时间线锁内对当前账本重新判重：同步之后导入/手工录入的匹配分红在
    # 建议行的快照结论里看不见。命中 → 转 MATCHED、不插入（先提交状态
    # 转换再抛错，让前端刷新后看到"已在账"而非可重试的 NEW）。
    current_actions = (
        db.query(CorporateAction)
        .filter(
            CorporateAction.user_id == user.id,
            CorporateAction.symbol == suggestion.symbol,
            CorporateAction.market == suggestion.market,
        )
        .all()
    )
    late_match = match_existing_action(
        {"ex_date": suggestion.ex_date, "pay_date": suggestion.pay_date},
        suggestion.action_type,
        (
            Decimal(str(suggestion.estimated_total_dividend))
            if suggestion.action_type == "CASH_DIVIDEND"
            and suggestion.estimated_total_dividend is not None
            else None
        ),
        current_actions,
        match_window_days=settings.dividend_sync_match_window_days,
        broker_account_id=broker_account_id,
        # 港股建议与同步时同一判重口径（跨币种不比金额、同日多行合计）
        currency=(
            suggestion.currency if suggestion.source == hkex_dividend_source.SOURCE else None
        ),
    )
    if late_match:
        suggestion.status = "MATCHED"
        suggestion.matched_corporate_action_id = late_match["matched_action_id"]
        suggestion.match_detail = late_match
        db.commit()
        raise SuggestionStateError(
            "账本中已存在匹配的分红记录"
            f"（公司行动 #{late_match['matched_action_id']}，可能来自券商导入或"
            "手工录入），未重复入账；建议已标记为已匹配。"
        )
    action_kwargs: Dict[str, Any] = {
        "user_id": user.id,
        "symbol": suggestion.symbol,
        "name": suggestion.name,
        "market": suggestion.market,
        "action_type": suggestion.action_type,
        "ex_date": suggestion.ex_date,
        "record_date": suggestion.record_date,
        "payment_date": suggestion.pay_date,
        "currency": suggestion.currency or "CNY",
        "broker_account_id": broker_account_id,
        "notes": f"来自分红公告建议 #{suggestion.id}（{suggestion.source}）",
    }
    quantity = Decimal(str(suggestion.record_date_quantity or 0))
    if suggestion.action_type == "CASH_DIVIDEND":
        per_share = Decimal(str(suggestion.cash_div_pre_tax or 0))
        gross = (
            Decimal(str(overrides["total_dividend"]))
            if overrides.get("total_dividend") is not None
            else per_share * quantity
        )
        tax = (
            Decimal(str(overrides["tax_withheld"]))
            if overrides.get("tax_withheld") is not None
            else Decimal("0")
        )
        if tax > gross:
            raise SuggestionStateError(
                f"预扣税额（{tax}）不能超过股息总额（{gross}），净股息不能为负"
            )
        action_kwargs.update(
            {
                "dividend_per_share": per_share,
                "total_dividend": gross,
                "tax_withheld": tax,
                "net_dividend": gross - tax,
            }
        )
    else:  # STOCK_DIVIDEND：ratio 优先级语义（semantics.bonus_share_factor）
        stk_div = Decimal(str(suggestion.stk_div_per_share or 0))
        # 每股送转 → "10:N" 基数比例（format 'f' 防 normalize 产生科学计数法）
        bonus_per_ten = format((stk_div * 10).normalize(), "f")
        action_kwargs["distribution_ratio"] = f"10:{bonus_per_ten}"

    db_action = CorporateAction(**action_kwargs)
    db.add(db_action)
    db.flush()

    if suggestion.action_type == "STOCK_DIVIDEND":
        recalculate_holdings(db, user.id, suggestion.symbol, suggestion.market, commit=False)

    suggestion.status = "ACCEPTED"
    suggestion.created_corporate_action_id = db_action.id
    db.commit()
    db.refresh(db_action)
    return db_action


def _locked_suggestion(db: Session, user_id: int, suggestion_id: int) -> CorporateActionSuggestion:
    """记录锁内取回建议行（锁后重读，保证看到并发提交的最新状态）。"""
    lock_record(db, "ca-suggestion-record", suggestion_id)
    suggestion = (
        db.query(CorporateActionSuggestion)
        .filter(
            CorporateActionSuggestion.id == suggestion_id,
            CorporateActionSuggestion.user_id == user_id,
        )
        .first()
    )
    if suggestion is None:
        raise LookupError("分红建议不存在")
    db.refresh(suggestion)
    return suggestion


def ignore_suggestion(db: Session, user_id: int, suggestion_id: int) -> CorporateActionSuggestion:
    """忽略建议（幂等）。锁内重读做条件转换：已接受入账的不能忽略——
    否则 accept/ignore 竞态会留下 status=IGNORED 但账本记录已存在的矛盾态。"""
    suggestion = _locked_suggestion(db, user_id, suggestion_id)
    if suggestion.status == "ACCEPTED":
        raise SuggestionStateError("建议已接受入账，不能忽略")
    suggestion.status = "IGNORED"
    db.commit()
    db.refresh(suggestion)
    return suggestion


def restore_suggestion(db: Session, user_id: int, suggestion_id: int) -> CorporateActionSuggestion:
    """恢复被忽略的建议到忽略前的原状态（锁内条件转换）。

    曾匹配到账本记录的回 MATCHED（保留关联，防止经"忽略→恢复"洗成可入账
    的 NEW 造成双计），否则回 NEW。
    """
    suggestion = _locked_suggestion(db, user_id, suggestion_id)
    if suggestion.status != "IGNORED":
        raise SuggestionStateError("仅已忽略的建议可以恢复")
    suggestion.status = "MATCHED" if suggestion.matched_corporate_action_id is not None else "NEW"
    db.commit()
    db.refresh(suggestion)
    return suggestion
