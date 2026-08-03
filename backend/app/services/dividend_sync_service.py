"""分红公告同步：Tushare dividend → 建议表 + 标的事件表（绝不自动入账）。

仅支持 A/B 股（Tushare 无港/美股分红公开接口，港美股仍以券商对账单导入为准）。
建议入账只发生在用户显式"接受"时，与对账"报告不修复"哲学一致。

判重窗口刻意放宽：三个券商导入器写入的 CASH_DIVIDEND 的 ex_date 实为资金
到账日（滞后真实除权日），且到账金额常为税前全额（tax=0），因此按
[公告除权日−3天, 派息日+match_window] 的日期窗 + 税前口径金额容差匹配。
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import literal_column
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from ..config import settings
from ..core.logging import get_app_logger
from ..models.corporate_action import CorporateAction
from ..models.corporate_action_suggestion import CorporateActionSuggestion
from ..models.holding import Holding
from ..models.security_event import SecurityEvent
from ..models.transaction import Transaction
from ..models.user import User
from .holding_service import (
    AccountReplayError,
    lock_record,
    lock_security_timeline,
    recalculate_holdings,
    replay_transactions_merged,
    replay_transactions_per_account,
)
from .security_rule_service import get_cash_management_symbols, get_excluded_keys
from .stock_price_service import to_tushare_a_code, tushare_query

logger = get_app_logger(__name__)

SUPPORTED_MARKETS = ("A股", "B股")

# 判重日期窗：既有记录 ex_date（实为到账日）允许早于公告除权日的回拨天数
MATCH_WINDOW_BEFORE_DAYS = 3
# 金额相对容差（1%）与绝对容差（1 元）：取较宽者
AMOUNT_RELATIVE_TOLERANCE = Decimal("0.01")
AMOUNT_ABSOLUTE_TOLERANCE = Decimal("1")

# 送转判重窗（送转记录通常按真实除权日录入）
STOCK_MATCH_WINDOW_DAYS = 7


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
    except ValueError:
        return []
    rows = []
    for raw in df.to_dict("records"):
        rows.append({
            "end_date": _parse_ts_date(raw.get("end_date")),
            "ann_date": _parse_ts_date(raw.get("ann_date")),
            "div_proc": str(raw.get("div_proc") or "").strip(),
            "stk_div": _parse_ts_number(raw.get("stk_div")),
            "cash_div": _parse_ts_number(raw.get("cash_div")),
            "cash_div_tax": _parse_ts_number(raw.get("cash_div_tax")),
            "record_date": _parse_ts_date(raw.get("record_date")),
            "ex_date": _parse_ts_date(raw.get("ex_date")),
            "pay_date": _parse_ts_date(raw.get("pay_date")),
        })
    return rows


def fetch_disclosure_dates(symbol: str, market: str) -> List[Dict[str, Any]]:
    """财报披露计划：未实际披露（actual_date 为空）的 pre_date 即未来事件。"""
    try:
        df = tushare_query("disclosure_date", ts_code=to_tushare_a_code(symbol))
    except ValueError:
        return []
    rows = []
    for raw in df.to_dict("records"):
        rows.append({
            "end_date": _parse_ts_date(raw.get("end_date")),
            "pre_date": _parse_ts_date(raw.get("pre_date")),
            "actual_date": _parse_ts_date(raw.get("actual_date")),
        })
    return rows


def fetch_share_floats(symbol: str, market: str) -> List[Dict[str, Any]]:
    """限售解禁：按解禁日聚合（同日多股东合并为一条事件）。"""
    try:
        df = tushare_query("share_float", ts_code=to_tushare_a_code(symbol))
    except ValueError:
        return []
    rows = []
    for raw in df.to_dict("records"):
        rows.append({
            "float_date": _parse_ts_date(raw.get("float_date")),
            "float_share": _parse_ts_number(raw.get("float_share")),
            "float_ratio": _parse_ts_number(raw.get("float_ratio")),
        })
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
    transactions = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.symbol == symbol,
        Transaction.market == market,
        Transaction.transaction_date <= entitle_date,
    ).all()
    corporate_actions = db.query(CorporateAction).filter(
        CorporateAction.user_id == user_id,
        CorporateAction.symbol == symbol,
        CorporateAction.market == market,
        CorporateAction.ex_date <= entitle_date,
    ).all()
    try:
        buckets = replay_transactions_per_account(
            transactions, corporate_actions, symbol, market
        )
        basis = "per_account"
    except AccountReplayError:
        buckets = replay_transactions_merged(
            transactions, corporate_actions, symbol, market
        )
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
) -> Optional[Dict[str, Any]]:
    """纯函数判重：公告 vs 既有账本记录（按建议的账户归属过滤候选）。

    候选 = 同账户记录 + 未归属（NULL 账户，手工录入常见）记录；其他账户的
    记录不算命中——账户 A 已录不代表账户 B 的权益也已入账。
    现金分红：日期窗 [ex_date−3d, (pay_date or ex_date)+window] 命中即 MATCHED；
    金额超容差仍 MATCHED 但携带 amount_diff（前端标黄提示核对）。
    送转：±7 天窗口、STOCK_DIVIDEND/BONUS_ISSUE 均视为已录。
    """
    ex_date = announcement["ex_date"]
    if action_type == "CASH_DIVIDEND":
        window_start = ex_date - timedelta(days=MATCH_WINDOW_BEFORE_DAYS)
        window_end = (announcement.get("pay_date") or ex_date) + timedelta(
            days=match_window_days
        )
        candidate_types = {"CASH_DIVIDEND"}
    else:
        window_start = ex_date - timedelta(days=STOCK_MATCH_WINDOW_DAYS)
        window_end = ex_date + timedelta(days=STOCK_MATCH_WINDOW_DAYS)
        candidate_types = {"STOCK_DIVIDEND", "BONUS_ISSUE"}

    best: Optional[Dict[str, Any]] = None
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
        detail: Dict[str, Any] = {
            "matched_by": "date_window",
            "matched_action_id": action.id,
            "date_gap_days": abs((action.ex_date - ex_date).days),
        }
        if action_type == "CASH_DIVIDEND" and estimated_total is not None:
            recorded_total = Decimal(str(action.total_dividend or 0))
            diff = abs(recorded_total - estimated_total)
            tolerance = max(
                estimated_total * AMOUNT_RELATIVE_TOLERANCE, AMOUNT_ABSOLUTE_TOLERANCE
            )
            if diff > tolerance:
                detail["amount_diff"] = float(diff)
                detail["recorded_total"] = float(recorded_total)
                detail["estimated_total"] = float(estimated_total)
        # 取日期差最小的候选
        if best is None or detail["date_gap_days"] < best["date_gap_days"]:
            best = detail
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
    existing = db.query(CorporateActionSuggestion).filter_by(
        user_id=user_id, **identity
    ).first()
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
    candidates = db.query(CorporateActionSuggestion).filter(
        CorporateActionSuggestion.user_id == user_id,
        CorporateActionSuggestion.symbol == symbol,
        CorporateActionSuggestion.market == market,
        CorporateActionSuggestion.ex_date == ex_date,
        CorporateActionSuggestion.status.in_(("NEW", "MATCHED")),
    ).all()
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
            symbol=symbol, market=market, event_type=event_type,
            event_date=event_date, source=source, payload=payload,
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
            db, symbol, market, "DIVIDEND_PLAN", row["ex_date"], "tushare-dividend",
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
            db, symbol, market, "EARNINGS_DISCLOSURE", row["pre_date"],
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
            db, symbol, market, "SHARE_UNLOCK", float_date, "tushare-share_float",
            payload={
                "float_share": float(bucket["float_share"]),
                "float_ratio_pct": float(bucket["float_ratio"]),
                "batches": bucket["batches"],
            },
        ):
            events_upserted += 1

    return events_upserted


def sync_dividends_for_user(db: Session, user_id: int) -> Dict[str, Any]:
    """主流程：持仓收集 → 拉公告 → 建议判重 upsert + 事件落库。

    单 symbol 失败只记入 failed_list，不中断整个 job；配额类错误由
    tushare_query 抛出后同样按单标的失败处理（下一标的会再次触发并快速失败）。
    """
    lookback_start = date.today() - timedelta(days=settings.dividend_sync_lookback_days)
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
    targets = sorted({
        (symbol, market)
        for symbol, market in candidate_keys
        if market in SUPPORTED_MARKETS
        and (symbol, market) not in excluded
        and symbol not in cash_management
    })
    unsupported_markets = sorted({
        market for _, market in candidate_keys if market not in SUPPORTED_MARKETS
    })

    result: Dict[str, Any] = {
        "symbols_scanned": 0,
        "announcements": 0,
        "new": 0,
        "matched": 0,
        "refreshed": 0,
        "skipped_no_position": 0,
        "stale_removed": 0,
        "events_upserted": 0,
        "failed": [],
        "unsupported_markets": unsupported_markets,
    }

    for symbol, market in targets:
        try:
            rows = fetch_dividend_announcements(symbol, market)
            result["symbols_scanned"] += 1

            existing_actions = db.query(CorporateAction).filter(
                CorporateAction.user_id == user_id,
                CorporateAction.symbol == symbol,
                CorporateAction.market == market,
            ).all()

            for row in rows:
                if row["div_proc"] != "实施" or row["ex_date"] is None:
                    continue
                if row["ex_date"] < lookback_start:
                    continue
                result["announcements"] += 1

                entitle_date = row["record_date"] or (row["ex_date"] - timedelta(days=1))
                breakdown, basis = quantity_on_record_date(
                    db, user_id, symbol, market, entitle_date
                )
                if not breakdown:
                    result["skipped_no_position"] += 1

                # 现金分红是账户域收入 → 每账户一条建议（NULL 桶 = 未指定
                # 账户或合并降级）；送转是比例行动、重放时作用于所有账户桶
                # → 每公告仅一条账户无关建议，避免因子按账户重复应用。
                # valid_identities 记录本轮仍然有效的建议身份，处理完公告后
                # 撤销已失效的旧 NEW/MATCHED（如账户桶消失、权益归零）。
                valid_identities: Set[Tuple[str, Optional[int]]] = set()
                per_share_pre_tax = row["cash_div_tax"] or row["cash_div"]
                if breakdown and per_share_pre_tax and per_share_pre_tax > 0:
                    for account_id, quantity in sorted(
                        breakdown.items(),
                        key=lambda item: (item[0] is None, item[0] or 0),
                    ):
                        valid_identities.add(("CASH_DIVIDEND", account_id))
                        estimated_total = per_share_pre_tax * quantity
                        match = match_existing_action(
                            row, "CASH_DIVIDEND", estimated_total, existing_actions,
                            match_window_days=settings.dividend_sync_match_window_days,
                            broker_account_id=account_id,
                        )
                        outcome = _upsert_suggestion(
                            db, user_id,
                            identity={
                                "symbol": symbol, "market": market,
                                "action_type": "CASH_DIVIDEND",
                                "ex_date": row["ex_date"],
                                "broker_account_id": account_id,
                            },
                            values={
                                "ann_date": row["ann_date"],
                                "record_date": row["record_date"],
                                "pay_date": row["pay_date"],
                                "currency": "CNY",
                                "cash_div_pre_tax": per_share_pre_tax,
                                "cash_div_after_tax": row["cash_div"],
                                "record_date_quantity": quantity,
                                "quantity_basis": basis,
                                "estimated_total_dividend": estimated_total,
                                "status": "MATCHED" if match else "NEW",
                                "matched_corporate_action_id": (
                                    match["matched_action_id"] if match else None
                                ),
                                "match_detail": match,
                            },
                        )
                        _count_outcome(result, outcome, match)

                if breakdown and row["stk_div"] and row["stk_div"] > 0:
                    valid_identities.add(("STOCK_DIVIDEND", None))
                    total_quantity = sum(breakdown.values(), Decimal("0"))
                    match = match_existing_action(
                        row, "STOCK_DIVIDEND", None, existing_actions,
                        match_window_days=settings.dividend_sync_match_window_days,
                        broker_account_id=None,
                    )
                    outcome = _upsert_suggestion(
                        db, user_id,
                        identity={
                            "symbol": symbol, "market": market,
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
                            "matched_corporate_action_id": (
                                match["matched_action_id"] if match else None
                            ),
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
            db.commit()
        except Exception as exc:  # 单标的失败不中断
            db.rollback()
            logger.warning("Dividend sync failed for %s (%s): %s", symbol, market, exc)
            result["failed"].append({"symbol": symbol, "market": market, "error": str(exc)[:200]})

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
    suggestion = db.query(CorporateActionSuggestion).filter(
        CorporateActionSuggestion.id == suggestion_id,
        CorporateActionSuggestion.user_id == user.id,
    ).first()
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
    current_actions = db.query(CorporateAction).filter(
        CorporateAction.user_id == user.id,
        CorporateAction.symbol == suggestion.symbol,
        CorporateAction.market == suggestion.market,
    ).all()
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
        action_kwargs.update({
            "dividend_per_share": per_share,
            "total_dividend": gross,
            "tax_withheld": tax,
            "net_dividend": gross - tax,
        })
    else:  # STOCK_DIVIDEND：ratio 优先级语义（semantics.bonus_share_factor）
        stk_div = Decimal(str(suggestion.stk_div_per_share or 0))
        # 每股送转 → "10:N" 基数比例（format 'f' 防 normalize 产生科学计数法）
        bonus_per_ten = format((stk_div * 10).normalize(), "f")
        action_kwargs["distribution_ratio"] = f"10:{bonus_per_ten}"

    db_action = CorporateAction(**action_kwargs)
    db.add(db_action)
    db.flush()

    if suggestion.action_type == "STOCK_DIVIDEND":
        recalculate_holdings(
            db, user.id, suggestion.symbol, suggestion.market, commit=False
        )

    suggestion.status = "ACCEPTED"
    suggestion.created_corporate_action_id = db_action.id
    db.commit()
    db.refresh(db_action)
    return db_action


def _locked_suggestion(
    db: Session, user_id: int, suggestion_id: int
) -> CorporateActionSuggestion:
    """记录锁内取回建议行（锁后重读，保证看到并发提交的最新状态）。"""
    lock_record(db, "ca-suggestion-record", suggestion_id)
    suggestion = db.query(CorporateActionSuggestion).filter(
        CorporateActionSuggestion.id == suggestion_id,
        CorporateActionSuggestion.user_id == user_id,
    ).first()
    if suggestion is None:
        raise LookupError("分红建议不存在")
    db.refresh(suggestion)
    return suggestion


def ignore_suggestion(
    db: Session, user_id: int, suggestion_id: int
) -> CorporateActionSuggestion:
    """忽略建议（幂等）。锁内重读做条件转换：已接受入账的不能忽略——
    否则 accept/ignore 竞态会留下 status=IGNORED 但账本记录已存在的矛盾态。"""
    suggestion = _locked_suggestion(db, user_id, suggestion_id)
    if suggestion.status == "ACCEPTED":
        raise SuggestionStateError("建议已接受入账，不能忽略")
    suggestion.status = "IGNORED"
    db.commit()
    db.refresh(suggestion)
    return suggestion


def restore_suggestion(
    db: Session, user_id: int, suggestion_id: int
) -> CorporateActionSuggestion:
    """恢复被忽略的建议到忽略前的原状态（锁内条件转换）。

    曾匹配到账本记录的回 MATCHED（保留关联，防止经"忽略→恢复"洗成可入账
    的 NEW 造成双计），否则回 NEW。
    """
    suggestion = _locked_suggestion(db, user_id, suggestion_id)
    if suggestion.status != "IGNORED":
        raise SuggestionStateError("仅已忽略的建议可以恢复")
    suggestion.status = (
        "MATCHED" if suggestion.matched_corporate_action_id is not None else "NEW"
    )
    db.commit()
    db.refresh(suggestion)
    return suggestion
