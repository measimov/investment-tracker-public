"""旧仓库 monitor_symbols Markdown 产物的一次性导入（`manage.py xueqiu-import-archive-exports`）。

原则：雪球数据尽量复用，收纳后首轮不做历史回溯——旧仓库 exports/ 下已有的按标的
Markdown（每日一份快照）一次性导入，之后只靠每日一页的增量。

字段评估（原 render_timeline / render_hots 的渲染口径）：
- `### YYYY-MM-DD HH:MM:SS`：发帖时间（宿主东八区 localtime，秒精度）→ 按业务时区换回毫秒。
- `[原文链接](https://xueqiu.com/{uid}/{post_id})`：post_id 与作者 ID（讨论/热帖）；
  公告是 `/S/{雪球symbol}/{post_id}`，没有作者。
- `赞 N · 评 M`：公告/讨论是 fav_count / reply_count，热帖只有 fav_count。
- 正文：html_to_text 后的纯文本（`text` → `description` → `title` 取第一个非空），
  `（无正文）` 视为空。**丢失**：作者昵称、标题（有正文时）、附件链接。
- 组合调仓 Markdown 没有调仓记录 ID，也只有股票名称没有代码 → **不导入**（无法幂等）。

写入一律 **insert-only**（ON CONFLICT DO NOTHING）：已由采集器写入的行更完整，旧快照
不得覆盖；同一帖在多份快照里出现时按文件时间**从新到旧**处理，保留最新快照的计数与
热帖名次。标的身份键由文件名里的雪球 symbol 经 `from_xueqiu` 换成本仓 (symbol, market)，
雪球格式 symbol 不落库。Markdown 文件本身只读，不复制。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ...core.timeutil import business_timezone
from .feed_parsing import KIND_ANNOUNCEMENT, KIND_DISCUSSION, FeedPost, from_xueqiu
from .feed_store import upsert_hot_posts, upsert_symbol_posts

FILE_RE = re.compile(
    r"^(?:stock-(?P<kind>announcement|discussion)-(?P<xq>[A-Z0-9.]+)|market-hots-(?P<scope>[a-z]+))"
    r"-(?P<stamp>\d{8}-\d{6})\.md$"
)
BLOCK_RE = re.compile(r"^### (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\b.*$", re.M)
LINK_RE = re.compile(
    r"^\[原文链接\]\((?P<url>https://xueqiu\.com/(?P<path>[^)\s]+))\)(?P<rest>.*)$"
)
FAV_RE = re.compile(r"赞 (\d+)")
REPLY_RE = re.compile(r"评 (\d+)")
EMPTY_BODY = "（无正文）"


@dataclass
class ArchiveFile:
    path: Path
    snapshot_at: datetime
    kind: Optional[str] = None  # announcement / discussion；热帖为 None
    symbol: Optional[str] = None
    market: Optional[str] = None
    scope: Optional[str] = None


@dataclass
class ImportStats:
    files: int = 0
    skipped_files: List[str] = field(default_factory=list)
    parsed: Dict[str, int] = field(default_factory=dict)
    inserted: Dict[str, int] = field(default_factory=dict)
    with_author: Dict[str, int] = field(default_factory=dict)
    with_text: Dict[str, int] = field(default_factory=dict)
    malformed_blocks: int = 0
    symbols: set = field(default_factory=set)
    unique: Dict[str, set] = field(default_factory=dict)

    def bump(self, bucket: Dict[str, int], key: str, amount: int = 1) -> None:
        bucket[key] = bucket.get(key, 0) + amount


def _local_ms(text: str) -> int:
    moment = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=business_timezone())
    return int(moment.timestamp() * 1000)


def classify_file(path: Path) -> Optional[ArchiveFile]:
    """文件名 → 类型/标的/快照时间；不是按标的产物（或标的认不出）返回 None。"""
    matched = FILE_RE.match(path.name)
    if not matched:
        return None
    snapshot_at = datetime.strptime(matched.group("stamp"), "%Y%m%d-%H%M%S").replace(
        tzinfo=business_timezone()
    )
    if matched.group("scope"):
        return ArchiveFile(path=path, snapshot_at=snapshot_at, scope=matched.group("scope"))
    pair = from_xueqiu(matched.group("xq"))
    if pair is None:
        return None
    return ArchiveFile(
        path=path, snapshot_at=snapshot_at, kind=matched.group("kind"),
        symbol=pair[0], market=pair[1],
    )


def parse_markdown(text: str) -> Tuple[List[FeedPost], int]:
    """一份 Markdown → (帖子列表（文件内顺序）, 无法解析的块数)。"""
    posts: List[FeedPost] = []
    malformed = 0
    matches = list(BLOCK_RE.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        lines = text[match.end():end].strip("\n").splitlines()
        link = LINK_RE.match(lines[0].strip()) if lines else None
        if link is None:
            malformed += 1
            continue
        parts = link.group("path").strip("/").split("/")
        post_id = parts[-1]
        if not post_id.isdigit():
            malformed += 1
            continue
        author_id = ""
        if len(parts) == 2 and parts[0].isdigit():
            author_id = parts[0]
        body = "\n".join(lines[1:]).strip()
        if body == EMPTY_BODY:
            body = ""
        payload: Dict[str, Any] = {"archive_import": True}
        fav = FAV_RE.search(link.group("rest"))
        reply = REPLY_RE.search(link.group("rest"))
        if fav:
            payload["fav_count"] = int(fav.group(1))
        if reply:
            payload["reply_count"] = int(reply.group(1))
        posts.append(FeedPost(
            post_id=post_id,
            created_at_ms=_local_ms(match.group(1)),
            text=body,
            author_id=author_id,
            url=link.group("url"),
            payload=payload,
        ))
    return posts, malformed


def import_archive_exports(db: Session, directory: Path, *, dry_run: bool = False) -> ImportStats:
    """导入目录下全部按标的 Markdown；dry_run 只解析统计不写库。"""
    stats = ImportStats()
    files: List[ArchiveFile] = []
    for path in sorted(directory.glob("*.md")):
        if not path.name.startswith(("stock-announcement-", "stock-discussion-", "market-hots-")):
            continue
        archive = classify_file(path)
        if archive is None:
            stats.skipped_files.append(path.name)
            continue
        files.append(archive)
    # 从新到旧：insert-only 下同一帖保留最新快照的计数/名次
    files.sort(key=lambda item: item.snapshot_at, reverse=True)
    for archive in files:
        stats.files += 1
        posts, malformed = parse_markdown(archive.path.read_text(encoding="utf-8"))
        stats.malformed_blocks += malformed
        key = archive.kind or f"hots:{archive.scope}"
        stats.bump(stats.parsed, key, len(posts))
        stats.bump(stats.with_author, key, sum(1 for post in posts if post.author_id))
        stats.bump(stats.with_text, key, sum(1 for post in posts if post.text))
        stats.unique.setdefault(key, set()).update(
            (archive.symbol, archive.market, post.post_id) for post in posts
        )
        if archive.kind:
            stats.symbols.add((archive.symbol, archive.market))
        if dry_run or not posts:
            continue
        if archive.kind in (KIND_ANNOUNCEMENT, KIND_DISCUSSION):
            inserted, _ = upsert_symbol_posts(
                db, archive.symbol, archive.market, archive.kind, posts, overwrite=False
            )
        else:
            inserted, _ = upsert_hot_posts(
                db, archive.scope, posts, archive.snapshot_at, overwrite=False
            )
        db.commit()
        stats.bump(stats.inserted, key, inserted)
    return stats
