"""雪球观点数据源：读取本仓采集器（`services/xueqiu_collector/`）写入的关注作者发言。

`xueqiu_archiver_utterances` 原由独立仓库 xueqiu-timeline-archiver 的 cron 写入，
2026-09 收纳进本仓（迁移 20260927_0024 纳入 Alembic，表**总是存在**），读取走 ORM
（`models/xueqiu_collector.py`），不再探测表是否存在（#280）。

「数据源未接入」的含义随之改变：不再是"表不存在"，而是**采集器从未成功运行且库里
没有任何发言**（未启用 / 刚部署 / Cookie 一直无效）。入口显式抛
`OpinionSourceUnavailable`（API 层映射 409"数据源未接入"），绝不静默返回空冒充"无观点"。

实测过的表事实（2026-08-31，appdb）：
- `created_at` 列是 **TEXT** 不是 timestamptz——时间一律用 `created_at_ms`
  （bigint，全表零空值），别碰 created_at，更别 COALESCE 两列（类型不匹配）。
- 采集活性 = 最近一次 status 为 ok/partial 的 `xueqiu_archiver_scan_runs.finished_at`
  （采集器每位作者每轮写一行；partial = 时间线首页成功、个别页/帖失败，同样证明采集
  在流动；error/failed/waf 一律不算——把失败响应记成成功会掩盖停摆，PR #236 评审 P2）。
  尚无成功记录时（刚从 archiver cron 切换过来）退回 `max(last_seen_at)`——每轮扫描
  对已见行也会刷新它，作者沉默不影响。
  `max(created_at_ms)` 是"最新发言"展示值，不是活性判据。

标的匹配（v1 精确口径，不做名称模糊匹配）：正文/上下文里的
`$名称(SH600519)$` cashtag + 帖子链接里的 `/S/{code}/{post_id}`。括号内实测
形态：A股 `SH600519`、港股五位 `00700`（与持仓存储一致）、美股 ticker `PDD`；
指数（`HKHSI`）靠 wanted 集合自然过滤。匹配键 = `xueqiu_source.to_xueqiu`
的雪球码，只存在于内存，绝不落库。
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..config import settings
from ..core.logging import get_app_logger
from ..models.xueqiu_collector import XueqiuArchiverScanRun as ScanRun
from ..models.xueqiu_collector import XueqiuArchiverUtterance as Utterance
from .xueqiu_collector.state import LIVE_RUN_STATUSES

logger = get_app_logger(__name__)

# cashtag：$名称(SH600519)$ / $腾讯控股(00700)$ / $苹果(AAPL)$。
# 内码宽容 [A-Za-z0-9.\-]（覆盖 BRK.A 类 ticker）；精确性由 wanted 集合过滤。
CASHTAG_RE = re.compile(r"\$[^$()]{1,60}\(([A-Za-z0-9.\-]{1,12})\)\$")
# 帖子永久链接：/S/SH600519/312345678、/S/06666/407442726
PERMALINK_RE = re.compile(r"/S/([A-Za-z0-9.\-]{1,12})/\d+")

# kind 的中文语义（LLM 输入 meta 与前端展示共用）
KIND_LABELS = {
    "homepage_post": "主帖",
    "homepage_reply": "回复",
    "homepage_repost": "转发",
    "comment_reply": "评论回复",
}


class OpinionSourceUnavailable(Exception):
    """雪球观点数据源未接入（采集器从未成功运行且无发言数据）。API 层映射 409。"""


UNAVAILABLE_MESSAGE = (
    "雪球观点数据源未接入（采集器未启用或尚未成功运行，库中暂无关注作者发言；"
    "见观点页「采集器」卡片）"
)


def _from_epoch_ms(value: Optional[int]) -> Optional[datetime]:
    """created_at_ms → aware UTC datetime（整数运算，不经浮点除法丢毫秒）。"""
    if value is None:
        return None
    seconds, millis = divmod(int(value), 1000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(milliseconds=millis)


def is_opinion_source_available(db: Session) -> bool:
    """有发言数据，或采集器成功跑过一轮（哪怕关注作者近期都没发言）。

    每次现查不缓存：采集器可能在本进程启动后才第一次跑成功，缓存"不可用"会把
    已接入的数据源继续报 409；两条 EXISTS 走主键/小表，<1ms。
    """
    with db.begin_nested():
        if db.query(db.query(Utterance.utterance_key).exists()).scalar():
            return True
        return bool(
            db.query(
                db.query(ScanRun.run_id).filter(ScanRun.status.in_(LIVE_RUN_STATUSES)).exists()
            ).scalar()
        )


def ensure_opinion_source(db: Session) -> None:
    if not is_opinion_source_available(db):
        raise OpinionSourceUnavailable(UNAVAILABLE_MESSAGE)


def latest_successful_scan_at(db: Session) -> Optional[datetime]:
    """最近一次成功采集的结束时间；无记录返回 None（调用方退回 last_seen_at）。"""
    with db.begin_nested():
        return (
            db.query(func.max(ScanRun.finished_at))
            .filter(ScanRun.status.in_(LIVE_RUN_STATUSES))
            .scalar()
        )


def source_freshness(db: Session) -> Dict[str, Any]:
    """数据源新鲜度：{available, latest_scan_at, latest_utterance_at, stale}。

    latest_scan_at = 最近一次成功采集（scan_runs），无记录退回 max(last_seen_at)；
    stale = 它距今超过 xueqiu_opinion_stale_hours——"采集停摆"的预警信号。任何异常
    按 unavailable 处理不抛：本函数被 Dashboard snapshot 顺带调用，观点数据源的
    故障不配拖垮整个看板。
    """
    unavailable = {
        "available": False,
        "latest_scan_at": None,
        "latest_utterance_at": None,
        "stale": False,
    }
    try:
        if not is_opinion_source_available(db):
            return unavailable
        # SAVEPOINT：外部表列漂移/权限错误等真实 DBAPI 异常会把当前事务标成
        # aborted——只 catch 不回滚的话，同一 Session 里随后的持仓/自选查询全部
        # InFailedSqlTransaction，"降级"承诺变成 500（评审 P2）。嵌套事务失败
        # 只回滚到 SAVEPOINT，外层事务照常可用。
        with db.begin_nested():
            last_seen_at, latest_ms = db.query(
                func.max(Utterance.last_seen_at), func.max(Utterance.created_at_ms)
            ).one()
        successful_scan_at = latest_successful_scan_at(db)
    except Exception as exc:
        logger.warning("雪球观点数据源新鲜度探测失败: %s", str(exc)[:150])
        return unavailable
    latest_scan_at = successful_scan_at or last_seen_at
    latest_utterance_at = _from_epoch_ms(latest_ms)
    stale = False
    if latest_scan_at is not None:
        age = datetime.now(timezone.utc) - latest_scan_at
        stale = age > timedelta(hours=settings.xueqiu_opinion_stale_hours)
    return {
        "available": True,
        "latest_scan_at": latest_scan_at.isoformat() if latest_scan_at else None,
        "latest_utterance_at": (latest_utterance_at.isoformat() if latest_utterance_at else None),
        "stale": stale,
    }


def extract_symbol_refs(*texts: Optional[str]) -> Set[str]:
    """从若干文本/URL 中提取全部标的引用（雪球码形态，统一大写）。"""
    refs: Set[str] = set()
    for value in texts:
        if not value:
            continue
        refs.update(match.upper() for match in CASHTAG_RE.findall(value))
        refs.update(match.upper() for match in PERMALINK_RE.findall(value))
    return refs


def build_wanted_map(targets: List[Tuple[str, str]]) -> Dict[str, Tuple[str, str]]:
    """(symbol, market) 列表 -> {雪球码: (symbol, market)}。

    经 `to_xueqiu` 归一（A股 SH/SZ/BJ 前缀含 920xxx 纠正、港股 zfill(5) 解决
    持仓"700" vs cashtag"00700"、美股 ticker 原样大写）；转换失败的标的静默
    跳过——匹配不到只是"无观点"，不该让一个异常代码拖垮全部目标。
    """
    from .xueqiu_source import to_xueqiu

    wanted: Dict[str, Tuple[str, str]] = {}
    for symbol, market in targets:
        try:
            wanted[to_xueqiu(symbol, market)] = (symbol, market)
        except Exception as exc:
            logger.warning("标的 %s/%s 无法转换雪球码，跳过观点匹配: %s", symbol, market, exc)
    return wanted


def scan_matched_utterances(
    db: Session, wanted: Set[str], *, since: datetime
) -> Dict[str, List[Dict[str, Any]]]:
    """一次全扫：把 since 之后含标的引用的发言分配到 wanted 中的雪球码。

    单标的 job（wanted 单元素）、角标端点（wanted=全部目标）、观点页 feed
    共用本函数——一条匹配代码路径，只测一次。一行可引用多只标的，会出现在
    多个键下。返回值内每个列表按时间升序；数据源未接入时抛
    OpinionSourceUnavailable。

    量级：180 天窗口内几千行、均长 80 字符，LIKE 预筛后单次全扫 <100ms，
    不值得为它建预计算索引（两条路径必然漂移）。
    """
    ensure_opinion_source(db)
    since_ms = int(since.timestamp() * 1000)
    with db.begin_nested():
        # 只取列元组：批量 job 的长会话里几千个 ORM 实体会一直挂在 identity map 上
        rows = (
            db.query(
                Utterance.utterance_key,
                Utterance.kind,
                Utterance.author_name,
                Utterance.text,
                Utterance.context_text,
                Utterance.context_author_name,
                Utterance.post_url,
                Utterance.context_url,
                Utterance.created_at_ms,
            )
            .filter(
                Utterance.created_at_ms >= since_ms,
                or_(
                    Utterance.text.like("%$%"),
                    Utterance.context_text.like("%$%"),
                    Utterance.post_url.like("%/S/%"),
                    Utterance.context_url.like("%/S/%"),
                ),
            )
            .order_by(Utterance.created_at_ms.asc())
            .all()
        )

    matched: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        refs = extract_symbol_refs(row.text, row.context_text, row.post_url, row.context_url)
        hits = refs & wanted
        if not hits:
            continue
        item = {
            "utterance_key": row.utterance_key,
            "kind": row.kind,
            "author_name": row.author_name,
            "text": row.text,
            "context_text": row.context_text,
            "context_author_name": row.context_author_name,
            "post_url": row.post_url,
            "created_at": _from_epoch_ms(row.created_at_ms),
        }
        for key in hits:
            matched.setdefault(key, []).append(item)
    return matched
