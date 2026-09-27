"""采集器共用的数据形状与文本工具（纯函数，移植自 xueqiu-timeline-archiver）。

`utterance_key` / `reply_key` 的构造、`created_at` 文本格式都是**存量数据的身份与形态**，
逐字照搬原实现，改了就会与生产存量行对不上（重复键或同一发言两份）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import unescape
from typing import Any, Dict

from lxml import etree

from ...core.timeutil import business_timezone


@dataclass(frozen=True)
class CandidatePost:
    post_id: str
    url: str
    title: str = ""
    author_id: str = ""
    author_name: str = ""
    text: str = ""
    created_at: str = ""
    created_at_ms: int = 0
    source: str = ""


@dataclass
class MatchedReply:
    post_id: str
    post_url: str
    comment_id: str
    created_at: str
    author_id: str
    author_name: str
    text: str
    like_count: int
    created_at_ms: int = 0
    reply_to: str = ""


@dataclass
class UserUtterance:
    utterance_key: str
    target_user_id: str
    source: str
    source_id: str
    kind: str
    post_id: str
    post_url: str
    created_at: str
    created_at_ms: int
    author_id: str
    author_name: str
    text: str
    context_post_id: str = ""
    context_url: str = ""
    context_author_name: str = ""
    context_text: str = ""


def format_timestamp(timestamp_ms: int | float) -> str:
    """毫秒时间戳 → `YYYY-MM-DD HH:MM:SS`（**业务时区**）。

    原实现用 `time.localtime`，依赖宿主时区（东八区）；容器是 UTC，照搬会让
    created_at 文本整体偏 8 小时、与存量行形态不一致。这里显式按业务时区格式化。
    """
    moment = datetime.fromtimestamp(timestamp_ms / 1000, tz=business_timezone())
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def html_to_text(value: str) -> str:
    if not value:
        return ""

    fragment = etree.HTML(f"<div>{value}</div>")
    if fragment is None:
        return unescape(value).strip()

    text = fragment.xpath("string(.)")
    return unescape(text).strip()


def valid_author_id(value: Any) -> str:
    author_id = str(value or "")
    if not author_id or author_id == "-1":
        return ""
    return author_id


def build_post_url(author_id: str, post_id: str) -> str:
    return (
        f"https://xueqiu.com/{author_id}/{post_id}"
        if author_id
        else f"https://xueqiu.com/statuses/{post_id}"
    )


def build_status_url(status: Dict[str, Any], author_id: str, post_id: str) -> str:
    target = str(status.get("target") or "")
    if f"/{post_id}" in target:
        if target.startswith("https://xueqiu.com/"):
            return target
        if target.startswith("/"):
            return f"https://xueqiu.com{target}"
    return build_post_url(author_id, post_id)


def reply_dedupe_key(reply: MatchedReply) -> str:
    """replies 表主键；原实现逐字照搬（缺 comment_id 时退回组合键）。"""
    if reply.comment_id:
        return reply.comment_id
    return "|".join([reply.post_id, str(reply.created_at_ms), reply.author_id, reply.text[:80]])
