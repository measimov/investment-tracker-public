"""作者采集一轮（`run_authors_cycle`）与常驻循环（`run_collector_loop`）。

常驻循环每个轮询点先看作者轮次是否到期，再看每日按标的采集（`symbols.py`）是否到期；
两者共用本模块的 advisory lock、进程级限速时钟与 WAF 冷却，串行执行。

一位作者的流程移植自 scan_user_replies 监控模式（`--monitor-user-id`）：

1. 主页时间线（type=3，回看 monitor_days 天）→ 主页发言入库 + 候选帖；
2. 候选帖并入 posts 表，未补全文的抓全文页（lxml）；
3. 逐帖翻评论（冷却期内跳过，旧帖只翻最新几页），命中目标作者的回复写 replies +
   utterances，每页更新 post_scan_state。

丢弃：checkpoint 导入与 Markdown 导出（数据只进库）。

与原 shell 编排（monitor_all_users.sh）的对应与差异：
- mkdir 锁 → PostgreSQL advisory lock（进程崩溃自动释放，不留死锁目录）；
- 作者之间随机间隔 [gap_min, gap_max] 秒、每轮最多 N 位（最久没跑的优先）；
- 原来靠 grep 日志里的 "WAF" 停批 → `WafChallenge` 异常直接中止本轮，冷却期
  （waf_cooldown）内循环不开新一轮；
- 原来 scan_runs 建表不写 → 每位作者每轮一行（状态/错误/是否 WAF）；
- 作者 30 天内没有任何动态：原脚本 SystemExit 记失败，这里记 ok（候选 0）。
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...core.logging import get_app_logger
from . import cookie_health
from . import state as st
from .client import (
    CollectorFetchError,
    CollectorUnavailable,
    WafChallenge,
    XueqiuWebClient,
    load_collector_cookies,
)
from .comments import comment_page_limit_for_post, scan_post
from .common import CandidatePost, reply_dedupe_key
from .detail import DETAIL_FAILED_PREFIX, fetch_post_detail, should_use_fetched_detail
from .state import AuthorResult
from .store import ArchiverStore
from .timeline import collect_profile, cutoff_timestamp_ms

logger = get_app_logger(__name__)

# 全仓唯一的 advisory lock 键（字符串经 hashtext 取 int4，与 holding_service 的键空间不撞）
CYCLE_LOCK_NAME = "xueqiu-collector:authors-cycle"

CYCLE_OK = "ok"
CYCLE_PARTIAL = "partial"
CYCLE_FAILED = "failed"
CYCLE_WAF = "waf"
CYCLE_UNAVAILABLE = "unavailable"
CYCLE_LOCKED = "locked"
CYCLE_NO_AUTHORS = "no_authors"
CYCLE_INTERRUPTED = "interrupted"


class Interrupted(Exception):
    """收到停止信号：在帖子/作者边界退出。"""


@dataclass
class CollectorKnobs:
    monitor_days: int
    profile_pages: int
    max_comment_pages: int
    stale_post_days: int
    stale_comment_pages: int
    rescan_cooldown_hours: float
    waf_cooldown_seconds: float
    max_waf_hits: int
    author_gap_min: int
    author_gap_max: int
    max_authors: int
    # 低成本验证（影子运行）：只翻主页第 1 页、只处理最新 N 个候选帖；None = 不限
    max_posts: Optional[int] = None

    @classmethod
    def from_settings(cls) -> "CollectorKnobs":
        return cls(
            monitor_days=settings.xueqiu_collector_monitor_days,
            profile_pages=settings.xueqiu_collector_profile_pages,
            max_comment_pages=settings.xueqiu_collector_max_comment_pages,
            stale_post_days=settings.xueqiu_collector_stale_post_days,
            stale_comment_pages=settings.xueqiu_collector_stale_comment_pages,
            rescan_cooldown_hours=settings.xueqiu_collector_rescan_cooldown_hours,
            waf_cooldown_seconds=settings.xueqiu_collector_waf_cooldown_seconds,
            max_waf_hits=settings.xueqiu_collector_max_waf_hits,
            author_gap_min=settings.xueqiu_collector_author_gap_min_seconds,
            author_gap_max=max(
                settings.xueqiu_collector_author_gap_min_seconds,
                settings.xueqiu_collector_author_gap_max_seconds,
            ),
            max_authors=settings.xueqiu_collector_max_authors_per_run,
        )


@dataclass
class CycleResult:
    status: str
    message: str = ""
    authors: List[AuthorResult] = field(default_factory=list)
    cookie: Optional[Dict[str, Any]] = None
    request_count: int = 0


class _Pacer:
    """可中断的停顿：按块等待 stop_event，块间写心跳（长停顿不让 healthcheck 误判）。"""

    def __init__(self, stop_event: Optional[threading.Event], heartbeat: Optional[st.Heartbeat],
                 chunk_seconds: float = 60.0) -> None:
        self.stop_event = stop_event or threading.Event()
        self.heartbeat = heartbeat
        self.chunk_seconds = chunk_seconds

    def stopped(self) -> bool:
        return self.stop_event.is_set()

    def pause(self, seconds: float) -> None:
        remaining = max(0.0, float(seconds))
        while remaining > 0:
            step = min(self.chunk_seconds, remaining)
            if self.stop_event.wait(step):
                raise Interrupted()
            remaining -= step
            if self.heartbeat is not None:
                self.heartbeat.beat()


def should_fetch_candidate_detail(post: CandidatePost) -> bool:
    if not post.author_id and post.text and "/S/" not in post.url:
        return False
    return True


def enrich_candidates(
    client: XueqiuWebClient, store: ArchiverStore, candidates: List[CandidatePost],
    pacer: _Pacer, failures: List[str],
) -> List[CandidatePost]:
    """未补全文的候选帖抓全文页（已补全的以库内版本为准）。

    全文页请求失败（网络/HTTP 错误，`【抓取失败】…`）时**不标记已补全**：原实现照样
    写 detail_enriched=true，帖子从此永不重抓，失败也不留痕。这里保留候选摘要、记入
    failures，下一轮重试。页面正常返回却没有正文区（已删帖/仅自己可见）仍按原语义
    视为补全完成。
    """
    existing = store.load_posts(post.post_id for post in candidates)
    enriched: List[CandidatePost] = []
    for post in candidates:
        if post.post_id in existing and existing[post.post_id][1]:
            enriched.append(existing[post.post_id][0])
            continue
        if pacer.stopped():
            raise Interrupted()
        if not should_fetch_candidate_detail(post):
            store.upsert_post(post, detail_enriched=True)
            store.commit()
            enriched.append(post)
            continue
        detail = fetch_post_detail(client, post.url)
        if detail.startswith(DETAIL_FAILED_PREFIX):
            logger.warning("原帖 %s 全文抓取失败，下一轮重试：%s", post.post_id, detail[:160])
            failures.append(f"全文 {post.post_id}: {detail[:160]}")
            enriched.append(post)
            continue
        if should_use_fetched_detail(detail):
            post = replace(post, text=detail)
        elif not post.text:
            post = replace(post, text=detail)
        else:
            logger.info("原帖 %s 详情页没有正文，保留候选来源已有内容", post.post_id)
        store.upsert_post(post, detail_enriched=True)
        store.commit()
        enriched.append(post)
    return enriched


def _finalize_status(result: AuthorResult, failures: List[str]) -> AuthorResult:
    """有局部失败（时间线后续页/全文页/评论页）的作者记 partial 并写明原因——
    首屏已成功，这一轮不是一无所获，但也不能记成完整成功。"""
    if failures and result.status == st.RUN_OK:
        result.status = st.RUN_PARTIAL
        head = "；".join(failures[:3])
        more = f"（另 {len(failures) - 3} 处）" if len(failures) > 3 else ""
        result.error = f"{len(failures)} 处抓取失败：{head}{more}"[:2000]
    return result


def run_author(
    client: XueqiuWebClient,
    store: ArchiverStore,
    author_id: str,
    knobs: CollectorKnobs,
    pacer: _Pacer,
) -> AuthorResult:
    """一位作者的完整采集。

    - 时间线首页拿不到合法响应 → 抛 CollectorFetchError（调用方记 error，不是成功）；
      只有合法的 `statuses: []` 才算作者近期无动态（ok）。
    - 时间线后续页、全文页、评论页的失败 → 保留已取到的数据，作者记 partial。
    - WAF：时间线/全文阶段向上抛；评论阶段按 max_waf_hits 处理。
    """
    result = AuthorResult(author_id=author_id)
    failures: List[str] = []
    since_ms = cutoff_timestamp_ms(knobs.monitor_days)
    pages = 1 if knobs.max_posts else knobs.profile_pages
    candidates, utterances, timeline_error = collect_profile(
        client, author_id, pages=pages, since_ms=since_ms
    )
    if timeline_error:
        failures.append(timeline_error)
    if knobs.max_posts:
        candidates = candidates[: knobs.max_posts]
    for utterance in utterances:
        store.upsert_utterance(utterance)
        result.utterance_keys.append(utterance.utterance_key)
    store.commit()
    result.utterance_count = len(utterances)
    logger.info("作者 %s 主页发言入库 %s 条，候选帖 %s", author_id, len(utterances), len(candidates))

    candidates = store.merge_candidates(candidates)
    candidates = enrich_candidates(client, store, candidates, pacer, failures)
    candidates = store.merge_candidates(candidates)
    result.candidate_count = len(candidates)

    reply_since_ms = cutoff_timestamp_ms(knobs.monitor_days)
    waf_hits = 0
    for post in candidates:
        if pacer.stopped():
            result.status = st.RUN_INTERRUPTED
            result.stopped_early = True
            return result
        if store.should_skip_recent_scan(author_id, post.post_id, knobs.rescan_cooldown_hours):
            continue
        max_pages = comment_page_limit_for_post(
            post, knobs.max_comment_pages, knobs.stale_post_days, knobs.stale_comment_pages
        )
        try:
            matches = scan_post(
                client, post, author_id, sink=store, max_pages=max_pages, since_ms=reply_since_ms
            )
        except WafChallenge as exc:
            waf_hits += 1
            logger.warning("%s。WAF 命中次数：%s", exc, waf_hits)
            if knobs.max_waf_hits > 0 and waf_hits >= knobs.max_waf_hits:
                result.status = st.RUN_WAF
                result.waf = True
                result.stopped_early = True
                result.error = str(exc)
                return result
            pacer.pause(knobs.waf_cooldown_seconds)
            continue
        except Interrupted:
            raise
        except Exception as exc:  # noqa: BLE001 - 单帖失败不拖垮作者，但必须留痕
            store.db.rollback()
            logger.warning("帖子 %s 评论扫描失败，下一轮重扫。错误=%s", post.post_id, exc)
            failures.append(f"评论 {post.post_id}: {exc}"[:200])
            store.clear_scan_time(author_id, post.post_id)
            store.commit()
            continue
        result.reply_count += len(matches)
        for reply in matches:
            result.utterance_keys.append(f"comment:{reply.comment_id or reply_dedupe_key(reply)}")
    return _finalize_status(result, failures)


# --------------------------------------------------------------------------- #
# advisory lock
# --------------------------------------------------------------------------- #
class _CycleLock:
    """会话级 advisory lock，持有一条独立连接直到释放。

    不能挂在采集 Session 上：Session 每次 commit 后可能把连接还回连接池、下次换一条，
    会话级锁留在旧连接上，解锁时就解不到了。"""

    def __init__(self, db: Session) -> None:
        self.engine = db.get_bind()
        self.conn = None

    def acquire(self) -> bool:
        conn = self.engine.connect()
        got = conn.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:name))"), {"name": CYCLE_LOCK_NAME}
        ).scalar()
        conn.commit()
        if not got:
            conn.close()
            return False
        self.conn = conn
        return True

    def release(self) -> None:
        if self.conn is None:
            return
        try:
            self.conn.execute(
                text("SELECT pg_advisory_unlock(hashtext(:name))"), {"name": CYCLE_LOCK_NAME}
            )
            self.conn.commit()
            self.conn.close()
        except Exception:  # noqa: BLE001 - 解锁失败就把物理连接丢掉，锁随会话释放
            self.conn.invalidate()
        finally:
            self.conn = None


def _summarize(results: List[AuthorResult]) -> str:
    parts = []
    for item in results:
        piece = f"{item.author_id}:{item.status}"
        if item.status in st.LIVE_RUN_STATUSES:
            piece += f"(候选{item.candidate_count}/回复{item.reply_count}/发言{item.utterance_count})"
        if item.error:
            piece += f"({item.error[:80]})"
        parts.append(piece)
    return "；".join(parts)


def _push(status: str, message: str) -> None:
    url = (settings.xueqiu_collector_push_url or "").strip()
    if not url:
        return
    try:
        cookie_health.push_status(url, status=status, message=message)
    except Exception as exc:  # noqa: BLE001 - 推送失败只记日志
        logger.warning("Uptime Kuma 推送失败: %s", exc)


def run_authors_cycle(
    db: Session,
    *,
    author_ids: Optional[List[str]] = None,
    dry_run: bool = False,
    knobs: Optional[CollectorKnobs] = None,
    client_factory: Optional[Callable[[Dict[str, str]], XueqiuWebClient]] = None,
    stop_event: Optional[threading.Event] = None,
    heartbeat: Optional[st.Heartbeat] = None,
    rng: Optional[random.Random] = None,
) -> CycleResult:
    """跑一轮作者采集。返回 CycleResult；运行记录写 scan_runs / 作者行 / 状态行
    （dry_run 时一律不写，只抓取解析并汇总将要写入的键）。"""
    knobs = knobs or CollectorKnobs.from_settings()
    rng = rng or random.Random()
    pacer = _Pacer(stop_event, heartbeat)
    lock = _CycleLock(db)
    if not lock.acquire():
        logger.warning("另一轮雪球采集正在运行（advisory lock 被占用），本次跳过")
        return CycleResult(status=CYCLE_LOCKED, message="另一轮采集正在运行")

    try:
        if not dry_run:
            orphans = st.close_orphan_runs(db)
            if orphans:
                logger.warning("上一个采集进程留下 %s 条未结束的 scan_runs，已标为 interrupted", orphans)
            st.mark_cycle_started(db)
        cookie = cookie_health.check_expiry(
            settings.xueqiu_cookie_file,
            warn_days=settings.xueqiu_cookie_warn_days,
            critical_days=settings.xueqiu_cookie_critical_days,
        )
        if cookie["level"] in ("warning", "critical"):
            logger.warning(cookie["message"])

        try:
            cookies = load_collector_cookies()
        except CollectorUnavailable as exc:
            message = f"采集器不可用：{exc}"
            logger.warning(message)
            if not dry_run:
                st.mark_cycle_finished(db, CYCLE_UNAVAILABLE, message)
                _push("down", message)
            return CycleResult(status=CYCLE_UNAVAILABLE, message=message, cookie=cookie)

        authors = list(author_ids) if author_ids else st.pick_authors(db, knobs.max_authors)
        if not authors:
            message = "没有启用的关注作者"
            if not dry_run:
                st.mark_cycle_finished(db, CYCLE_NO_AUTHORS, message)
                _push("up", message)
            return CycleResult(status=CYCLE_NO_AUTHORS, message=message, cookie=cookie)

        on_request = heartbeat.beat if heartbeat is not None else None
        client = (
            client_factory(cookies)
            if client_factory is not None
            else XueqiuWebClient(cookies, on_request=on_request)
        )
        results: List[AuthorResult] = []
        waf_at = None
        interrupted = False
        for index, author_id in enumerate(authors):
            try:
                if index > 0:
                    gap = rng.randint(knobs.author_gap_min, knobs.author_gap_max)
                    logger.info("作者间隔 %ss 后采集下一位", gap)
                    pacer.pause(gap)
            except Interrupted:
                interrupted = True
                break
            logger.info("(%s/%s) 采集雪球作者 %s", index + 1, len(authors), author_id)
            run_id = None if dry_run else st.start_scan_run(db, author_id)
            store = ArchiverStore(db, dry_run=dry_run)
            try:
                result = run_author(client, store, author_id, knobs, pacer)
            except WafChallenge as exc:
                db.rollback()
                result = AuthorResult(
                    author_id=author_id, status=st.RUN_WAF, waf=True, stopped_early=True,
                    error=str(exc),
                )
            except Interrupted:
                db.rollback()
                result = AuthorResult(
                    author_id=author_id, status=st.RUN_INTERRUPTED, stopped_early=True,
                    error="收到停止信号",
                )
            except CollectorFetchError as exc:
                db.rollback()
                logger.warning("作者 %s 时间线首页抓取失败：%s", author_id, exc)
                result = AuthorResult(
                    author_id=author_id, status=st.RUN_ERROR, error=str(exc)[:2000]
                )
            except Exception as exc:  # noqa: BLE001 - 单作者失败不拖垮整轮
                db.rollback()
                logger.exception("作者 %s 采集失败", author_id)
                result = AuthorResult(
                    author_id=author_id, status=st.RUN_FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                )
            results.append(result)
            if not dry_run:
                st.finish_scan_run(db, run_id, result)
                st.mark_author(db, result)
            if result.waf:
                waf_at = st.utcnow()
                logger.warning(
                    "作者 %s 命中 WAF，停止本轮；%ss 冷却期内不开新一轮",
                    author_id, knobs.waf_cooldown_seconds,
                )
                break
            if result.status == st.RUN_INTERRUPTED:
                interrupted = True
                break

        statuses = {item.status for item in results}
        if waf_at is not None:
            status = CYCLE_WAF
        elif interrupted:
            status = CYCLE_INTERRUPTED
        elif statuses == {st.RUN_OK}:
            status = CYCLE_OK
        elif statuses & st.LIVE_RUN_STATUSES:
            status = CYCLE_PARTIAL
        else:
            status = CYCLE_FAILED
        message = _summarize(results)
        if cookie["level"] in ("warning", "critical"):
            message = f"{message}；{cookie['message']}"
        cycle = CycleResult(
            status=status, message=message, authors=results, cookie=cookie,
            request_count=getattr(client, "request_count", 0),
        )
        if not dry_run:
            st.mark_cycle_finished(db, status, message, waf_at=waf_at)
            # 一轮只推一次：采集失败或主凭证 critical 推 down，warning 只写进消息；
            # 停机中断不推（不是采集结果）
            if status != CYCLE_INTERRUPTED:
                # partial（首屏都成功、个别帖子/页失败）数据仍在流动，推 up 并把原因写进
                # 消息；有作者整体 error/failed 才推 down
                healthy = (
                    bool(statuses) and statuses <= st.LIVE_RUN_STATUSES
                    and cookie["level"] != "critical"
                )
                _push("up" if healthy else "down", f"[{status}] {message}")
        logger.info("雪球采集一轮结束：%s %s", status, message)
        return cycle
    finally:
        lock.release()


# --------------------------------------------------------------------------- #
# 常驻循环
# --------------------------------------------------------------------------- #
def run_collector_loop(
    stop_event: threading.Event,
    *,
    session_factory: Optional[Callable[[], Session]] = None,
    poll_seconds: float = 30.0,
) -> None:
    """常驻：每 poll_seconds 写心跳并检查是否该开新一轮（定时 / 立即运行请求 / WAF 冷却）。"""
    from ...database import SessionLocal, engine

    session_factory = session_factory or SessionLocal
    heartbeat = st.Heartbeat(engine)
    if not settings.xueqiu_collector_enabled:
        logger.warning(
            "雪球采集器未启用（XUEQIU_COLLECTOR_ENABLED=false）：进程空转，只写心跳"
        )
        while True:
            heartbeat.beat(force_db=True)
            if stop_event.wait(poll_seconds):
                return

    logger.info(
        "雪球采集器启动：每 %s 分钟一轮，限速 %s-%ss",
        settings.xueqiu_collector_cycle_minutes,
        settings.xueqiu_collector_min_delay_seconds,
        settings.xueqiu_collector_max_delay_seconds,
    )
    while not stop_event.is_set():
        heartbeat.beat(force_db=True)
        db = session_factory()
        try:
            state = st.get_state(db)
            due, reason = st.cycle_due(
                state,
                st.utcnow(),
                interval_minutes=settings.xueqiu_collector_cycle_minutes,
                waf_cooldown_seconds=settings.xueqiu_collector_waf_cooldown_seconds,
            )
            db.rollback()
            if due:
                logger.info("开始一轮雪球作者采集（%s）", reason)
                run_authors_cycle(db, stop_event=stop_event, heartbeat=heartbeat)
            if not stop_event.is_set():
                # 每日按标的采集：到点（业务时区 run_after 之后、当天未跑）或管理员请求才跑；
                # 与作者轮次串行（同一把 advisory lock、同一个限速时钟与 WAF 冷却）
                from .symbols import maybe_run_symbols_cycle

                maybe_run_symbols_cycle(db, stop_event=stop_event, heartbeat=heartbeat)
        except Exception:  # noqa: BLE001 - 循环本身不能死
            logger.exception("雪球采集循环异常")
            db.rollback()
        finally:
            db.close()
        stop_event.wait(poll_seconds)
    logger.info("雪球采集器收到停止信号，退出")
