"""原始报告文件的本地缓存（PDF / EDGAR 主文档）与生命周期。

解析规则一升版就要重新解析原件：此前原件不落地，每次都从巨潮/披露易/EDGAR 重新下载（港股
报表约 290 份、2GB，1 小时以上，还有下载过慢被中断的偶发失败）。这里把下载到的原件按 URL
存到本地持久化目录，`report_fetchers._download_guarded` 先查缓存、未命中才下载。

- **身份**：URL。三个站点的报告 URL 都按文件唯一（修订版是新 URL、新文件），同一 URL 的内容
  不会变；源指纹（`source_fingerprint`）变化时 URL 也随之变化，旧文件自然成为「无人引用」。
- **只缓存完整、合法的内容**：PDF 必须以 `%PDF` 开头；下载失败、被看门狗中断的一律不写。
- **写入原子**：同目录临时文件 + `os.replace`，进程被杀不会留下半截文件被当成缓存。
- **尽力而为**：缓存目录不存在、不可写、磁盘满都只记日志，不影响下载本身。

**生命周期**（`prune_report_cache`，每日周期任务与 `manage.py report-cache-prune`）：
1. 「被引用」= 当前跟踪标的（持仓 ∪ 自选）的报表抽取记录、章节节选、财报摘要里记着的 source_url
   ——也就是以后重跑会用到的原件；
2. 无人引用且最近一次使用早于 `report_cache_unreferenced_days` 天的删除（修订版的旧文件、滚出
   十年窗口的旧报告、不再跟踪的标的、股息公告表格——后者的原文已存在库里）；
3. 总量仍超过 `report_cache_max_gb` 时按最近使用时间从旧到新删，先删无人引用的，再删被引用的
   （被引用的删掉只是下次重跑要重新下载，不丢数据）。
「最近使用」记在文件的 mtime 上（命中时 touch）：atime 在 noatime 挂载下不可靠。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from ..config import settings
from ..core.logging import get_app_logger

logger = get_app_logger(__name__)

PDF_SOURCES = frozenset({"cninfo", "hkexnews"})
# 孤儿文件（正文没有元数据、元数据没有正文、写到一半的临时文件）的宽限期：给正在写入的请求留时间，
# 过了就当残留清掉——它们也计入用量，否则崩溃/磁盘满留下的正文能绕过总量上限（PR #382 评审 P2）
ORPHAN_GRACE_SECONDS = 3600
_warned_unavailable: Set[str] = set()


def cache_root() -> Optional[Path]:
    """缓存根目录；未配置、不存在或不可写时为 None（缓存关闭）。根目录不自动创建——它应是
    部署时挂载的持久化目录，开发环境与测试里不存在即视为关闭。"""
    raw = (settings.report_cache_dir or "").strip()
    if not raw:
        return None
    root = Path(raw)
    if not root.is_dir() or not os.access(root, os.W_OK):
        if raw not in _warned_unavailable:
            _warned_unavailable.add(raw)
            logger.warning("原始报告缓存目录不可用（不存在或不可写），缓存关闭: %s", raw)
        return None
    return root


def _key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _paths(root: Path, url: str) -> tuple[Path, Path]:
    key = _key(url)
    directory = root / key[:2]
    return directory / f"{key}.bin", directory / f"{key}.json"


def _valid(source: str, data: bytes) -> bool:
    if not data:
        return False
    if source in PDF_SOURCES:
        return data[:5] == b"%PDF-"
    return True


def get_cached(url: str, source: str) -> Optional[bytes]:
    root = cache_root()
    if root is None:
        return None
    body, meta = _paths(root, url)
    try:
        info = json.loads(meta.read_text(encoding="utf-8"))
        if info.get("url") != url:
            return None  # 哈希撞车（理论上）或元数据损坏：当作未命中
        data = body.read_bytes()
    except (OSError, ValueError):
        return None
    if len(data) != info.get("bytes") or not _valid(source, data):
        return None
    now = time.time()
    try:
        os.utime(body, (now, now))  # 最近使用时间（LRU 依据）
    except OSError:
        pass
    return data


def put_cached(url: str, source: str, data: bytes) -> bool:
    """写入缓存；返回是否写入。不合法的内容与任何写入错误都只记日志。"""
    root = cache_root()
    if root is None or not _valid(source, data):
        return False
    body, meta = _paths(root, url)
    info = {
        "url": url,
        "source": source,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "stored_at": int(time.time()),
    }
    try:
        body.parent.mkdir(parents=True, exist_ok=True)
        # 先元数据后正文：中途失败最多留下没有正文的小元数据（读取时判未命中、清理时当孤儿）；
        # 先正文的话，失败会留下几十 MB 没有元数据的正文
        for target, payload in (
            (meta, json.dumps(info, ensure_ascii=False).encode("utf-8")),
            (body, data),
        ):
            handle, tmp = tempfile.mkstemp(dir=body.parent, prefix=".tmp-")
            try:
                with os.fdopen(handle, "wb") as out:
                    out.write(payload)
                os.replace(tmp, target)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
    except OSError as exc:
        logger.warning("写入原始报告缓存失败（不影响本次下载）%s: %s", url, exc)
        for path in (body, meta):  # 不留半套：失败后两个文件都尽力删掉
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return False
    return True


def cached_download(url: str, source: str, fetch: Callable[[], bytes]) -> bytes:
    """先查缓存，未命中才调用 fetch 下载，并把合法的结果写入缓存。"""
    cached = get_cached(url, source)
    if cached is not None:
        return cached
    data = fetch()
    put_cached(url, source, data)
    return data


def _entries(root: Path) -> List[Dict[str, Any]]:
    """缓存里的全部条目。正文与元数据按文件名配对；只有一半的是孤儿（`url=None`），同样计入用量
    与清理——孤儿的「最近使用」取它自己的 mtime，用于宽限期判断。"""
    stems: Dict[Path, Dict[str, Path]] = {}
    for path in root.glob("*/*"):
        if path.name.startswith(".tmp-") or path.suffix not in (".bin", ".json"):
            continue
        stems.setdefault(path.with_suffix(""), {})[path.suffix] = path
    entries = []
    for stem, parts in stems.items():
        body, meta = stem.with_suffix(".bin"), stem.with_suffix(".json")
        try:
            body_stat = body.stat() if ".bin" in parts else None
            meta_stat = meta.stat() if ".json" in parts else None
        except OSError:
            continue  # 枚举与删除之间被别的进程删掉
        info = None
        if body_stat and meta_stat:
            try:
                info = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                info = None
        size = (body_stat.st_size if body_stat else 0) + (meta_stat.st_size if meta_stat else 0)
        if info is None or not info.get("url"):
            newest = max(s.st_mtime for s in (body_stat, meta_stat) if s)
            entries.append({"url": None, "body": body, "meta": meta, "bytes": size, "used": newest})
            continue
        entries.append(
            {
                "url": info["url"],
                "body": body,
                "meta": meta,
                "bytes": size,
                "used": body_stat.st_mtime,
            }
        )
    return entries


def cache_usage() -> Dict[str, Any]:
    root = cache_root()
    if root is None:
        return {"enabled": False, "files": 0, "bytes": 0}
    entries = _entries(root)
    return {
        "enabled": True,
        "files": len(entries),
        "bytes": sum(entry["bytes"] for entry in entries),
    }


def prune(
    referenced_urls: Iterable[str],
    *,
    now: Optional[float] = None,
    unreferenced_days: Optional[int] = None,
    max_bytes: Optional[int] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """按生命周期清理（见模块文档）。返回 {enabled, files, bytes, removed_unreferenced,
    removed_over_cap, freed_bytes}；dry_run 只统计不删。"""
    root = cache_root()
    if root is None:
        return {"enabled": False}
    now = time.time() if now is None else now
    days = (
        settings.report_cache_unreferenced_days if unreferenced_days is None else unreferenced_days
    )
    cap = int(settings.report_cache_max_gb * 1024**3) if max_bytes is None else max_bytes
    referenced = set(referenced_urls)
    entries = _entries(root)
    removed_unreferenced = removed_over_cap = freed = 0

    def remove(entry: Dict[str, Any]) -> None:
        nonlocal freed
        freed += entry["bytes"]
        if not dry_run:
            for path in (entry["body"], entry["meta"]):
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("删除原始报告缓存失败 %s: %s", path, exc)

    kept = []
    removed_orphans = 0
    for entry in entries:
        if entry["url"] is None:
            # 孤儿：过了宽限期就删（不等 30 天——它根本不能被命中）；宽限期内可能正在写入，留着
            if now - entry["used"] > ORPHAN_GRACE_SECONDS:
                remove(entry)
                removed_orphans += 1
            else:
                kept.append(entry)
        elif entry["url"] not in referenced and now - entry["used"] > days * 86400:
            remove(entry)
            removed_unreferenced += 1
        else:
            kept.append(entry)
    total = sum(entry["bytes"] for entry in kept)
    if total > cap:
        # 超过总量：无人引用的先删，同类里最久没用的先删
        # 宽限期内的孤儿不在这里删（可能正在写入），但它们的体积已计入 total
        candidates = [e for e in kept if e["url"] is not None]
        for entry in sorted(candidates, key=lambda e: (e["url"] in referenced, e["used"])):
            if total <= cap:
                break
            remove(entry)
            total -= entry["bytes"]
            removed_over_cap += 1
    # 残留的临时文件（进程在写入中途被杀）：一小时以上的直接清掉
    for tmp in root.glob("*/.tmp-*"):
        try:
            if now - tmp.stat().st_mtime > ORPHAN_GRACE_SECONDS and not dry_run:
                tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return {
        "enabled": True,
        "files": len(entries) - removed_unreferenced - removed_over_cap - removed_orphans,
        "bytes": total,
        "removed_orphans": removed_orphans,
        "removed_unreferenced": removed_unreferenced,
        "removed_over_cap": removed_over_cap,
        "freed_bytes": freed,
    }


# 引用原件的数据集：以后重跑（抽取器/构建/章节/摘要升版）会用到的原件都记在这几类行的 source_url 上
REFERENCING_DATASETS = ("report_statement_extract", "report_section", "report_digest")


def source_url_of(value: Any) -> Optional[str]:
    """行上 source_url 的两种形态 → 下载用的 URL：字符串（巨潮/披露易 PDF）或 EDGAR 引用
    `{cik, accession, document}`（美股摘要的 target.url）。"""
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict) and value.get("cik") and value.get("accession"):
        from .report_fetchers import edgar_filing_url

        return edgar_filing_url(value["cik"], value["accession"], value.get("document") or "")
    return None


def referenced_report_urls(db) -> Set[str]:
    """当前跟踪标的（活跃用户持仓 ∪ 自选）的抽取/节选/摘要行记着的原件 URL。"""
    from sqlalchemy import func

    from ..models.security_profile import SecurityProfileData
    from .security_industry_service import scope_keys

    scope = set(scope_keys(db))
    rows = (
        db.query(
            SecurityProfileData.symbol,
            SecurityProfileData.market,
            func.json_extract_path_text(SecurityProfileData.payload, "source_url"),
        )
        .filter(SecurityProfileData.dataset.in_(REFERENCING_DATASETS))
        .all()
    )
    urls: Set[str] = set()
    for symbol, market, raw in rows:
        if (symbol, market) not in scope or not raw:
            continue
        try:
            value = json.loads(raw) if raw.startswith("{") else raw
        except ValueError:
            continue
        url = source_url_of(value)
        if url:
            urls.add(url)
    return urls


def prune_report_cache(db, *, dry_run: bool = False) -> Dict[str, Any]:
    """生命周期清理的入口（周期任务与 manage.py 共用）。"""
    if cache_root() is None:
        return {"enabled": False}
    return prune(referenced_report_urls(db), dry_run=dry_run)


PERIODIC_INTERVAL_SECONDS = 24 * 3600


def periodic_prune_report_cache():
    """周期任务入口（以 prune_report_cache 注册）：缓存关闭报 skipped；清理异常报 failed。"""
    from ..database import SessionLocal
    from .job_worker import PeriodicOutcome

    if cache_root() is None:
        return PeriodicOutcome.skipped("原始报告缓存未启用（REPORT_CACHE_DIR 不存在或不可写）")
    db = SessionLocal()
    try:
        result = prune_report_cache(db)
    except Exception as exc:  # noqa: BLE001 - 周期线程必须自吞异常
        db.rollback()
        logger.warning("原始报告缓存清理失败: %s", str(exc)[:200])
        return PeriodicOutcome.failed(
            f"原始报告缓存清理失败：{type(exc).__name__}: {str(exc)[:200]}"
        )
    finally:
        db.close()
    removed = (
        result["removed_unreferenced"] + result["removed_over_cap"] + result["removed_orphans"]
    )
    logger.info(
        "原始报告缓存：%d 个文件 %.1f MB，本次删除 %d 个（无人引用 %d、超总量 %d、孤儿 %d）",
        result["files"],
        result["bytes"] / 1e6,
        removed,
        result["removed_unreferenced"],
        result["removed_over_cap"],
        result["removed_orphans"],
    )
    return PeriodicOutcome.succeeded(removed)


# 等价于 @periodic_outcome_task（本模块由 report_fetchers 在导入早期加载，不在顶层导入 job_worker）
periodic_prune_report_cache.periodic_outcome_contract = True  # type: ignore[attr-defined]
