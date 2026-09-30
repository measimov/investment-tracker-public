"""每日按标的采集（收纳自 xueqiu-timeline-archiver 的 monitor_symbols，改为落库）。

一轮 = 标的范围内每只标的的公告流 + 讨论流（各最新 N 条）→ 已启用组合的调仓记录。
与作者采集同进程、同一把 advisory lock、同一个进程级限速时钟（10–25s/次）与同一个
WAF 冷却：

- **标的范围** = 全体活跃用户的持仓(quantity>0) ∪ 自选，∩ `OPINION_MARKETS`
  （A股/B股/港股/美股），减去各用户自己的 EXCLUDE / CASH_MANAGEMENT 规则——直接复用
  观点批量的 `candidate_opinion_targets`，逐用户取并集（被 A 排除、B 仍持有的标的照采）。
  原仓库是手工维护的 config/monitor_symbols.txt。
- 雪球 symbol 只在这里经 `to_xueqiu` 在内存里生成，落库的一律是本仓 (symbol, market)。
- 响应在入口严格校验（`feed_parsing.validate_feed_payload`）：错误对象（error_code /
  error_description / success=false）、未知结构、JSON null、非 JSON 都是**单项失败**，
  不是「当天恰好没数据」；只有端点认可的结构（{list: []} / {statuses: []}）才算成功空结果。
- 单项失败记入失败清单继续下一项；WAF 挑战页中止整轮并写 `last_waf_at`（作者轮次
  也随之冷却）。
- **业务日语义**（`_finish_daily`）：一轮无失败才把该业务日记为已跑；有失败（含 WAF、
  Cookie 不可用）则当天留一条待重试记录 `symbols_pending{date, attempts, items}`，
  items 是本轮**没成功**的项（失败的 + WAF 后没轮到的），距上一轮
  `symbols_retry_minutes` 后只重试这些项；当日尝试 `symbols_max_attempts` 轮（含首轮）
  仍有失败即记当日已跑，剩余失败项明日随整轮再采。停机中断不计次数、不动记录。
- 待重试记录里的项在重试前过 `known_work_items`：类型不认识的（含 2026-09-28 下线的
  热帖项）静默丢弃，不请求、不算失败。
- **不写 scan_runs**：观点页活性判据只看作者轮次，按标的采集的状态记在
  `xueqiu_collector_state.symbols_*`。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Optional

import requests
from sqlalchemy.orm import Session

from ...config import settings
from ...core.logging import get_app_logger
from ...core.timeutil import local_today
from ..job_runtime import rotate_by_market
from ...models.user import User
from ...models.xueqiu_collector import XueqiuCollectorCube
from . import state as st
from .client import (
    CollectorFetchError,
    CollectorUnavailable,
    WafChallenge,
    XueqiuWebClient,
    load_collector_cookies,
)
from .feed_parsing import (
    ENDPOINT_REBALANCING,
    KIND_LABELS,
    KINDS,
    feed_url,
    parse_rebalancings,
    parse_statuses,
    rebalancing_url,
    validate_feed_payload,
)
from .feed_store import upsert_rebalancing, upsert_symbol_posts

logger = get_app_logger(__name__)

MAX_FAILURES_KEPT = 20

# 一项工作（JSON 可序列化，待重试记录直接存它；只有本仓身份键，没有雪球 symbol）：
#   {"type": "feed", "symbol", "market", "kind"} / {"type": "cube", "cube_id"}
WorkItem = Dict[str, str]
WORK_ITEM_TYPES = frozenset({"feed", "cube"})


@dataclass(frozen=True)
class SymbolTarget:
    """一只被跟踪的标的。`xueqiu` 只活在内存里（请求参数），永不落库。"""

    symbol: str
    market: str
    xueqiu: str


def _opinion_markets() -> tuple:
    from ..opinion_summary_jobs import OPINION_MARKETS

    return OPINION_MARKETS


def compute_symbol_universe(db: Session) -> List[SymbolTarget]:
    """全体活跃用户的持仓∪自选 ∩ OPINION_MARKETS − 各自的排除/现金管理规则。"""
    from ..opinion_summary_batch_jobs import candidate_opinion_targets
    from ..xueqiu_source import to_xueqiu

    markets = _opinion_markets()
    pairs = set()
    user_ids = [
        row[0] for row in db.query(User.id).filter(User.is_active.is_(True)).order_by(User.id)
    ]
    for user_id in user_ids:
        for target in candidate_opinion_targets(db, user_id):
            if target["market"] in markets:
                pairs.add((target["symbol"], target["market"]))
    return [
        SymbolTarget(symbol=symbol, market=market, xueqiu=to_xueqiu(symbol, market))
        for symbol, market in rotate_by_market(list(pairs), markets)
    ]


def explicit_targets(symbols: List[str], market: str) -> List[SymbolTarget]:
    """`manage.py xueqiu-collector --symbols-once --symbol X --market M` 的显式目标。"""
    from ..symbol_normalization import normalize_manual_symbol
    from ..xueqiu_source import to_xueqiu

    if market not in _opinion_markets():
        raise ValueError(f"{market} 不在按标的采集支持的市场内（{'/'.join(_opinion_markets())}）")
    targets = []
    for raw in symbols:
        symbol = normalize_manual_symbol(raw, market)
        if symbol:
            targets.append(
                SymbolTarget(symbol=symbol, market=market, xueqiu=to_xueqiu(symbol, market))
            )
    return targets


# --------------------------------------------------------------------------- #
# 工作项
# --------------------------------------------------------------------------- #
def feed_items(targets: List[SymbolTarget]) -> List[WorkItem]:
    return [
        {"type": "feed", "symbol": target.symbol, "market": target.market, "kind": kind}
        for target in targets
        for kind in KINDS
    ]


def market_wide_items(db: Session) -> List[WorkItem]:
    """不绑定标的的工作项：已启用组合的调仓记录（市场热帖已于 2026-09-28 下线）。"""
    cubes = (
        db.query(XueqiuCollectorCube.cube_id)
        .filter(XueqiuCollectorCube.enabled.is_(True))
        .order_by(XueqiuCollectorCube.cube_id)
        .all()
    )
    return [{"type": "cube", "cube_id": row[0]} for row in cubes]


def known_work_items(items: List[Any]) -> List[WorkItem]:
    """待重试记录里仍受支持的工作项。已下线的类型（市场热帖 `hots`）与格式不对的
    条目静默丢弃——既不算失败，也不会再写回待重试记录被反复重试。"""
    return [
        dict(item)
        for item in items
        if isinstance(item, dict) and item.get("type") in WORK_ITEM_TYPES
    ]


def _item_label(item: WorkItem) -> str:
    if item["type"] == "feed":
        return f"{item['market']} {item['symbol']} {KIND_LABELS.get(item['kind'], item['kind'])}"
    return f"组合 {item['cube_id']} 调仓"


def _item_key(item: WorkItem) -> tuple:
    return tuple(sorted(item.items()))


# --------------------------------------------------------------------------- #
# 一轮
# --------------------------------------------------------------------------- #
@dataclass
class SymbolsCycleResult:
    status: str
    message: str = ""
    stats: Dict[str, Any] = field(default_factory=dict)
    failures: List[str] = field(default_factory=list)
    request_count: int = 0
    remaining: Optional[List[WorkItem]] = None


def _fetch_items(
    client: XueqiuWebClient, url: str, context: str, endpoint: str
) -> List[Dict[str, Any]]:
    """GET → 已校验的条目列表。HTTP 错误状态、非 JSON、错误对象与未知结构一律抛出
    （单项失败）；WAF 由 client 抛 `WafChallenge`。"""
    try:
        response = client.get(url, context=context)
        response.raise_for_status()
    except requests.RequestException as exc:  # 与 timeline.fetch_timeline_page 同口径
        raise CollectorFetchError(f"{context} 请求失败：{exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        snippet = (response.text or "")[:120].replace("\n", " ")
        raise CollectorFetchError(f"{context} 返回的不是 JSON（{snippet!r}）") from exc
    return validate_feed_payload(payload, endpoint)


def _empty_stats(symbol_count: int, mode: str) -> Dict[str, Any]:
    stats: Dict[str, Any] = {"mode": mode, "symbols": symbol_count, "failures": 0}
    for kind in KINDS:
        stats[kind] = {"fetched": 0, "new": 0}
    stats.update({"cubes": 0, "rebalancing_new": 0})
    return stats


def _summarize(stats: Dict[str, Any], failures: List[str]) -> str:
    parts = [f"标的 {stats.get('symbols', 0)} 只"]
    if stats.get("mode") == "retry":
        parts[0] = f"重试第 {stats.get('attempt', 0)} 轮：标的 {stats.get('symbols', 0)} 只"
    for kind in KINDS:
        item = stats.get(kind) or {}
        parts.append(f"{KIND_LABELS[kind]} {item.get('fetched', 0)} 条（新 {item.get('new', 0)}）")
    if stats.get("cubes"):
        parts.append(f"组合 {stats['cubes']} 个（新调仓 {stats.get('rebalancing_new', 0)}）")
    if failures:
        parts.append(f"失败 {len(failures)} 项：" + "；".join(failures[:5]))
    return "，".join(parts)


class _Stop(Exception):
    pass


def _run_item(
    db: Session, client: XueqiuWebClient, item: WorkItem, count: int, stats: Dict[str, Any]
) -> None:
    """执行一项并落库（commit）。失败抛出，由调用方记失败。"""
    from ..xueqiu_source import to_xueqiu

    label = _item_label(item)
    if item["type"] == "feed":
        kind = item["kind"]
        xueqiu = to_xueqiu(item["symbol"], item["market"])  # 仅请求参数，不落库
        items = _fetch_items(client, feed_url(kind, xueqiu, count), label, kind)
        posts = parse_statuses(items)
        new, _ = upsert_symbol_posts(db, item["symbol"], item["market"], kind, posts)
        db.commit()
        stats[kind]["fetched"] += len(posts)
        stats[kind]["new"] += new
        return
    if item["type"] == "cube":
        cube_id = item["cube_id"]
        try:
            items = _fetch_items(
                client, rebalancing_url(cube_id, count), label, ENDPOINT_REBALANCING
            )
            records = parse_rebalancings(items)
            new, _ = upsert_rebalancing(db, cube_id, records)
        except WafChallenge:
            raise
        except Exception as exc:
            db.rollback()
            _mark_cube(db, cube_id, st.RUN_FAILED, f"{type(exc).__name__}: {str(exc)[:400]}")
            raise
        _mark_cube(db, cube_id, st.RUN_OK, f"调仓 {len(records)} 条（新 {new}）")
        stats["cubes"] += 1
        stats["rebalancing_new"] += new
        return
    raise ValueError(f"未知的工作项类型: {item.get('type')!r}")


def _mark_cube(db: Session, cube_id: str, status: str, message: str) -> None:
    cube = db.get(XueqiuCollectorCube, cube_id)
    if cube is not None:
        cube.last_status = status
        cube.last_message = message[:500]
        cube.last_run_at = st.utcnow()
    db.commit()


def _finish_daily(
    db: Session,
    status: str,
    message: str,
    stats: Dict[str, Any],
    *,
    today: date,
    remaining: Optional[List[WorkItem]],
    waf_at,
) -> str:
    """按业务日语义收尾（见模块 docstring），返回最终写入的消息。"""
    from .runner import CYCLE_INTERRUPTED, CYCLE_OK

    if status == CYCLE_INTERRUPTED:  # 停机：不计次数、不动待重试记录，重启后按原计划
        st.mark_symbols_finished(db, status, message, stats=stats, waf_at=waf_at)
        return message
    if status == CYCLE_OK:
        st.mark_symbols_finished(
            db, status, message, stats=stats, business_date=today, waf_at=waf_at, pending=None
        )
        return message
    previous = st.todays_symbols_pending(st.get_state(db), today)
    attempts = int((previous or {}).get("attempts") or 0) + 1
    max_attempts = max(1, settings.xueqiu_collector_symbols_max_attempts)
    stats["attempt"] = attempts
    if attempts >= max_attempts:
        message = f"{message}；当日已尝试 {attempts} 轮，剩余失败项明日随整轮再采"
        st.mark_symbols_finished(
            db, status, message, stats=stats, business_date=today, waf_at=waf_at, pending=None
        )
        return message
    pending = {"date": today.isoformat(), "attempts": attempts, "items": remaining}
    message = (
        f"{message}；{settings.xueqiu_collector_symbols_retry_minutes} 分钟后重试"
        f"{'整轮' if remaining is None else f'未成功的 {len(remaining)} 项'}"
        f"（第 {attempts}/{max_attempts} 轮）"
    )
    st.mark_symbols_finished(db, status, message, stats=stats, waf_at=waf_at, pending=pending)
    return message


def run_symbols_cycle(
    db: Session,
    *,
    targets: Optional[List[SymbolTarget]] = None,
    include_market_wide: bool = True,
    record_daily: bool = True,
    retry_items: Optional[List[WorkItem]] = None,
    count: Optional[int] = None,
    client_factory: Optional[Callable[[Dict[str, str]], XueqiuWebClient]] = None,
    stop_event: Optional[threading.Event] = None,
    heartbeat: Optional[st.Heartbeat] = None,
    business_date: Optional[date] = None,
) -> SymbolsCycleResult:
    """跑一轮按标的采集。

    targets 为空 = 按范围计算；include_market_wide = 是否顺带组合调仓；
    retry_items = 只重试这些项（待重试记录里的，已下线类型经 `known_work_items` 丢弃）；
    record_daily = 是否按业务日语义收尾（手动指定标的的补跑不动业务日与待重试记录）。
    """
    from .runner import (
        CYCLE_FAILED,
        CYCLE_INTERRUPTED,
        CYCLE_LOCKED,
        CYCLE_OK,
        CYCLE_PARTIAL,
        CYCLE_UNAVAILABLE,
        CYCLE_WAF,
        CycleLock,
    )

    count = count or settings.xueqiu_collector_symbol_count
    stop_event = stop_event or threading.Event()
    lock = CycleLock(db)
    if not lock.acquire():
        logger.info("另一轮雪球采集正在运行（advisory lock 被占用），按标的采集稍后再试")
        return SymbolsCycleResult(status=CYCLE_LOCKED, message="另一轮采集正在运行")

    today = business_date or local_today()
    mode = "retry" if retry_items is not None else ("full" if record_daily else "manual")

    def finish(status: str, message: str, stats: Dict[str, Any], remaining, waf_at=None) -> str:
        if record_daily:
            return _finish_daily(
                db, status, message, stats, today=today, remaining=remaining, waf_at=waf_at
            )
        st.mark_symbols_finished(db, status, message, stats=stats, waf_at=waf_at)
        return message

    try:
        st.mark_symbols_started(db)
        try:
            cookies = load_collector_cookies()
        except CollectorUnavailable as exc:
            message = f"采集器不可用：{exc}"
            logger.warning(message)
            # 没发出任何请求：待重试项保持原样（重试轮）或整轮（首轮）
            message = finish(CYCLE_UNAVAILABLE, message, {"mode": mode}, retry_items)
            return SymbolsCycleResult(status=CYCLE_UNAVAILABLE, message=message)

        if retry_items is not None:
            work = known_work_items(retry_items)
        else:
            if targets is None:
                targets = compute_symbol_universe(db)
            work = feed_items(targets)
            if include_market_wide:
                work += market_wide_items(db)
        symbol_count = len({(i["symbol"], i["market"]) for i in work if i["type"] == "feed"})

        on_request = heartbeat.beat if heartbeat is not None else None
        client = (
            client_factory(cookies)
            if client_factory is not None
            else XueqiuWebClient(cookies, on_request=on_request)
        )
        stats = _empty_stats(symbol_count, mode)
        failures: List[str] = []
        succeeded: set = set()
        waf_at = None
        interrupted = False

        try:
            for index, item in enumerate(work, start=1):
                if stop_event.is_set():
                    raise _Stop()
                label = _item_label(item)
                try:
                    _run_item(db, client, item, count, stats)
                except WafChallenge:
                    raise
                except Exception as exc:  # noqa: BLE001 - 单项失败继续
                    db.rollback()
                    failures.append(f"{label}: {type(exc).__name__}: {str(exc)[:160]}")
                    logger.warning("按标的采集 %s 失败，已跳过：%s", label, exc)
                    continue
                succeeded.add(_item_key(item))
                if index % 20 == 0:
                    logger.info("按标的采集进度 %s/%s", index, len(work))
        except WafChallenge as exc:
            db.rollback()
            waf_at = st.utcnow()
            failures.append(str(exc))
            logger.warning(
                "按标的采集命中 WAF，停止本轮；%ss 冷却期内不开新一轮",
                settings.xueqiu_collector_waf_cooldown_seconds,
            )
        except _Stop:
            db.rollback()
            interrupted = True

        # 没成功的项 = 失败的 + WAF/停机后没轮到的
        remaining = [item for item in work if _item_key(item) not in succeeded]
        stats["failures"] = len(failures)
        if waf_at is not None:
            status = CYCLE_WAF
        elif interrupted:
            status = CYCLE_INTERRUPTED
        elif not failures:
            status = CYCLE_OK
        elif succeeded:
            status = CYCLE_PARTIAL
        else:
            status = CYCLE_FAILED
        if retry_items is not None:
            previous = st.todays_symbols_pending(st.get_state(db), today)
            stats["attempt"] = int((previous or {}).get("attempts") or 0) + 1
        message = _summarize(stats, failures)
        stats["failure_samples"] = failures[:MAX_FAILURES_KEPT]
        message = finish(status, message, stats, remaining, waf_at=waf_at)
        logger.info("雪球按标的采集一轮结束：%s %s", status, message)
        return SymbolsCycleResult(
            status=status,
            message=message,
            stats=stats,
            failures=failures,
            request_count=getattr(client, "request_count", 0),
            remaining=remaining,
        )
    finally:
        lock.release()


def maybe_run_symbols_cycle(
    db: Session,
    *,
    stop_event: Optional[threading.Event] = None,
    heartbeat: Optional[st.Heartbeat] = None,
    client_factory: Optional[Callable[[Dict[str, str]], XueqiuWebClient]] = None,
) -> Optional[SymbolsCycleResult]:
    """常驻循环的每日钩子：到点、到重试时间或管理员请求才跑，否则返回 None。"""
    if not settings.xueqiu_collector_symbols_enabled:
        return None
    state = st.get_state(db)
    now = st.utcnow()
    due, reason = st.symbols_cycle_due(
        state,
        now,
        run_after=st.parse_run_after(settings.xueqiu_collector_symbols_run_after),
        waf_cooldown_seconds=settings.xueqiu_collector_waf_cooldown_seconds,
        retry_minutes=settings.xueqiu_collector_symbols_retry_minutes,
    )
    retry_items = None
    if due and reason == "retry":
        pending = st.todays_symbols_pending(state, local_today()) or {}
        items = pending.get("items")
        retry_items = items if isinstance(items, list) else None  # None = 整轮重跑
    db.rollback()
    if not due:
        return None
    logger.info("开始一轮雪球按标的采集（%s）", reason)
    return run_symbols_cycle(
        db,
        stop_event=stop_event,
        heartbeat=heartbeat,
        retry_items=retry_items,
        client_factory=client_factory,
    )
