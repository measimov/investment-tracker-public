"""评论区反扫：在候选帖的评论里找目标作者的回复（`statuses/comments.json`）。

移植自 scan_user_replies：`build_comments_url` / `fetch_comment_page` / `parse_reply` /
`comment_page_limit_for_post` / `scan_post`（去掉 checkpoint 分支，只保留数据库路径）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Protocol
from urllib.parse import urlencode

from ...core.logging import get_app_logger
import requests

from .client import CollectorFetchError, XueqiuWebClient, require_list
from .common import (
    CandidatePost,
    MatchedReply,
    UserUtterance,
    format_timestamp,
    html_to_text,
    reply_dedupe_key,
)
from .timeline import cutoff_timestamp_ms

logger = get_app_logger(__name__)

COMMENTS_API = "https://xueqiu.com/statuses/comments.json"
COMMENTS_PER_PAGE = 20


class ReplySink(Protocol):
    def upsert_reply(self, target_user_id: str, reply: MatchedReply) -> bool: ...

    def upsert_utterance(self, utterance: UserUtterance) -> None: ...

    def update_scan_state(
        self, target_user_id: str, post_id: str, page_count: int, max_page: int
    ) -> None: ...

    def commit(self) -> None: ...


def build_comments_url(post_id: str, page: int, count: int = COMMENTS_PER_PAGE) -> str:
    params = {"id": post_id, "count": count, "page": page, "reply": "true", "split": "true"}
    return f"{COMMENTS_API}?{urlencode(params)}"


def comment_user_id(comment: Dict[str, Any]) -> str:
    user = comment.get("user") or {}
    return str(user.get("id") or comment.get("user_id") or "")


def parse_reply(comment: Dict[str, Any], post: CandidatePost) -> MatchedReply:
    user = comment.get("user") or {}
    reply_comment = comment.get("reply_comment") or {}
    reply_user = reply_comment.get("user") or {}
    reply_to = ""
    if reply_comment:
        reply_to = (
            f"{reply_user.get('screen_name', '未知用户')}: "
            f"{html_to_text(reply_comment.get('text', ''))}"
        )

    return MatchedReply(
        post_id=post.post_id,
        post_url=post.url,
        comment_id=str(comment.get("id") or ""),
        created_at=format_timestamp(comment.get("created_at", 0)),
        author_id=str(user.get("id") or comment.get("user_id") or ""),
        author_name=user.get("screen_name") or "",
        text=html_to_text(comment.get("text", "")),
        like_count=comment.get("like_count", 0),
        created_at_ms=int(comment.get("created_at") or 0),
        reply_to=reply_to,
    )


def utterance_from_reply(
    target_user_id: str, reply: MatchedReply, post: CandidatePost
) -> UserUtterance:
    source_id = reply.comment_id or reply_dedupe_key(reply)
    return UserUtterance(
        utterance_key=f"comment:{source_id}",
        target_user_id=target_user_id,
        source="comment_scan",
        source_id=source_id,
        kind="comment_reply",
        post_id=reply.post_id,
        post_url=reply.post_url,
        created_at=reply.created_at,
        created_at_ms=reply.created_at_ms,
        author_id=reply.author_id,
        author_name=reply.author_name,
        text=reply.text,
        context_post_id=post.post_id,
        context_url=post.url,
        context_author_name=post.author_name,
        context_text=post.text,
    )


def comment_page_limit_for_post(
    post: CandidatePost,
    max_pages: int,
    stale_post_days: int,
    stale_comment_pages: int,
    *,
    now: Optional[float] = None,
) -> int:
    """超过 stale_post_days 的旧帖只扫最新 stale_comment_pages 页评论。"""
    if stale_post_days > 0 and stale_comment_pages > 0 and post.created_at_ms:
        stale_cutoff_ms = cutoff_timestamp_ms(stale_post_days, now=now)
        if post.created_at_ms < stale_cutoff_ms:
            return min(max_pages, stale_comment_pages) if max_pages > 0 else stale_comment_pages
    return max_pages


def matched_replies_on_page(
    comments: List[Dict[str, Any]], post: CandidatePost, target_user_id: str, since_ms: int
) -> List[MatchedReply]:
    """一页评论 → 目标作者在回看窗口内的回复（纯函数）。"""
    matches: List[MatchedReply] = []
    for comment in comments:
        if comment_user_id(comment) != target_user_id:
            continue
        reply = parse_reply(comment, post)
        if since_ms and reply.created_at_ms and reply.created_at_ms < since_ms:
            continue
        matches.append(reply)
    return matches


def fetch_comment_page(
    client: XueqiuWebClient, post_id: str, page: int, count: int = COMMENTS_PER_PAGE
) -> Dict[str, Any]:
    """一页评论。合法空页（`comments: []`）照常返回；WAF 抛 WafChallenge；其他失败
    （网络/HTTP/非 JSON/缺 comments 数组）抛 CollectorFetchError。

    原实现把失败吞成 `{}`——等价于"评论已翻完"，于是该帖被记为扫描完成、进入 12h
    冷却，当轮还以成功收尾（PR #236 评审 P2）。
    """
    context = f"帖子 {post_id} 第 {page} 页评论"
    try:
        payload = client.get_json(build_comments_url(post_id, page, count), context=context)
    except requests.RequestException as exc:
        raise CollectorFetchError(f"{context} 请求失败：{exc}") from exc
    require_list(payload, "comments", context=context)
    return payload


def scan_post(
    client: XueqiuWebClient,
    post: CandidatePost,
    target_user_id: str,
    *,
    sink: ReplySink,
    max_pages: int,
    since_ms: int,
    count: int = COMMENTS_PER_PAGE,
    early_stop_on_old_page: bool = True,
) -> List[MatchedReply]:
    """逐页扫一个帖子的评论，命中即写库；每页结束更新扫描状态并提交。

    停止条件：空页 / 整页早于回看窗口（early stop）/ 达到页数上限（0 = 接口报告的 maxPage）。
    某页拿不到合法响应时抛 CollectorFetchError（之前各页的命中已提交），由调用方把该帖
    记为失败并清掉扫描时间，下一轮重扫。
    """
    matches: List[MatchedReply] = []
    page = 1
    checked_pages = 0
    while True:
        data = fetch_comment_page(client, post.post_id, page, count)
        checked_pages += 1
        comments = data.get("comments") or []
        logger.debug("帖子 %s 第 %s 页评论：%s 条", post.post_id, page, len(comments))

        page_created = [
            int(comment.get("created_at") or 0) for comment in comments if comment.get("created_at")
        ]
        for reply in matched_replies_on_page(comments, post, target_user_id, since_ms):
            inserted = sink.upsert_reply(target_user_id, reply)
            sink.upsert_utterance(utterance_from_reply(target_user_id, reply, post))
            matches.append(reply)
            if logger.isEnabledFor(logging.INFO):
                logger.info(
                    "  命中(%s)：%s %s - %s",
                    "新增" if inserted else "已存在", reply.created_at, reply.author_name,
                    reply.text[:60],
                )

        response_max_page = int(data.get("maxPage") or data.get("max_page") or 0)
        configured_limit = max_pages if max_pages > 0 else response_max_page
        old_page = bool(
            early_stop_on_old_page and since_ms and page_created and max(page_created) < since_ms
        )
        completed = not comments or old_page or bool(configured_limit and page >= configured_limit)
        sink.update_scan_state(target_user_id, post.post_id, checked_pages, response_max_page)
        sink.commit()
        if completed:
            break
        page += 1
    return matches
