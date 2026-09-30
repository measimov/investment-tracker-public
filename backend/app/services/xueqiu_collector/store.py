"""归档表的 upsert（posts / replies / utterances / post_scan_state）。

**SQL 与冲突键逐字照搬 xueqiu-timeline-archiver**（只把 psycopg 的 `%s` 换成
SQLAlchemy 的命名参数）：哪些列冲突时覆盖、哪些保留（posts.text 只在补全文或旧行未补全
时覆盖；replies 只刷新 post_url/like_count/reply_to；scan_state 的 last_waf_at 只进不退），
都是与生产存量行无缝衔接的前提。`tests/test_xueqiu_collector_store.py` 用原仓库格式的
存量行钉住这些语义。

`dry_run=True`：读照常走库（冷却期、已补全判断与真实运行一致），写一律不落库，
只记进 `captured` 供 `manage.py xueqiu-collector --dry-run` 汇报。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from .common import CandidatePost, MatchedReply, UserUtterance, reply_dedupe_key

UPSERT_POST_SQL = text(
    """
    insert into xueqiu_archiver_posts (
        post_id, url, title, author_id, author_name, text, created_at, created_at_ms, source,
        detail_enriched, last_detail_fetch_at
    )
    values (:post_id, :url, :title, :author_id, :author_name, :text, :created_at, :created_at_ms,
            :source, :detail_enriched, case when :detail_enriched then now() else null end)
    on conflict (post_id) do update set
        url = excluded.url,
        title = excluded.title,
        author_id = excluded.author_id,
        author_name = excluded.author_name,
        text = case
            when excluded.detail_enriched or not xueqiu_archiver_posts.detail_enriched
            then excluded.text
            else xueqiu_archiver_posts.text
        end,
        created_at = excluded.created_at,
        created_at_ms = excluded.created_at_ms,
        source = excluded.source,
        detail_enriched = xueqiu_archiver_posts.detail_enriched or excluded.detail_enriched,
        last_seen_at = now(),
        last_detail_fetch_at = case
            when excluded.detail_enriched then now()
            else xueqiu_archiver_posts.last_detail_fetch_at
        end
    """
)

UPSERT_REPLY_SQL = text(
    """
    insert into xueqiu_archiver_replies (
        reply_key, target_user_id, post_id, post_url, comment_id, created_at, author_id, author_name,
        text, like_count, created_at_ms, reply_to
    )
    values (:reply_key, :target_user_id, :post_id, :post_url, :comment_id, :created_at, :author_id,
            :author_name, :text, :like_count, :created_at_ms, :reply_to)
    on conflict (reply_key) do update set
        post_url = excluded.post_url,
        like_count = excluded.like_count,
        reply_to = excluded.reply_to,
        last_seen_at = now()
    returning (xmax = 0) as inserted
    """
)

UPSERT_UTTERANCE_SQL = text(
    """
    insert into xueqiu_archiver_utterances (
        utterance_key, target_user_id, source, source_id, kind, post_id, post_url, created_at,
        created_at_ms, author_id, author_name, text, context_post_id, context_url,
        context_author_name, context_text
    )
    values (:utterance_key, :target_user_id, :source, :source_id, :kind, :post_id, :post_url,
            :created_at, :created_at_ms, :author_id, :author_name, :text, :context_post_id,
            :context_url, :context_author_name, :context_text)
    on conflict (utterance_key) do update set
        target_user_id = excluded.target_user_id,
        source = excluded.source,
        source_id = excluded.source_id,
        kind = excluded.kind,
        post_id = excluded.post_id,
        post_url = excluded.post_url,
        created_at = excluded.created_at,
        created_at_ms = excluded.created_at_ms,
        author_id = excluded.author_id,
        author_name = excluded.author_name,
        text = excluded.text,
        context_post_id = excluded.context_post_id,
        context_url = excluded.context_url,
        context_author_name = excluded.context_author_name,
        context_text = excluded.context_text,
        last_seen_at = now()
    """
)

UPDATE_SCAN_STATE_SQL = text(
    """
    insert into xueqiu_archiver_post_scan_state (
        target_user_id, post_id, last_scanned_at, last_max_page, last_checked_page_count, last_waf_at
    )
    values (:target_user_id, :post_id, now(), :max_page, :page_count,
            case when :waf then now() else null end)
    on conflict (target_user_id, post_id) do update set
        last_scanned_at = now(),
        last_max_page = excluded.last_max_page,
        last_checked_page_count = excluded.last_checked_page_count,
        last_waf_at = case when :waf then now() else xueqiu_archiver_post_scan_state.last_waf_at end
    """
)


def _post_params(post: CandidatePost, detail_enriched: bool) -> Dict[str, Any]:
    return {
        "post_id": post.post_id,
        "url": post.url,
        "title": post.title,
        "author_id": post.author_id,
        "author_name": post.author_name,
        "text": post.text,
        "created_at": post.created_at,
        "created_at_ms": post.created_at_ms,
        "source": post.source,
        "detail_enriched": bool(detail_enriched),
    }


def _utterance_params(utterance: UserUtterance) -> Dict[str, Any]:
    return {
        "utterance_key": utterance.utterance_key,
        "target_user_id": utterance.target_user_id,
        "source": utterance.source,
        "source_id": utterance.source_id,
        "kind": utterance.kind,
        "post_id": utterance.post_id,
        "post_url": utterance.post_url,
        "created_at": utterance.created_at,
        "created_at_ms": utterance.created_at_ms,
        "author_id": utterance.author_id,
        "author_name": utterance.author_name,
        "text": utterance.text,
        "context_post_id": utterance.context_post_id,
        "context_url": utterance.context_url,
        "context_author_name": utterance.context_author_name,
        "context_text": utterance.context_text,
    }


class ArchiverStore:
    def __init__(self, db: Session, *, dry_run: bool = False) -> None:
        self.db = db
        self.dry_run = dry_run
        self.captured: Dict[str, List[Any]] = {
            "posts": [],
            "replies": [],
            "utterances": [],
            "scan_states": [],
        }

    # ---- posts ----
    def upsert_post(self, post: CandidatePost, detail_enriched: bool = False) -> None:
        if self.dry_run:
            self.captured["posts"].append((post.post_id, bool(detail_enriched)))
            return
        self.db.execute(UPSERT_POST_SQL, _post_params(post, detail_enriched))

    def load_posts(self, post_ids: Iterable[str]) -> Dict[str, Tuple[CandidatePost, bool]]:
        """{post_id: (帖, 是否已补全文)}——原实现全表加载，这里只取用到的 id，结果相同。"""
        ids = sorted({str(pid) for pid in post_ids})
        if not ids:
            return {}
        rows = self.db.execute(
            text(
                "select post_id, url, title, author_id, author_name, text, created_at, "
                "created_at_ms, source, detail_enriched "
                "from xueqiu_archiver_posts where post_id = any(:ids)"
            ),
            {"ids": ids},
        ).all()
        return {
            str(row[0]): (
                CandidatePost(
                    post_id=str(row[0]),
                    url=row[1],
                    title=row[2],
                    author_id=row[3],
                    author_name=row[4],
                    text=row[5],
                    created_at=row[6],
                    created_at_ms=int(row[7] or 0),
                    source=row[8],
                ),
                bool(row[9]),
            )
            for row in rows
        }

    def merge_candidates(self, candidates: List[CandidatePost]) -> List[CandidatePost]:
        """已补全文的帖以库内版本为准；其余以 detail_enriched=false 写入。"""
        existing = self.load_posts(post.post_id for post in candidates)
        merged: List[CandidatePost] = []
        for post in candidates:
            if post.post_id in existing and existing[post.post_id][1]:
                merged.append(existing[post.post_id][0])
            else:
                self.upsert_post(post, detail_enriched=False)
                merged.append(post)
        self.commit()
        return merged

    # ---- replies / utterances ----
    def upsert_reply(self, target_user_id: str, reply: MatchedReply) -> bool:
        reply_key = reply_dedupe_key(reply)
        if self.dry_run:
            self.captured["replies"].append(reply_key)
            return True
        row = self.db.execute(
            UPSERT_REPLY_SQL,
            {
                "reply_key": reply_key,
                "target_user_id": target_user_id,
                "post_id": reply.post_id,
                "post_url": reply.post_url,
                "comment_id": reply.comment_id,
                "created_at": reply.created_at,
                "author_id": reply.author_id,
                "author_name": reply.author_name,
                "text": reply.text,
                "like_count": reply.like_count,
                "created_at_ms": reply.created_at_ms,
                "reply_to": reply.reply_to,
            },
        ).one()
        return bool(row[0])

    def upsert_utterance(self, utterance: UserUtterance) -> None:
        if self.dry_run:
            self.captured["utterances"].append(utterance.utterance_key)
            return
        self.db.execute(UPSERT_UTTERANCE_SQL, _utterance_params(utterance))

    # ---- scan state ----
    def load_scan_state(self, target_user_id: str, post_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.execute(
            text(
                "select last_scanned_at, last_max_page, last_checked_page_count, last_waf_at "
                "from xueqiu_archiver_post_scan_state "
                "where target_user_id = :target_user_id and post_id = :post_id"
            ),
            {"target_user_id": target_user_id, "post_id": post_id},
        ).first()
        if not row:
            return None
        return {
            "last_scanned_at": row[0],
            "last_max_page": int(row[1] or 0),
            "last_checked_page_count": int(row[2] or 0),
            "last_waf_at": row[3],
        }

    def should_skip_recent_scan(
        self,
        target_user_id: str,
        post_id: str,
        cooldown_hours: float,
        *,
        now: Optional[float] = None,
    ) -> bool:
        if cooldown_hours <= 0:
            return False
        state = self.load_scan_state(target_user_id, post_id)
        last_scanned_at = state.get("last_scanned_at") if state else None
        if not last_scanned_at:
            return False
        age_seconds = (time.time() if now is None else now) - last_scanned_at.timestamp()
        return age_seconds < cooldown_hours * 60 * 60

    def update_scan_state(
        self,
        target_user_id: str,
        post_id: str,
        page_count: int,
        max_page: int,
        waf: bool = False,
    ) -> None:
        if self.dry_run:
            self.captured["scan_states"].append((post_id, page_count, max_page))
            return
        self.db.execute(
            UPDATE_SCAN_STATE_SQL,
            {
                "target_user_id": target_user_id,
                "post_id": post_id,
                "max_page": max_page,
                "page_count": page_count,
                "waf": bool(waf),
            },
        )

    def clear_scan_time(self, target_user_id: str, post_id: str) -> None:
        """评论扫描中途失败：清掉 last_scanned_at，让下一轮不受冷却期阻挡、重扫该帖
        （前面各页已写入的 last_max_page 等保留）。"""
        if self.dry_run:
            self.captured["scan_states"].append((post_id, "cleared"))
            return
        self.db.execute(
            text(
                "update xueqiu_archiver_post_scan_state set last_scanned_at = null "
                "where target_user_id = :target_user_id and post_id = :post_id"
            ),
            {"target_user_id": target_user_id, "post_id": post_id},
        )

    def commit(self) -> None:
        if self.dry_run:
            # 只读路径也要结束事务：长轮次里挂着的空闲事务会阻塞 VACUUM
            self.db.rollback()
            return
        self.db.commit()
