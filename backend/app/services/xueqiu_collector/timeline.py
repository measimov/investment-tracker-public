"""作者主页时间线（`v4/statuses/user_timeline.json`，type=3 = 全部/含回复）。

移植自 scan_user_replies 的监控模式：`timeline_statuses_until` +
`collect_profile_candidates_and_utterances` + `profile_utterance_from_status` +
`post_from_status`。纯函数负责解析，`collect_profile` 负责翻页 IO。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

from .client import CollectorFetchError, XueqiuWebClient, require_list
from .common import (
    CandidatePost,
    UserUtterance,
    build_status_url,
    format_timestamp,
    html_to_text,
    valid_author_id,
)

TIMELINE_API = "https://xueqiu.com/v4/statuses/user_timeline.json"
# 实测 type=3 返回"全部（含回复）"混合流
PROFILE_TIMELINE_TYPE = 3


def build_user_timeline_url(user_id: str, page: int, timeline_type: Optional[int] = None) -> str:
    params: Dict[str, Any] = {"page": page, "user_id": user_id}
    if timeline_type is not None:
        params["type"] = timeline_type
    return f"{TIMELINE_API}?{urlencode(params)}"


def cutoff_timestamp_ms(since_days: int, *, now: Optional[float] = None) -> int:
    if since_days <= 0:
        return 0
    return int(((time.time() if now is None else now) - since_days * 24 * 60 * 60) * 1000)


def post_from_status(status: Dict[str, Any], source: str) -> Optional[CandidatePost]:
    post_id = status.get("id")
    if not post_id:
        return None

    user = status.get("user") or {}
    author_id = valid_author_id(user.get("id") or status.get("user_id"))
    url = build_status_url(status, author_id, str(post_id))
    text = html_to_text(status.get("text") or status.get("description") or "")
    title = html_to_text(status.get("title") or "")
    created_at_ms = int(status.get("created_at") or 0)
    created_at = format_timestamp(created_at_ms) if created_at_ms else ""

    return CandidatePost(
        post_id=str(post_id),
        url=url,
        title=title,
        author_id=author_id,
        author_name=user.get("screen_name") or "",
        text=text,
        created_at=created_at,
        created_at_ms=created_at_ms,
        source=source,
    )


def profile_utterance_from_status(
    status: Dict[str, Any], target_user_id: str
) -> Optional[UserUtterance]:
    post = post_from_status(status, source=f"profile:{target_user_id}")
    if not post:
        return None

    retweeted_status = status.get("retweeted_status") or {}
    context_post = (
        post_from_status(retweeted_status, source=f"profile-context:{target_user_id}")
        if retweeted_status
        else None
    )
    text = html_to_text(status.get("text") or status.get("description") or "")
    kind = "homepage_post"
    if retweeted_status and text.startswith("回复"):
        kind = "homepage_reply"
    elif retweeted_status:
        kind = "homepage_repost"

    return UserUtterance(
        utterance_key=f"profile:{target_user_id}:{post.post_id}",
        target_user_id=target_user_id,
        source="profile_timeline",
        source_id=post.post_id,
        kind=kind,
        post_id=post.post_id,
        post_url=post.url,
        created_at=post.created_at,
        created_at_ms=post.created_at_ms,
        author_id=post.author_id,
        author_name=post.author_name,
        text=text,
        context_post_id=context_post.post_id if context_post else "",
        context_url=context_post.url if context_post else "",
        context_author_name=context_post.author_name if context_post else "",
        context_text=context_post.text if context_post else "",
    )


def filter_page_statuses(
    page_statuses: List[Dict[str, Any]], since_ms: int
) -> Tuple[List[Dict[str, Any]], bool]:
    """一页时间线 → (窗口内的 status, 本页是否见到窗口内的条目)。"""
    kept: List[Dict[str, Any]] = []
    saw_recent = False
    for status in page_statuses:
        created_at_ms = int(status.get("created_at") or 0)
        if since_ms and created_at_ms and created_at_ms < since_ms:
            continue
        if since_ms and created_at_ms:
            saw_recent = True
        kept.append(status)
    return kept, saw_recent


def candidates_and_utterances(
    statuses: List[Dict[str, Any]], user_id: str
) -> Tuple[List[CandidatePost], List[UserUtterance]]:
    """时间线 status → (候选帖, 主页发言)。候选帖取被转发的原帖（有则），否则本帖。"""
    candidates: Dict[str, CandidatePost] = {}
    utterances: Dict[str, UserUtterance] = {}
    for status in statuses:
        utterance = profile_utterance_from_status(status, user_id)
        if utterance:
            utterances[utterance.utterance_key] = utterance

        candidate_status = status.get("retweeted_status") or status
        source = f"monitor:{user_id}"
        if status.get("retweeted_status"):
            source = f"monitor-retweeted:{user_id}"
        post = post_from_status(candidate_status, source=source)
        if post:
            candidates[post.post_id] = post
    return list(candidates.values()), list(utterances.values())


def fetch_timeline_page(
    client: XueqiuWebClient, user_id: str, page: int, timeline_type: Optional[int]
) -> List[Dict[str, Any]]:
    """一页时间线的 statuses。合法空页返回 []；拿不到合法响应抛 CollectorFetchError
    （WAF 仍抛 WafChallenge）——两者绝不能混为一谈（PR #236 评审 P2）。"""
    context = f"用户 {user_id} 主页发言 第 {page} 页"
    try:
        payload = client.get_json(
            build_user_timeline_url(user_id, page, timeline_type), context=context
        )
    except requests.RequestException as exc:
        raise CollectorFetchError(f"{context} 请求失败：{exc}") from exc
    return require_list(payload, "statuses", context=context)


def fetch_timeline_statuses(
    client: XueqiuWebClient,
    user_id: str,
    *,
    pages: int,
    since_ms: int,
    timeline_type: Optional[int] = PROFILE_TIMELINE_TYPE,
) -> Tuple[List[Dict[str, Any]], str]:
    """翻页直到：合法空页 / 达到页数上限 / 整页都早于回看窗口。

    返回 (statuses, 中途失败说明)。**首页**拿不到合法响应直接抛 CollectorFetchError
    （本轮对该作者一无所知，不能记成功）；后续页失败则保留已取到的部分、停止翻页，
    并把失败说明交给调用方记 partial。
    """
    statuses: List[Dict[str, Any]] = []
    page = 1
    while True:
        try:
            page_statuses = fetch_timeline_page(client, user_id, page, timeline_type)
        except CollectorFetchError as exc:
            if page == 1:
                raise
            return statuses, str(exc)
        if not page_statuses:
            break
        kept, saw_recent = filter_page_statuses(page_statuses, since_ms)
        statuses.extend(kept)
        if pages > 0 and page >= pages:
            break
        if since_ms and not saw_recent:
            break
        page += 1
    return statuses, ""


def collect_profile(
    client: XueqiuWebClient, user_id: str, *, pages: int, since_ms: int
) -> Tuple[List[CandidatePost], List[UserUtterance], str]:
    """→ (候选帖, 主页发言, 中途分页失败说明——空串表示完整)。"""
    statuses, partial_error = fetch_timeline_statuses(
        client, user_id, pages=pages, since_ms=since_ms
    )
    candidates, utterances = candidates_and_utterances(statuses, user_id)
    return candidates, utterances, partial_error
