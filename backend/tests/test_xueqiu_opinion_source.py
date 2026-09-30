"""雪球观点数据源 reader：符号提取、归档表查询、新鲜度、降级。

表由迁移 20260927_0024 建出（DDL 与原 archiver 逐字一致，created_at 是 TEXT）：
reader 的价值全在那段查询（created_at_ms 时间过滤、LIKE 预筛），
monkeypatch 掉等于什么都没测。表总是存在，「未接入」= 采集器从未成功运行且无发言；
fixture 在前后清空 utterances / scan_runs。
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.database import SessionLocal
from app.services import xueqiu_opinion_source as src
from tests.helpers import drifted_last_seen_column


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
        session.rollback()
    finally:
        session.close()


def _clean(db):
    db.execute(
        text("TRUNCATE xueqiu_archiver_utterances, xueqiu_archiver_scan_runs RESTART IDENTITY")
    )
    db.commit()


@pytest.fixture
def archiver_table(db):
    """保留旧名：表由迁移保证存在，这里只负责前后清空。"""
    _clean(db)
    yield
    _clean(db)


def _insert(
    db,
    key,
    *,
    author="某作者",
    body="",
    context=None,
    post_url=None,
    context_url=None,
    kind="homepage_post",
    at=None,
    last_seen=None,
):
    at = at or datetime.now(timezone.utc)
    db.execute(
        text(
            "INSERT INTO xueqiu_archiver_utterances "
            "(utterance_key, target_user_id, source, kind, author_name, text, context_text, "
            " post_url, context_url, created_at_ms, last_seen_at) "
            "VALUES (:key, '1', 'profile_timeline', :kind, :author, :body, :context, "
            "        :post_url, :context_url, :ms, :last_seen)"
        ),
        {
            "key": key,
            "kind": kind,
            "author": author,
            "body": body,
            "context": context or "",
            "post_url": post_url or "",
            "context_url": context_url or "",
            "ms": int(at.timestamp() * 1000),
            "last_seen": last_seen or datetime.now(timezone.utc),
        },
    )
    db.commit()


def _scan_run(db, *, status="ok", finished=None):
    db.execute(
        text(
            "INSERT INTO xueqiu_archiver_scan_runs (target_user_id, author_user_id, status, "
            " finished_at) VALUES ('1', '1', :status, :finished)"
        ),
        {"status": status, "finished": finished or datetime.now(timezone.utc)},
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
    refs = src.extract_symbol_refs("$贵州茅台(SH600519)$", None, "https://xueqiu.com/S/00700/1", "")
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
    _insert(
        db,
        "u2",
        body="回复：同意",
        context="原帖聊 $腾讯控股(00700)$",
        at=now - timedelta(days=2),
        kind="comment_reply",
    )
    _insert(
        db,
        "u3",
        body="无标记发言",
        post_url="https://xueqiu.com/S/SH600519/9",
        at=now - timedelta(days=3),
    )
    _insert(db, "u4", body="$别的标的(SZ000001)$", at=now - timedelta(days=1))
    _insert(db, "u5", body="太老的 $贵州茅台(SH600519)$", at=now - timedelta(days=99))

    matched = src.scan_matched_utterances(db, {"SH600519", "00700"}, since=now - timedelta(days=30))
    assert sorted(row["utterance_key"] for row in matched["SH600519"]) == ["u1", "u3"]
    assert [row["utterance_key"] for row in matched["00700"]] == ["u2"]
    # 时间升序 + created_at 由 created_at_ms 还原为 aware datetime
    assert matched["SH600519"][0]["created_at"] < matched["SH600519"][1]["created_at"]
    assert matched["SH600519"][0]["created_at"].tzinfo is not None


def test_scan_row_hitting_two_symbols_appears_under_both(db, archiver_table):
    _insert(db, "u1", body="$贵州茅台(SH600519)$ 换 $腾讯控股(00700)$")
    matched = src.scan_matched_utterances(
        db,
        {"SH600519", "00700"},
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
    _insert(
        db,
        "u1",
        body="$贵州茅台(SH600519)$",
        last_seen=datetime.now(timezone.utc) - timedelta(hours=100),
    )
    monkeypatch.setattr(src.settings, "xueqiu_opinion_stale_hours", 48)
    fresh = src.source_freshness(db)
    assert fresh["available"] is True
    assert fresh["stale"] is True
    assert fresh["latest_scan_at"] is not None

    # 刚扫过则不 stale
    _insert(db, "u2", body="$贵州茅台(SH600519)$", last_seen=datetime.now(timezone.utc))
    assert src.source_freshness(db)["stale"] is False


def test_real_sql_error_recovers_session(db, archiver_table):
    """评审 P2：发言表列漂移触发真实 DBAPI 错误后，同一 Session 必须还能用。

    没有 SAVEPOINT 时事务被标成 aborted，后续任何查询都是
    InFailedSqlTransaction——"降级"变 500。表由迁移管理、不会缺列，这里临时改掉
    last_seen_at 的列名来复现同一类错误。
    """
    _insert(db, "k", body="$贵州茅台(SH600519)$")
    with drifted_last_seen_column(db):
        fresh = src.source_freshness(db)
        assert fresh["available"] is False
        # 同一 Session 继续查询必须成功（回归点）
        assert db.execute(text("SELECT 1")).scalar() == 1


def test_empty_source_degrades_explicitly(db, archiver_table):
    """表在但采集器从未成功运行、库里没有发言：available False、ensure 抛、scan 抛——
    绝不静默返回空。"""
    assert src.is_opinion_source_available(db) is False
    with pytest.raises(src.OpinionSourceUnavailable, match="未接入"):
        src.ensure_opinion_source(db)
    with pytest.raises(src.OpinionSourceUnavailable):
        src.scan_matched_utterances(db, {"SH600519"}, since=datetime.now(timezone.utc))
    fresh = src.source_freshness(db)
    assert fresh == {
        "available": False,
        "latest_scan_at": None,
        "latest_utterance_at": None,
        "stale": False,
    }
    # 失败的采集轮次不算"接入"
    _scan_run(db, status="failed")
    assert src.is_opinion_source_available(db) is False


def test_successful_scan_without_utterances_is_available(db, archiver_table):
    """采集器成功跑过但关注作者都没发言：已接入（匹配为空是真实的"无观点"）。"""
    finished = datetime.now(timezone.utc) - timedelta(hours=1)
    _scan_run(db, status="ok", finished=finished)
    assert src.is_opinion_source_available(db) is True
    fresh = src.source_freshness(db)
    assert fresh["available"] is True and fresh["stale"] is False
    assert datetime.fromisoformat(fresh["latest_scan_at"]) == finished
    assert fresh["latest_utterance_at"] is None
    assert (
        src.scan_matched_utterances(
            db, {"SH600519"}, since=datetime.now(timezone.utc) - timedelta(days=1)
        )
        == {}
    )


def test_liveness_prefers_successful_scan_runs(db, archiver_table, monkeypatch):
    """活性判据：最近一次成功采集优先，last_seen_at 只是没有采集记录时的兜底。"""
    monkeypatch.setattr(src.settings, "xueqiu_opinion_stale_hours", 48)
    _insert(db, "u1", body="x", last_seen=datetime.now(timezone.utc))
    old_ok = datetime.now(timezone.utc) - timedelta(hours=100)
    _scan_run(db, status="ok", finished=old_ok)
    _scan_run(db, status="failed")  # 更新的失败轮次不刷新活性
    fresh = src.source_freshness(db)
    assert datetime.fromisoformat(fresh["latest_scan_at"]) == old_ok
    assert fresh["stale"] is True


def test_snapshot_warns_only_when_available_and_stale(db, archiver_table, monkeypatch):
    """Dashboard 预警口径：已接入且停摆才报；未接入不报。"""
    from app.services.statistics import snapshot as snap_module

    _insert(
        db,
        "u1",
        body="$贵州茅台(SH600519)$",
        last_seen=datetime.now(timezone.utc) - timedelta(hours=100),
    )
    monkeypatch.setattr(src.settings, "xueqiu_opinion_stale_hours", 48)
    result = snap_module.build_portfolio_snapshot(db, user_id=1)
    assert any("停摆" in w for w in result["data_quality"]["warnings"])


def test_snapshot_silent_without_data(db, archiver_table):
    from app.services.statistics import snapshot as snap_module

    result = snap_module.build_portfolio_snapshot(db, user_id=1)
    assert not any("停摆" in w for w in result["data_quality"]["warnings"])
