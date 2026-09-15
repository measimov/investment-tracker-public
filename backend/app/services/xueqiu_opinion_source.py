"""雪球观点数据源：读取 xueqiu-timeline-archiver 写入同库的关注用户发言。

**红线**：`xueqiu_archiver_utterances` 归 xueqiu-timeline-archiver 项目所有，
本应用对它**只读**——不建 ORM 模型、不加 FK、Alembic 永不触碰；一律 raw SQL
（`sqlalchemy.text()`），只依赖用到的列名，表结构由对方演进。表不存在
（测试库 / 未部署 archiver）是合法形态：入口显式抛 `OpinionSourceUnavailable`
（API 层映射 409"数据源未接入"），绝不静默返回空冒充"无观点"。

实测过的表事实（2026-08-31，appdb）：
- `created_at` 列是 **TEXT** 不是 timestamptz——时间一律用 `created_at_ms`
  （bigint，全表零空值），别碰 created_at，更别 COALESCE 两列（类型不匹配）。
- cron 活性信号 = `max(last_seen_at)`（timestamptz）：archiver 每轮扫描对已见
  行也会刷新它，作者沉默不影响。`xueqiu_archiver_scan_runs` 是空表，不能用；
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

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import settings
from ..core.logging import get_app_logger

logger = get_app_logger(__name__)

UTTERANCE_TABLE = "xueqiu_archiver_utterances"

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
    """雪球观点数据源未接入（archiver 表不存在）。API 层映射 409。"""


def is_opinion_source_available(db: Session) -> bool:
    """探测外部表是否存在。每次现查不缓存：archiver 可能在本进程启动后才部署，
    缓存"不存在"会把已接入的数据源继续报 409；单条 catalog 查询 <1ms。"""
    with db.begin_nested():
        row = db.execute(
            text("SELECT to_regclass(:name) IS NOT NULL"),
            {"name": f"public.{UTTERANCE_TABLE}"},
        ).scalar()
    return bool(row)


def ensure_opinion_source(db: Session) -> None:
    if not is_opinion_source_available(db):
        raise OpinionSourceUnavailable(
            f"雪球观点数据源未接入（未找到 {UTTERANCE_TABLE} 表，"
            "该表由 xueqiu-timeline-archiver 项目写入）"
        )


def source_freshness(db: Session) -> Dict[str, Any]:
    """数据源新鲜度：{available, latest_scan_at, latest_utterance_at, stale}。

    stale = max(last_seen_at) 距今超过 xueqiu_opinion_stale_hours——这是
    "archiver cron 停摆"的预警信号（见模块 docstring：last_seen_at 每轮扫描
    都刷新）。任何异常按 unavailable 处理不抛：本函数被 Dashboard snapshot
    顺带调用，观点数据源的故障不配拖垮整个看板。
    """
    unavailable = {
        "available": False, "latest_scan_at": None,
        "latest_utterance_at": None, "stale": False,
    }
    try:
        if not is_opinion_source_available(db):
            return unavailable
        # SAVEPOINT：外部表列漂移/权限错误等真实 DBAPI 异常会把当前事务标成
        # aborted——只 catch 不回滚的话，同一 Session 里随后的持仓/自选查询全部
        # InFailedSqlTransaction，"降级"承诺变成 500（评审 P2）。嵌套事务失败
        # 只回滚到 SAVEPOINT，外层事务照常可用。
        with db.begin_nested():
            row = db.execute(
                text(
                    f"SELECT max(last_seen_at) AS latest_scan_at, "
                    f"       to_timestamp(max(created_at_ms) / 1000.0) AS latest_utterance_at "
                    f"FROM {UTTERANCE_TABLE}"
                )
            ).one()
    except Exception as exc:
        logger.warning("雪球观点数据源新鲜度探测失败: %s", str(exc)[:150])
        return unavailable
    latest_scan_at = row.latest_scan_at
    stale = False
    if latest_scan_at is not None:
        age = datetime.now(timezone.utc) - latest_scan_at
        stale = age > timedelta(hours=settings.xueqiu_opinion_stale_hours)
    return {
        "available": True,
        "latest_scan_at": latest_scan_at.isoformat() if latest_scan_at else None,
        "latest_utterance_at": (
            row.latest_utterance_at.isoformat() if row.latest_utterance_at else None
        ),
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
    多个键下。返回值内每个列表按时间升序；不存在的表在此处抛
    OpinionSourceUnavailable。

    量级：180 天窗口内几千行、均长 80 字符，LIKE 预筛后单次全扫 <100ms，
    不值得为它建预计算索引（两条路径必然漂移）。
    """
    ensure_opinion_source(db)
    since_ms = int(since.timestamp() * 1000)
    with db.begin_nested():
        rows = db.execute(
            text(
            f"SELECT utterance_key, kind, author_name, text AS body, context_text, "
            f"       context_author_name, post_url, context_url, "
            f"       to_timestamp(created_at_ms / 1000.0) AS created_at "
            f"FROM {UTTERANCE_TABLE} "
            f"WHERE created_at_ms >= :since_ms "
            f"  AND (text LIKE '%$%' OR context_text LIKE '%$%' "
            f"       OR post_url LIKE '%/S/%' OR context_url LIKE '%/S/%') "
            f"ORDER BY created_at_ms ASC"
            ),
            {"since_ms": since_ms},
        ).mappings().all()

    matched: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        refs = extract_symbol_refs(
            row["body"], row["context_text"], row["post_url"], row["context_url"]
        )
        hits = refs & wanted
        if not hits:
            continue
        item = {
            "utterance_key": row["utterance_key"],
            "kind": row["kind"],
            "author_name": row["author_name"],
            "text": row["body"],
            "context_text": row["context_text"],
            "context_author_name": row["context_author_name"],
            "post_url": row["post_url"],
            "created_at": row["created_at"],
        }
        for key in hits:
            matched.setdefault(key, []).append(item)
    return matched
