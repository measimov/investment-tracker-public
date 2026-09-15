"""雪球观点数据源 reader：符号提取、外部表 raw SQL、新鲜度、降级。

外部表读取用**真 DDL fixture**（列类型与生产一致，尤其 created_at 是 TEXT）：
reader 的价值全在那段 raw SQL（created_at_ms 时间过滤、LIKE 预筛），
monkeypatch 掉等于什么都没测。表归 xueqiu-timeline-archiver 所有，生产上
本应用只读——fixture 建表仅为测试，teardown 即删。
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.database import SessionLocal
from app.services import xueqiu_opinion_source as src

UTTERANCE_DDL = """
CREATE TABLE IF NOT EXISTS xueqiu_archiver_utterances (
    utterance_key text PRIMARY KEY,
    target_user_id text, source text, source_id text, kind text,
    post_id text, post_url text,
    created_at text,              -- 生产实测就是 TEXT，不是 timestamptz
    created_at_ms bigint,
    author_id text, author_name text, text text,
    context_post_id text, context_url text, context_author_name text,
    context_text text,
    first_seen_at timestamptz, last_seen_at timestamptz
)
"""


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
        session.rollback()
    finally:
        session.close()


@pytest.fixture
def archiver_table(db):
    db.execute(text(UTTERANCE_DDL))
    db.commit()
    yield
    db.execute(text("DROP TABLE IF EXISTS xueqiu_archiver_utterances"))
    db.commit()


def _insert(db, key, *, author="管我财", body="", context=None, post_url=None,
            context_url=None, kind="homepage_post", at=None, last_seen=None):
    at = at or datetime.now(timezone.utc)
    db.execute(
        text(
            "INSERT INTO xueqiu_archiver_utterances "
            "(utterance_key, kind, author_name, text, context_text, post_url, "
            " context_url, created_at_ms, last_seen_at) "
            "VALUES (:key, :kind, :author, :body, :context, :post_url, "
            "        :context_url, :ms, :last_seen)"
        ),
        {
            "key": key, "kind": kind, "author": author, "body": body,
            "context": context, "post_url": post_url, "context_url": context_url,
            "ms": int(at.timestamp() * 1000),
            "last_seen": last_seen or datetime.now(timezone.utc),
        },
    )
    db.commit()


# --------------------------------------------------------------------------- #
# 符号提取（纯函数金样）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text_value", "expected"),
    [
        ("$药明康德(SH603259)$ 三季报超预期", {"SH603259"}),
        ("加仓 $贵州茅台(SH600519)$ 和 $腾讯控股(00700)$", {"SH600519", "00700"}),
        ("$苹果(AAPL)$ vs $伯克希尔(BRK.A)$", {"AAPL", "BRK.A"}),
        ("https://xueqiu.com/S/06666/407442726", {"06666"}),
        ("见 https://xueqiu.com/S/SZ000538/312345678 的讨论", {"SZ000538"}),
        ("$没有括号$", set()),
        ("裸括号 (600519) 不算 cashtag", set()),
        ("", set()),
    ],
)
def test_extract_symbol_refs(text_value, expected):
    assert src.extract_symbol_refs(text_value) == expected


def test_extract_symbol_refs_merges_multiple_fields():
    refs = src.extract_symbol_refs(
        "$贵州茅台(SH600519)$", None, "https://xueqiu.com/S/00700/1", ""
    )
    assert refs == {"SH600519", "00700"}


def test_build_wanted_map_normalizes_identity():
    """A股加前缀（含 920xxx 北交所纠正）、港股 zfill(5)、美股原样。"""
    wanted = src.build_wanted_map(
        [("600519", "A股"), ("920599", "A股"), ("700", "港股"), ("PDD", "美股")]
    )
    assert wanted == {
        "SH600519": ("600519", "A股"),
        "BJ920599": ("920599", "A股"),
        "00700": ("700", "港股"),
        "PDD": ("PDD", "美股"),
    }


# --------------------------------------------------------------------------- #
# 外部表读取（真 DDL）
# --------------------------------------------------------------------------- #
def test_scan_assigns_rows_to_wanted_keys(db, archiver_table):
    now = datetime.now(timezone.utc)
    _insert(db, "u1", body="看好 $贵州茅台(SH600519)$", at=now - timedelta(days=1))
    _insert(db, "u2", body="回复：同意", context="原帖聊 $腾讯控股(00700)$",
            at=now - timedelta(days=2), kind="comment_reply")
    _insert(db, "u3", body="无标记发言", post_url="https://xueqiu.com/S/SH600519/9",
            at=now - timedelta(days=3))
    _insert(db, "u4", body="$别的标的(SZ000001)$", at=now - timedelta(days=1))
    _insert(db, "u5", body="太老的 $贵州茅台(SH600519)$", at=now - timedelta(days=99))

    matched = src.scan_matched_utterances(
        db, {"SH600519", "00700"}, since=now - timedelta(days=30)
    )
    assert sorted(row["utterance_key"] for row in matched["SH600519"]) == ["u1", "u3"]
    assert [row["utterance_key"] for row in matched["00700"]] == ["u2"]
    # 时间升序 + created_at 由 created_at_ms 还原为 aware datetime
    assert matched["SH600519"][0]["created_at"] < matched["SH600519"][1]["created_at"]
    assert matched["SH600519"][0]["created_at"].tzinfo is not None


def test_scan_row_hitting_two_symbols_appears_under_both(db, archiver_table):
    _insert(db, "u1", body="$贵州茅台(SH600519)$ 换 $腾讯控股(00700)$")
    matched = src.scan_matched_utterances(
        db, {"SH600519", "00700"},
        since=datetime.now(timezone.utc) - timedelta(days=1),
    )
    assert {row["utterance_key"] for row in matched["SH600519"]} == {"u1"}
    assert {row["utterance_key"] for row in matched["00700"]} == {"u1"}


def test_hk_holding_bare_code_matches_padded_cashtag(db, archiver_table):
    """持仓存"700"，cashtag 是"00700"——经 build_wanted_map 归一后必须命中。"""
    _insert(db, "u1", body="$腾讯控股(00700)$ 回购")
    wanted = src.build_wanted_map([("700", "港股")])
    matched = src.scan_matched_utterances(
        db, set(wanted), since=datetime.now(timezone.utc) - timedelta(days=1)
    )
    assert [row["utterance_key"] for row in matched["00700"]] == ["u1"]


# --------------------------------------------------------------------------- #
# 新鲜度与降级
# --------------------------------------------------------------------------- #
def test_source_freshness_stale_detection(db, archiver_table, monkeypatch):
    _insert(db, "u1", body="$贵州茅台(SH600519)$",
            last_seen=datetime.now(timezone.utc) - timedelta(hours=100))
    monkeypatch.setattr(src.settings, "xueqiu_opinion_stale_hours", 48)
    fresh = src.source_freshness(db)
    assert fresh["available"] is True
    assert fresh["stale"] is True
    assert fresh["latest_scan_at"] is not None

    # 刚扫过则不 stale
    _insert(db, "u2", body="$贵州茅台(SH600519)$", last_seen=datetime.now(timezone.utc))
    assert src.source_freshness(db)["stale"] is False


def test_real_sql_error_recovers_session(db):
    """评审 P2：外部表列漂移触发真实 DBAPI 错误后，同一 Session 必须还能用。

    没有 SAVEPOINT 时事务被标成 aborted，后续任何查询都是
    InFailedSqlTransaction——"降级"变 500。
    """
    db.execute(text(
        "CREATE TABLE IF NOT EXISTS xueqiu_archiver_utterances "
        "(utterance_key text PRIMARY KEY, created_at_ms bigint)"  # 刻意缺 last_seen_at
    ))
    db.commit()
    try:
        fresh = src.source_freshness(db)
        assert fresh["available"] is False
        # 同一 Session 继续查询必须成功（回归点）
        assert db.execute(text("SELECT 1")).scalar() == 1
    finally:
        db.execute(text("DROP TABLE IF EXISTS xueqiu_archiver_utterances"))
        db.commit()


def test_missing_table_degrades_explicitly(db):
    """无表：available False、ensure 抛、scan 抛——绝不静默返回空。"""
    assert src.is_opinion_source_available(db) is False
    with pytest.raises(src.OpinionSourceUnavailable):
        src.ensure_opinion_source(db)
    with pytest.raises(src.OpinionSourceUnavailable):
        src.scan_matched_utterances(db, {"SH600519"}, since=datetime.now(timezone.utc))
    fresh = src.source_freshness(db)
    assert fresh == {
        "available": False, "latest_scan_at": None,
        "latest_utterance_at": None, "stale": False,
    }


def test_snapshot_warns_only_when_available_and_stale(db, archiver_table, monkeypatch):
    """Dashboard 预警口径：已接入且停摆才报；未接入不报。"""
    from app.services.statistics import snapshot as snap_module

    _insert(db, "u1", body="$贵州茅台(SH600519)$",
            last_seen=datetime.now(timezone.utc) - timedelta(hours=100))
    monkeypatch.setattr(src.settings, "xueqiu_opinion_stale_hours", 48)
    result = snap_module.build_portfolio_snapshot(db, user_id=1)
    assert any("停摆" in w for w in result["data_quality"]["warnings"])


def test_snapshot_silent_without_table(db):
    from app.services.statistics import snapshot as snap_module

    result = snap_module.build_portfolio_snapshot(db, user_id=1)
    assert not any("停摆" in w for w in result["data_quality"]["warnings"])
