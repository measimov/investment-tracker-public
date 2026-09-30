"""跟踪标的的日线尾部同步 + A股估值快照（daily_basic）每日刷新。

两个周期任务都只服务**跟踪范围**（全体活跃用户的持仓 ∪ 自选，
`security_industry_service.scope_keys`），并用 `scheduled_state` 在库里记进度——
重启不重跑、同一交易日不重复外呼：

- `periodic_sync_price_tails`（每小时）：A股/B股/美股的日线尾部推进到该市场最近一个
  已完成交易日（港股由港交所日报负责）。已最新的标的纯查库跳过；落后的走
  `fetch_and_store_security_price_history_incremental`（Tushare → Tiingo → 腾讯兜底）。
  自选标的顺带把历史回补到加入日之前 7 天，供「加入以来涨跌幅」按加入日收盘取基准。
  每个标的每个目标交易日**成功**一次即不再请求（停牌日数据源返回空也是成功）；真实失败
  下个 tick 重试，同一交易日最多 MAX_TAIL_ATTEMPTS 次；配额错误中止整轮。本轮有任何失败
  即报 failed（部分成功不能把失败告警清掉）。
- `periodic_refresh_daily_basic`（每小时 tick，业务时区 18:00 后才处理当日）：一次
  `daily_basic(trade_date=…)` 取全市场，只存跟踪中的 A 股，每标的保留最近 30 行。
  观察清单与持仓的 A 股格雷厄姆估值（PE_TTM/PB）因此每个交易日更新，不再依赖跑分析。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, tuple_
from sqlalchemy.orm import Session

from ..config import settings
from ..core.logging import get_app_logger
from ..core.timeutil import business_timezone, local_today, to_local_date
from ..database import SessionLocal
from ..models.security_price import SecurityPrice
from ..models.user import User
from ..models.watchlist_item import WatchlistItem
from . import scheduled_state
from .job_worker import PeriodicOutcome, periodic_outcome_task
from .stock_price_service import tushare_configured

logger = get_app_logger(__name__)

Key = Tuple[str, str]

TAIL_TASK_NAME = "sync_price_tails"
DAILY_BASIC_TASK_NAME = "refresh_daily_basic"
PERIODIC_INTERVAL_SECONDS = 3600

# 港股尾部由港交所日报（hkex_dayquot_source）推进，这里不重复外呼
TAIL_MARKETS = ("A股", "B股", "美股")
MAX_KEYS_PER_TICK = 40
# 同一目标交易日内，真实失败（超时/5xx/全部兜底源失败）最多重试的次数（含首次，按小时一次）
MAX_TAIL_ATTEMPTS = 3
# 无任何日线的标的首次回补的天数（持仓标的的深历史由用户触发的 history-sync 负责）
COLD_START_DAYS = 30
# 自选标的回补到加入日之前多少天（加入日可能是非交易日）
WATCHLIST_LEAD_DAYS = 7
# 业务时区几点之后才去取「今天」的 daily_basic（Tushare 约 17 点后发布）
DAILY_BASIC_READY_HOUR = 18


def format_price_key(key: Key) -> str:
    return f"{key[0]}|{key[1]}"


def _tracked_keys(db: Session, markets: Tuple[str, ...]) -> List[Key]:
    from .security_industry_service import scope_keys

    return [key for key in scope_keys(db) if key[1] in markets]


def _coverage(db: Session, keys: List[Key]) -> Dict[Key, Tuple[date, date]]:
    if not keys:
        return {}
    rows = (
        db.query(
            SecurityPrice.symbol,
            SecurityPrice.market,
            func.min(SecurityPrice.price_date),
            func.max(SecurityPrice.price_date),
        )
        .filter(tuple_(SecurityPrice.symbol, SecurityPrice.market).in_(keys))
        .group_by(SecurityPrice.symbol, SecurityPrice.market)
        .all()
    )
    return {(symbol, market): (start, end) for symbol, market, start, end in rows}


def _coverage_end(db: Session, key: Key) -> Optional[date]:
    return (
        db.query(func.max(SecurityPrice.price_date))
        .filter(SecurityPrice.symbol == key[0], SecurityPrice.market == key[1])
        .scalar()
    )


def _watchlist_added_dates(db: Session, keys: List[Key]) -> Dict[Key, date]:
    """活跃用户自选的最早加入日（业务时区日期）。"""
    if not keys:
        return {}
    rows = (
        db.query(WatchlistItem.symbol, WatchlistItem.market, func.min(WatchlistItem.created_at))
        .join(User, User.id == WatchlistItem.user_id)
        .filter(
            User.is_active.is_(True),
            tuple_(WatchlistItem.symbol, WatchlistItem.market).in_(keys),
        )
        .group_by(WatchlistItem.symbol, WatchlistItem.market)
        .all()
    )
    return {
        (symbol, market): to_local_date(created)
        for symbol, market, created in rows
        if created is not None
    }


# 各市场当地时间几点之后，当天的日线收盘在数据源里可取（收盘后留出数据源入库时间）
TAIL_CLOSE_READY = {"A股": 16, "B股": 16, "美股": 17}


def tail_target_date(market: str, last_completed, *, now_utc: Optional[datetime] = None) -> date:
    """该市场应同步到的交易日：当地收盘就绪后含当天，否则为上一个已完成交易日。

    `get_last_completed_trading_day` 缺省以（UTC 容器的）今天为界且不含今天——A股当天的
    收盘要到次日北京时间 8 点才会被同步，当天收盘后的收益曲线/当日损益都用不上。
    """
    from .market_sessions import market_timezone

    tz = market_timezone(market)
    moment = now_utc or datetime.now(timezone.utc)
    if tz is None:
        return last_completed(market)
    local = moment.astimezone(tz)
    reference = (
        local.date() + timedelta(days=1)
        if local.hour >= TAIL_CLOSE_READY.get(market, 24)
        else local.date()
    )
    return last_completed(market, today=reference)


def plan_tail_targets(
    keys: List[Key],
    coverage: Dict[Key, Tuple[date, date]],
    added_dates: Dict[Key, date],
    targets: Dict[str, date],
    attempted: Dict[str, str],
    today: date,
    head_probed: Optional[Dict[str, str]] = None,
    failures: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """纯函数：哪些标的要补、从哪天补起。

    - 尾部落后于该市场最近已完成交易日，或自选标的的覆盖起点晚于加入日 − 7 天 → 要补；
    - 本目标交易日已成功同步过（attempted）→ 跳过，停牌标的不会每小时重打；
    - 本目标交易日已失败 MAX_TAIL_ATTEMPTS 次（failures）→ 当日不再试，明天随新目标日重来；
    - 自选的头部（加入日 − 7 天）每个起点只探一次（head_probed）：起点落在周末/节假日或
      上市日之前时覆盖起点永远晚于它，不记下来就会每个交易日多打一次请求；
    - 起点：已有覆盖取覆盖起点（只补尾），自选再往前延到加入日 − 7 天；
      没有任何行时取 min(今天 − 30 天, 加入日 − 7 天)。
    """
    plan: List[Dict[str, Any]] = []
    for key in keys:
        target = targets.get(key[1])
        if target is None:
            continue
        if attempted.get(format_price_key(key)) == target.isoformat():
            continue
        failure = (failures or {}).get(format_price_key(key)) or {}
        if (
            failure.get("target") == target.isoformat()
            and int(failure.get("count") or 0) >= MAX_TAIL_ATTEMPTS
        ):
            continue
        added = added_dates.get(key)
        watch_start = added - timedelta(days=WATCHLIST_LEAD_DAYS) if added else None
        existing = coverage.get(key)
        if existing is None:
            start = today - timedelta(days=COLD_START_DAYS)
            if watch_start is not None:
                start = min(start, watch_start)
        else:
            coverage_start, coverage_end = existing
            head_gap = (
                watch_start is not None
                and coverage_start > watch_start
                and (head_probed or {}).get(format_price_key(key)) != watch_start.isoformat()
            )
            if coverage_end >= target and not head_gap:
                continue
            start = min(coverage_start, watch_start) if head_gap else coverage_start
        plan.append(
            {
                "symbol": key[0],
                "market": key[1],
                "start_date": start,
                "target": target,
                "head_probe": watch_start.isoformat() if watch_start is not None else None,
            }
        )
    return plan


def sync_price_tails(
    db: Session, *, max_keys: int = MAX_KEYS_PER_TICK, now_utc: Optional[datetime] = None
) -> Dict[str, Any]:
    """一轮尾部同步；返回 {planned, synced, failed, quota_error, errors}。"""
    from .market_data_service import (
        fetch_and_store_security_price_history_incremental,
        get_last_completed_trading_day,
    )
    from .stock_price_service import is_tushare_quota_failure

    keys = _tracked_keys(db, TAIL_MARKETS)
    summary: Dict[str, Any] = {
        "tracked": len(keys),
        "planned": 0,
        "synced": 0,
        "failed": 0,
        "incomplete": 0,
        "quota_error": None,
        "errors": [],
    }
    if not keys:
        return summary

    targets = {
        market: tail_target_date(market, get_last_completed_trading_day, now_utc=now_utc)
        for market in sorted({key[1] for key in keys})
    }
    state_detail = scheduled_state.get_detail(db, TAIL_TASK_NAME)
    attempted_prev = state_detail.get("attempted") or {}
    head_probed_prev = state_detail.get("head_probed") or {}
    failures_prev = state_detail.get("failures") or {}
    # 只保留仍指向当前目标交易日的标记，旧交易日的自然作废（明细不随时间膨胀）
    current_targets = {key: targets[key[1]].isoformat() for key in keys}
    attempted = {
        key_str: target
        for key_str, target in attempted_prev.items()
        if target in current_targets.values()
    }
    failures = {
        key_str: entry
        for key_str, entry in failures_prev.items()
        if isinstance(entry, dict) and entry.get("target") in current_targets.values()
    }
    plan = plan_tail_targets(
        keys,
        _coverage(db, keys),
        _watchlist_added_dates(db, keys),
        targets,
        attempted,
        local_today(),
        head_probed_prev,
        failures,
    )
    # 只保留仍在跟踪范围里的标的
    key_strs = {format_price_key(key) for key in keys}
    head_probed = {k: v for k, v in head_probed_prev.items() if k in key_strs}
    summary["planned"] = len(plan)

    for item in plan[:max_keys]:
        key = (item["symbol"], item["market"])
        try:
            result = fetch_and_store_security_price_history_incremental(
                db,
                symbol=item["symbol"],
                market=item["market"],
                start_date=item["start_date"],
                end_date=item["target"],
                # 目标日已按当地收盘就绪判定过（可能是当天）：底层不能再按 UTC 今天截回昨天
                completed_through=item["target"],
            )
        except Exception as exc:  # 单标的异常不拖垮整轮
            db.rollback()
            result = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        error = result.get("error")
        if is_tushare_quota_failure(result):
            summary["quota_error"] = str(error)[:200]
            logger.warning("日线尾部同步命中 Tushare 配额/权限错误，本轮中止: %s", str(error)[:200])
            break
        key_str = format_price_key(key)
        if result.get("success"):
            # 只有成功才算「头部已探过」；「本交易日已处理」还要求目标日真的入库了——
            # 收盘后数据源可能还没发布当天（成功但 0 行），那就下个 tick 再取，
            # 满 MAX_TAIL_ATTEMPTS 次仍没有（停牌、数据源当天缺失）才放弃这一目标日
            if item.get("head_probe"):
                head_probed[key_str] = item["head_probe"]
            reached = _coverage_end(db, key)
            if reached is not None and reached >= item["target"]:
                attempted[key_str] = item["target"].isoformat()
                failures.pop(key_str, None)
                summary["synced"] += 1
            else:
                previous = failures.get(key_str) or {}
                count = (
                    int(previous.get("count") or 0)
                    if (previous.get("target") == item["target"].isoformat())
                    else 0
                )
                failures[key_str] = {
                    "target": item["target"].isoformat(),
                    "count": count + 1,
                    "incomplete": True,
                }
                if count + 1 >= MAX_TAIL_ATTEMPTS:
                    attempted[key_str] = item["target"].isoformat()
                    failures.pop(key_str, None)
                summary["incomplete"] += 1
        else:
            previous = failures.get(key_str) or {}
            count = (
                int(previous.get("count") or 0)
                if (previous.get("target") == item["target"].isoformat())
                else 0
            )
            failures[key_str] = {"target": item["target"].isoformat(), "count": count + 1}
            summary["failed"] += 1
            summary["errors"].append(f"{item['symbol']}/{item['market']}: {str(error or '')[:120]}")
            logger.warning(
                "日线尾部同步失败 %s/%s: %s", item["symbol"], item["market"], str(error or "")[:200]
            )

    # 收盘入库后把持仓/自选现价追平：错过收盘后那次盘中刷新时，现价会停在旧日期（#267）
    summary["quotes_caught_up"] = _catch_up_quotes(db, keys)
    scheduled_state.mark_ran(
        db,
        TAIL_TASK_NAME,
        success=summary["quota_error"] is None,
        detail={"attempted": attempted, "head_probed": head_probed, "failures": failures},
    )
    # 历史到位后顺带给没有基准价的存量自选按加入日收盘补（纯查库，幂等只填空值）
    try:
        from .watchlist_price_service import backfill_added_prices

        summary["backfilled"] = len(backfill_added_prices(db, head_probed=head_probed)["filled"])
    except Exception:  # noqa: BLE001 - 回填失败不影响尾部同步结果
        db.rollback()
        logger.exception("自选基准价回填失败")
    return summary


def _catch_up_quotes(db: Session, keys) -> int:
    """尽力而为：失败只记日志，不影响尾部同步本身的结果。"""
    from .stock_price_service import apply_latest_closes

    try:
        updated = apply_latest_closes(db, keys)
        db.commit()
        return updated
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("持仓现价追平最新收盘失败")
        return 0


@periodic_outcome_task
def periodic_sync_price_tails() -> PeriodicOutcome:
    """main.py 以名字 sync_price_tails 注册（每小时）。"""
    if not settings.price_tail_sync_enabled:
        return PeriodicOutcome.skipped("PRICE_TAIL_SYNC_ENABLED=false")
    db = SessionLocal()
    try:
        summary = sync_price_tails(db)
    finally:
        db.close()
    if summary["quota_error"]:
        return PeriodicOutcome.failed(f"Tushare 配额/权限：{summary['quota_error']}")
    if summary["failed"]:
        # 部分失败也报 failed：成功的标的不能把失败标的的告警清掉
        return PeriodicOutcome.failed(
            f"{summary['failed']} 只失败（成功 {summary['synced']}）："
            + "；".join(summary["errors"][:5]),
            count=summary["synced"],
        )
    if not summary["planned"]:
        return PeriodicOutcome.skipped("跟踪标的日线已是最新")
    return PeriodicOutcome.succeeded(summary["synced"])


# --------------------------------------------------------------------------- #
# A股估值快照 daily_basic
# --------------------------------------------------------------------------- #
def daily_basic_target_date(now_local: datetime) -> date:
    """应处理的交易日：18:00 前是上一个已完成交易日，18:00 后含今天。"""
    from .market_data_service import get_last_completed_trading_day

    today = now_local.date()
    reference = today + timedelta(days=1) if now_local.hour >= DAILY_BASIC_READY_HOUR else today
    return get_last_completed_trading_day("A股", today=reference)


def refresh_daily_basic(db: Session, *, now_local: Optional[datetime] = None) -> Dict[str, Any]:
    """返回 {status: skipped|succeeded, reason, trade_date, symbols, rows}；外呼异常上抛。"""
    from .security_profile_service import normalize_row, prune_daily_basic, upsert_profile_rows
    from .stock_price_service import TushareEmptyResult, to_tushare_a_code, tushare_query

    now_local = now_local or datetime.now(business_timezone())
    keys = _tracked_keys(db, ("A股",))
    if not keys:
        return {"status": "skipped", "reason": "没有跟踪中的 A 股"}
    target = daily_basic_target_date(now_local)
    detail = scheduled_state.get_detail(db, DAILY_BASIC_TASK_NAME)
    if detail.get("last_trade_date") == target.isoformat():
        return {"status": "skipped", "reason": f"{target.isoformat()} 已处理", "trade_date": target}

    try:
        df = tushare_query("daily_basic", trade_date=target.strftime("%Y%m%d"))
        records = df.to_dict("records")
    except TushareEmptyResult:
        records = []  # 空数据：当日尚未发布（上游故障照常上抛，不再被当成「未发布」）
    if not records:
        return {
            "status": "skipped",
            "reason": f"{target.isoformat()} 的 daily_basic 尚未发布",
            "trade_date": target,
        }

    symbol_by_code = {to_tushare_a_code(symbol): symbol for symbol, _market in keys}
    stored = 0
    for raw in records:
        symbol = symbol_by_code.get(str(raw.get("ts_code") or "").upper())
        if symbol is None:
            continue
        upsert_profile_rows(db, symbol, "A股", "daily_basic", [normalize_row(raw)])
        prune_daily_basic(db, symbol, "A股")
        stored += 1
    db.commit()
    scheduled_state.mark_ran(
        db,
        DAILY_BASIC_TASK_NAME,
        detail={"last_trade_date": target.isoformat(), "symbols": stored},
    )
    return {
        "status": "succeeded",
        "trade_date": target,
        "symbols": stored,
        "rows": len(records),
    }


@periodic_outcome_task
def periodic_refresh_daily_basic() -> PeriodicOutcome:
    """main.py 以名字 refresh_daily_basic 注册（每小时 tick）。"""
    if not settings.daily_basic_refresh_enabled:
        return PeriodicOutcome.skipped("DAILY_BASIC_REFRESH_ENABLED=false")
    if not tushare_configured():
        return PeriodicOutcome.skipped("未配置 TUSHARE_TOKEN")
    db = SessionLocal()
    try:
        result = refresh_daily_basic(db)
    except Exception as exc:
        db.rollback()
        return PeriodicOutcome.failed(f"daily_basic 刷新失败：{str(exc)[:200]}")
    finally:
        db.close()
    if result["status"] == "skipped":
        return PeriodicOutcome.skipped(result["reason"])
    return PeriodicOutcome.succeeded(result["symbols"])
