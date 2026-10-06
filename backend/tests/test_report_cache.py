"""原始报告文件缓存与生命周期（report_cache）。"""

import json
import os
import time

import pytest

from app.config import settings
from app.database import SessionLocal
from app.models.holding import Holding
from app.models.security_profile import SecurityProfileData
from app.models.user import User
from app.models.watchlist_item import WatchlistItem
from app.services import report_cache, report_fetchers
from app.services.profile_store import upsert_profile_row

from .helpers import reset_tables

PDF = b"%PDF-1.7\n" + b"x" * 100
URL = "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0401/a.pdf"


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    root = tmp_path / "reports"
    root.mkdir()
    monkeypatch.setattr(settings, "report_cache_dir", str(root))
    return root


def _counting_fetch(data=PDF):
    calls = []

    def fetch():
        calls.append(1)
        return data

    return fetch, calls


def test_hit_avoids_download_and_marks_recent_use(cache_dir):
    fetch, calls = _counting_fetch()
    assert report_cache.cached_download(URL, "hkexnews", fetch) == PDF
    body, _meta = report_cache._paths(cache_dir, URL)
    old = time.time() - 10 * 86400
    os.utime(body, (old, old))
    assert report_cache.cached_download(URL, "hkexnews", fetch) == PDF
    assert len(calls) == 1
    assert body.stat().st_mtime > old + 86400  # 命中即更新最近使用时间
    assert not list(cache_dir.glob("*/.tmp-*"))


def test_disabled_when_directory_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "report_cache_dir", str(tmp_path / "absent"))
    fetch, calls = _counting_fetch()
    report_cache.cached_download(URL, "hkexnews", fetch)
    report_cache.cached_download(URL, "hkexnews", fetch)
    assert len(calls) == 2
    assert not (tmp_path / "absent").exists()  # 根目录不自动创建


def test_invalid_or_failed_downloads_are_not_cached(cache_dir):
    fetch, calls = _counting_fetch(b"<html>login</html>")
    report_cache.cached_download(URL, "hkexnews", fetch)
    report_cache.cached_download(URL, "hkexnews", fetch)
    assert len(calls) == 2  # 不是 PDF：不缓存

    def boom():
        raise TimeoutError("slow")

    with pytest.raises(TimeoutError):
        report_cache.cached_download(URL, "cninfo", boom)
    assert report_cache.get_cached(URL, "cninfo") is None
    # EDGAR 主文档是 HTML：不要求 %PDF 头
    edgar = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/a.htm"
    fetch, calls = _counting_fetch(b"<html>10-K</html>")
    report_cache.cached_download(edgar, "edgar", fetch)
    report_cache.cached_download(edgar, "edgar", fetch)
    assert len(calls) == 1


def test_truncated_or_mismatched_entries_are_misses(cache_dir):
    assert report_cache.put_cached(URL, "hkexnews", PDF)
    body, meta = report_cache._paths(cache_dir, URL)
    body.write_bytes(PDF[:20])  # 长度对不上
    assert report_cache.get_cached(URL, "hkexnews") is None
    report_cache.put_cached(URL, "hkexnews", PDF)
    info = json.loads(meta.read_text())
    meta.write_text(json.dumps({**info, "url": "https://other"}))
    assert report_cache.get_cached(URL, "hkexnews") is None


def test_download_report_pdf_goes_through_the_cache(cache_dir, monkeypatch):
    calls = []

    def fake(url, headers, *, source, interval):
        calls.append(source)
        return PDF

    monkeypatch.setattr(report_fetchers, "_download_with_retry", fake)
    assert report_fetchers.download_report_pdf(URL, source="hkexnews") == PDF
    assert report_fetchers.download_report_pdf(URL, source="hkexnews") == PDF
    assert calls == ["hkexnews"]


def _age(cache_dir, url, days, *, now):
    body, _ = report_cache._paths(cache_dir, url)
    ts = now - days * 86400
    os.utime(body, (ts, ts))


def _exists(cache_dir, url):
    """只看文件是否还在（get_cached 会更新最近使用时间，不能拿来断言清理结果）。"""
    return report_cache._paths(cache_dir, url)[0].exists()


def test_prune_removes_old_unreferenced_and_keeps_referenced(cache_dir):
    now = time.time()
    urls = [f"https://static.cninfo.com.cn/final/{i}.PDF" for i in range(4)]
    for url in urls:
        report_cache.put_cached(url, "cninfo", PDF)
    _age(cache_dir, urls[0], 40, now=now)  # 无人引用、久未使用 → 删
    _age(cache_dir, urls[1], 40, now=now)  # 被引用 → 留
    _age(cache_dir, urls[2], 5, now=now)  # 无人引用但最近用过 → 留
    dry = report_cache.prune({urls[1]}, now=now, unreferenced_days=30, dry_run=True)
    assert dry["removed_unreferenced"] == 1 and _exists(cache_dir, urls[0])
    result = report_cache.prune({urls[1]}, now=now, unreferenced_days=30)
    assert result["removed_unreferenced"] == 1 and result["removed_over_cap"] == 0
    assert not _exists(cache_dir, urls[0])
    assert all(_exists(cache_dir, u) for u in urls[1:])


def test_prune_enforces_size_cap_unreferenced_first_then_oldest(cache_dir):
    now = time.time()
    urls = [f"https://static.cninfo.com.cn/final/c{i}.PDF" for i in range(4)]
    for days, url in zip((1, 2, 3, 4), urls):
        report_cache.put_cached(url, "cninfo", PDF)
        _age(cache_dir, url, days, now=now)
    # 每个条目 = 正文 + 元数据（用量统计两者都算）；四个条目大小相同
    size = report_cache.cache_usage()["bytes"] // 4
    # 引用 c2、c3（最旧的两个）；上限只容两个文件：先删无人引用的 c1（更旧）再删 c0
    result = report_cache.prune(
        {urls[2], urls[3]}, now=now, unreferenced_days=30, max_bytes=2 * size
    )
    assert result["removed_over_cap"] == 2 and result["bytes"] == 2 * size
    assert [_exists(cache_dir, u) for u in urls] == [False, False, True, True]
    # 仍超上限时才动被引用的：最久没用的先删
    result = report_cache.prune({urls[2], urls[3]}, now=now, unreferenced_days=30, max_bytes=size)
    assert result["removed_over_cap"] == 1
    assert _exists(cache_dir, urls[2]) and not report_cache.get_cached(urls[3], "cninfo")


def test_prune_clears_stale_temp_files(cache_dir):
    report_cache.put_cached(URL, "hkexnews", PDF)
    body, _ = report_cache._paths(cache_dir, URL)
    tmp = body.parent / ".tmp-abandoned"
    tmp.write_bytes(b"half")
    old = time.time() - 7200
    os.utime(tmp, (old, old))
    report_cache.prune(set(), unreferenced_days=30)
    assert not tmp.exists()


def test_failed_metadata_or_body_write_leaves_nothing_behind(cache_dir, monkeypatch):
    """PR #382 评审 P2：写到一半失败（进程被杀之外的 OSError）不得留下半套文件。"""
    real_replace = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 2:  # 第二步（正文）失败
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(report_cache.os, "replace", flaky)
    assert report_cache.put_cached(URL, "hkexnews", PDF) is False
    assert str(calls[0]).endswith(".json")  # 先元数据
    assert not [p for p in cache_dir.rglob("*") if p.is_file()]
    assert report_cache.cache_usage()["files"] == 0


def test_orphans_count_toward_usage_and_are_reclaimed_after_grace(cache_dir):
    """进程在两步之间被杀：只有正文或只有元数据的文件计入用量，宽限期后清掉。"""
    now = time.time()
    report_cache.put_cached(URL, "hkexnews", PDF)
    body, meta = report_cache._paths(cache_dir, URL)
    meta.unlink()  # 只剩正文（旧版本写入顺序或外部删除）
    other = "https://static.cninfo.com.cn/final/z.PDF"
    report_cache.put_cached(other, "cninfo", PDF)
    other_body, other_meta = report_cache._paths(cache_dir, other)
    other_body.unlink()  # 只剩元数据
    usage = report_cache.cache_usage()
    assert usage["files"] == 2 and usage["bytes"] >= len(PDF)
    # 宽限期内：计入用量但不删（可能正在写入），也不被总量上限误删
    result = report_cache.prune(set(), now=now, unreferenced_days=30, max_bytes=0)
    assert result["removed_orphans"] == 0 and body.exists()
    assert result["bytes"] >= len(PDF)
    for path in (body, other_meta):
        old = now - 2 * report_cache.ORPHAN_GRACE_SECONDS
        os.utime(path, (old, old))
    result = report_cache.prune(set(), now=now, unreferenced_days=30)
    assert result["removed_orphans"] == 2
    assert not body.exists() and not other_meta.exists()
    assert report_cache.cache_usage() == {"enabled": True, "files": 0, "bytes": 0}


def test_source_url_of_edgar_reference():
    assert report_cache.source_url_of(URL) == URL
    assert report_cache.source_url_of(
        {"cik": 1, "accession": "0001-26-000001", "document": "a.htm"}
    ) == ("https://www.sec.gov/Archives/edgar/data/1/000126000001/a.htm")
    assert report_cache.source_url_of(None) is None


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        reset_tables(session, [SecurityProfileData, WatchlistItem, Holding])
        yield session
        session.rollback()
        reset_tables(session, [SecurityProfileData, WatchlistItem, Holding])
    finally:
        session.close()


def test_referenced_urls_are_the_tracked_symbols_rerun_inputs(db):
    user_id = db.query(User).filter(User.username == "demo").one().id
    db.add(WatchlistItem(user_id=user_id, symbol="00700", market="港股"))
    db.add(WatchlistItem(user_id=user_id, symbol="PDD", market="美股"))
    db.commit()
    tracked = "https://www1.hkexnews.hk/a.pdf"
    upsert_profile_row(
        db, "00700", "港股", "report_statement_extract", "20251231|annual", {"source_url": tracked}
    )
    upsert_profile_row(
        db,
        "PDD",
        "美股",
        "report_section",
        "20251231|20-F",
        {"source_url": {"cik": 1737806, "accession": "0001-26-000002", "document": "pdd.htm"}},
    )
    upsert_profile_row(
        db, "02313", "港股", "report_statement_extract", "20251231|annual", {"source_url": "x"}
    )  # 不再跟踪
    upsert_profile_row(db, "00700", "港股", "hkex_dividend_form", "doc1", {"source_url": "y"})
    db.commit()
    assert report_cache.referenced_report_urls(db) == {
        tracked,
        "https://www.sec.gov/Archives/edgar/data/1737806/000126000002/pdd.htm",
    }


def test_periodic_entry_skips_when_disabled_and_reports_removed(tmp_path, monkeypatch, db):
    monkeypatch.setattr(settings, "report_cache_dir", str(tmp_path / "absent"))
    outcome = report_cache.periodic_prune_report_cache()
    assert outcome.status == "skipped"
    root = tmp_path / "reports"
    root.mkdir()
    monkeypatch.setattr(settings, "report_cache_dir", str(root))
    report_cache.put_cached(URL, "hkexnews", PDF)
    body, _ = report_cache._paths(root, URL)
    old = time.time() - 60 * 86400
    os.utime(body, (old, old))
    outcome = report_cache.periodic_prune_report_cache()
    assert outcome.status == "succeeded" and outcome.count == 1
