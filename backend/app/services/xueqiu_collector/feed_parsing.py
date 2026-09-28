"""按标的监控的请求构造与响应解析（纯函数，移植自 monitor_symbols.py）。

三个签名域端点（参数逐字照搬原实现）：
- 公告流 `statuses/stock_timeline.json?symbol_id=…&count=…&source=公告`
- 讨论流 `query/v1/symbol/search/status`（stock_timeline 实际只服务公告）
- 组合调仓 `cubes/rebalancing/history.json`

原实现的市场热帖 `statuses/hots.json` 已于 2026-09-28 下线（与持仓无关，少打一类请求）；
旧 Markdown 热帖快照仍可经 `archive_import` 导入 `xueqiu_hot_posts`，但不再采集、不再展示。

**雪球 symbol 不作身份键落库**：请求参数里的雪球 symbol 由调用方经 `to_xueqiu` 在内存里
生成；解析结果里的 url 尽量用 `/{uid}/{id}` 形态（公告的 target 是 `/S/{雪球symbol}/{id}`），
附件链接剔除一切 xueqiu.com 站内链接（cashtag 链接就是 `/S/SH600519`），组合调仓明细里的
`stock_symbol` 换成本仓 (symbol, market)。正文里的 `$名称(SH600519)$` 是帖子原文，照存。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse

from lxml import etree

from .client import CollectorFetchError, require_list
from .common import build_post_url, build_status_url, html_to_text, valid_author_id

STOCK_TIMELINE_API = "https://xueqiu.com/statuses/stock_timeline.json"
SYMBOL_STATUS_API = "https://xueqiu.com/query/v1/symbol/search/status"
CUBE_REBALANCING_API = "https://xueqiu.com/cubes/rebalancing/history.json"

KIND_ANNOUNCEMENT = "announcement"
KIND_DISCUSSION = "discussion"
KINDS = (KIND_ANNOUNCEMENT, KIND_DISCUSSION)
KIND_LABELS = {KIND_ANNOUNCEMENT: "公告", KIND_DISCUSSION: "讨论", "rebalancing": "组合调仓"}

MAX_TEXT_CHARS = 20000
MAX_LINKS = 5
# payload 只留这些互动计数（整数）
COUNT_KEYS = ("reply_count", "retweet_count", "like_count", "fav_count", "view_count")


@dataclass(frozen=True)
class FeedPost:
    post_id: str
    created_at_ms: int
    title: str = ""
    text: str = ""
    author_id: str = ""
    author_name: str = ""
    url: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RebalancingRecord:
    rebalancing_id: str
    created_at_ms: int
    payload: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 请求 URL（参数与原实现一致）
# --------------------------------------------------------------------------- #
def announcement_url(xueqiu_symbol: str, count: int) -> str:
    params = {"symbol_id": xueqiu_symbol, "count": count, "source": "公告"}
    return f"{STOCK_TIMELINE_API}?{urlencode(params)}"


def discussion_url(xueqiu_symbol: str, count: int) -> str:
    params = {
        "count": count, "comment": 0, "symbol": xueqiu_symbol, "hl": 0,
        "source": "all", "sort": "", "page": 1,
    }
    return f"{SYMBOL_STATUS_API}?{urlencode(params)}"


def feed_url(kind: str, xueqiu_symbol: str, count: int) -> str:
    if kind == KIND_ANNOUNCEMENT:
        return announcement_url(xueqiu_symbol, count)
    if kind == KIND_DISCUSSION:
        return discussion_url(xueqiu_symbol, count)
    raise ValueError(f"未知的标的帖子类型: {kind}")


def rebalancing_url(cube_id: str, count: int) -> str:
    params = {"cube_symbol": cube_id, "count": count, "page": 1}
    return f"{CUBE_REBALANCING_API}?{urlencode(params)}"


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
ENDPOINT_REBALANCING = "rebalancing"
# 每个端点认可的列表字段（与原实现 `_extract_list` 的 list 口径一致；帖子流另认雪球
# status 列表通用的 statuses）。只有这些已知结构才算「合法的空结果」
FEED_LIST_KEYS = {
    KIND_ANNOUNCEMENT: ("list", "statuses"),
    KIND_DISCUSSION: ("list", "statuses"),
    ENDPOINT_REBALANCING: ("list",),
}


def response_error_reason(payload: Any) -> Optional[str]:
    """雪球常见错误形态：error_code / error_description / success=false。"""
    if not isinstance(payload, dict):
        return None
    code = payload.get("error_code")
    description = payload.get("error_description") or payload.get("error_message")
    if code not in (None, "", 0, "0") or description:
        return f"雪球返回错误 error_code={code!r}：{str(description or '')[:120]}"
    if payload.get("success") is False:
        detail = payload.get("message") or payload.get("msg") or payload.get("code") or ""
        return f"雪球返回 success=false：{str(detail)[:120]}"
    return None


def validate_feed_payload(payload: Any, endpoint: str) -> List[Dict[str, Any]]:
    """网络响应入口的校验：错误对象、未知结构、None 一律抛 `CollectorFetchError`
    （与作者时间线/评论同一个错误类型，PR #236）；只有该端点认可的对象结构
    （{list: [...]} 等）才返回条目（可为空）。顶层数组一律拒绝——唯一返回顶层数组的
    市场热帖端点已下线。"""
    if endpoint not in FEED_LIST_KEYS:
        raise ValueError(f"未知的端点类型: {endpoint}")
    context = f"{KIND_LABELS.get(endpoint, endpoint)}接口"
    if payload is None:
        raise CollectorFetchError(f"{context} 响应为空（JSON null）")
    reason = response_error_reason(payload)
    if reason:
        raise CollectorFetchError(f"{context} {reason}")
    if isinstance(payload, list):
        raise CollectorFetchError(f"{context} 响应顶层是数组，该端点预期对象")
    if isinstance(payload, dict):
        keys = FEED_LIST_KEYS[endpoint]
        key = next((name for name in keys if name in payload), None)
        if key is None:
            seen = ",".join(sorted(str(name) for name in payload)[:8]) or "（空对象）"
            raise CollectorFetchError(
                f"{context} 响应缺少列表字段（预期 {'/'.join(keys)}，实得键 {seen}）"
            )
        items = require_list(payload, key, context=context)
    else:
        raise CollectorFetchError(
            f"{context} 响应不是 JSON 对象（{type(payload).__name__}）"
        )
    if any(not isinstance(item, dict) for item in items):
        raise CollectorFetchError(f"{context} 列表里有非对象元素")
    return list(items)


def _as_items(payload: Any, endpoint: str) -> List[Dict[str, Any]]:
    """解析函数的输入：已校验的条目列表，或原始响应（走同一套严格校验）。"""
    if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        return payload
    return validate_feed_payload(payload, endpoint)


def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _is_xueqiu_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "xueqiu.com" or host.endswith(".xueqiu.com")


def extract_links(html: str) -> List[str]:
    """正文 HTML 里的站外链接（公告原文 PDF 等），最多 MAX_LINKS 条。

    站内链接一律剔除：cashtag 是 `xueqiu.com/S/{雪球symbol}`，@ 提及是 `/n/昵称`，
    图片走 imedao 图床——都不是附件，且前者会把雪球 symbol 带进库。
    """
    if not html or "<a" not in html:
        return []
    tree = etree.HTML(f"<div>{html}</div>")
    if tree is None:
        return []
    links: List[str] = []
    for anchor in tree.xpath("//a[@href]"):
        href = str(anchor.get("href") or "").strip()
        if href.startswith("//"):
            href = f"https:{href}"
        if not href.startswith(("http://", "https://")):
            continue
        if _is_xueqiu_host(href) or "imedao.com" in href or "co-img-link" in (
            anchor.get("class") or ""
        ):
            continue
        if href not in links:
            links.append(href)
        if len(links) >= MAX_LINKS:
            break
    return links


def status_url(item: Dict[str, Any], author_id: str, post_id: str) -> str:
    """原帖链接。`/S/{雪球symbol}/{id}` 形态的 target（公告常见）在知道作者时改成
    `/{uid}/{id}`；不知道作者时保留原 target——链接必须能打开，它只是跳转地址，
    不是身份键（身份键 symbol/market 列永远是本仓代码）。"""
    target = str(item.get("target") or "")
    if "/S/" in target and author_id:
        return build_post_url(author_id, post_id)
    return build_status_url(item, author_id, post_id)


def parse_status(item: Dict[str, Any]) -> Optional[FeedPost]:
    """一条帖子（公告/讨论同构）→ FeedPost；没有 id 的条目丢弃。"""
    post_id = str(item.get("id") or "").strip()
    if not post_id:
        return None
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    author_id = valid_author_id(item.get("user_id") or user.get("id"))
    raw_text = str(item.get("text") or "")
    raw_description = str(item.get("description") or "")
    text = html_to_text(raw_text) or html_to_text(raw_description)
    payload: Dict[str, Any] = {}
    for key in COUNT_KEYS:
        value = item.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            payload[key] = int(value)
    source = item.get("source")
    if isinstance(source, str) and source:
        payload["source"] = source[:50]
    hot = item.get("hot")
    if isinstance(hot, (bool, int, float)):
        payload["hot"] = hot
    links = extract_links(raw_text or raw_description)
    if links:
        payload["links"] = links
    retweeted = item.get("retweeted_status")
    if isinstance(retweeted, dict) and retweeted.get("id"):
        payload["retweeted_post_id"] = str(retweeted["id"])
    return FeedPost(
        post_id=post_id,
        created_at_ms=_to_int(item.get("created_at")),
        title=html_to_text(str(item.get("title") or ""))[:500],
        text=text[:MAX_TEXT_CHARS],
        author_id=author_id,
        author_name=str(user.get("screen_name") or "")[:100],
        url=status_url(item, author_id, post_id),
        payload=payload,
    )


def parse_statuses(payload: Any, endpoint: str = KIND_DISCUSSION) -> List[FeedPost]:
    """条目列表（或原始响应，按 endpoint 严格校验）→ FeedPost；没有 id 的条目丢弃。"""
    posts = []
    for item in _as_items(payload, endpoint):
        post = parse_status(item)
        if post is not None:
            posts.append(post)
    return posts


_A_SHARE_RE = re.compile(r"^(SH|SZ|BJ)(\d{6})$")
_HK_RE = re.compile(r"^\d{5}$")
_US_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def from_xueqiu(xueqiu_symbol: str) -> Optional[Tuple[str, str]]:
    """雪球 symbol → 本仓 (symbol, market)；认不出返回 None（只在内存里用）。

    `to_xueqiu` 的逆：A/B 股去交易所前缀（沪 900 / 深 200 开头为 B股），港股 5 位码，
    美股 ticker。只用于把组合调仓明细换成本仓身份键。
    """
    text = str(xueqiu_symbol or "").strip().upper()
    matched = _A_SHARE_RE.match(text)
    if matched:
        code = matched.group(2)
        is_b = (matched.group(1) == "SH" and code.startswith("900")) or (
            matched.group(1) == "SZ" and code.startswith("200")
        )
        return code, ("B股" if is_b else "A股")
    if _HK_RE.match(text):
        return text, "港股"
    if _US_RE.match(text):
        return text, "美股"
    return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


HISTORY_NUMBER_KEYS = (
    "prev_weight", "target_weight", "prev_weight_adjusted", "weight",
    "price", "prev_price", "volume", "prev_volume",
)


def parse_rebalancing(item: Dict[str, Any]) -> Optional[RebalancingRecord]:
    rebalancing_id = str(item.get("id") or "").strip()
    if not rebalancing_id:
        return None
    histories = []
    for history in item.get("rebalancing_histories") or []:
        if not isinstance(history, dict):
            continue
        pair = from_xueqiu(str(history.get("stock_symbol") or ""))
        entry: Dict[str, Any] = {
            "stock_name": str(history.get("stock_name") or ""),
            "symbol": pair[0] if pair else None,
            "market": pair[1] if pair else None,
        }
        for key in HISTORY_NUMBER_KEYS:
            number = _number(history.get(key))
            if number is not None:
                entry[key] = number
        histories.append(entry)
    payload: Dict[str, Any] = {
        "status": str(item.get("status") or ""),
        "category": str(item.get("category") or ""),
        "exe_strategy": html_to_text(str(item.get("exe_strategy") or "")),
        "comment": html_to_text(str(item.get("comment") or ""))[:2000],
        "updated_at_ms": _to_int(item.get("updated_at")),
        "histories": histories,
    }
    for key in ("cash", "cash_value"):
        number = _number(item.get(key))
        if number is not None:
            payload[key] = number
    return RebalancingRecord(
        rebalancing_id=rebalancing_id,
        created_at_ms=_to_int(item.get("created_at") or item.get("updated_at")),
        payload=payload,
    )


def parse_rebalancings(payload: Any) -> List[RebalancingRecord]:
    records = []
    for item in _as_items(payload, ENDPOINT_REBALANCING):
        record = parse_rebalancing(item)
        if record is not None:
            records.append(record)
    return records
