"""归档表 upsert 与存量数据的兼容性 + 模型/迁移与原 DDL 的一致性。

存量行按 xueqiu-timeline-archiver 的**原格式**直接 INSERT（它写生产库时的形态），
再用本仓代码对同一实体 upsert：主键不重复、各列按原冲突语义覆盖/保留。
"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from app import models  # noqa: F401
from app.database import SessionLocal, engine
from app.services.xueqiu_collector.comments import utterance_from_reply
from app.services.xueqiu_collector.common import CandidatePost, MatchedReply
from app.services.xueqiu_collector.store import ArchiverStore
from app.services.xueqiu_collector.timeline import profile_utterance_from_status

ARCHIVER_TABLES = (
    "xueqiu_archiver_posts",
    "xueqiu_archiver_replies",
    "xueqiu_archiver_post_scan_state",
    "xueqiu_archiver_scan_runs",
    "xueqiu_archiver_utterances",
)
MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20260927_0024_xueqiu_collector_tables.py"
)


def _truncate(db):
    db.execute(text(f"TRUNCATE {', '.join(ARCHIVER_TABLES)} RESTART IDENTITY CASCADE"))
    db.commit()


@pytest.fixture
def db():
    session = SessionLocal()
    _truncate(session)
    try:
        yield session
    finally:
        session.rollback()
        _truncate(session)
        session.close()


def _migration_module():
    spec = importlib.util.spec_from_file_location("collector_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# 迁移 = 原 DDL（模型 = 迁移由全库的 test_model_schema_parity 守护）
# --------------------------------------------------------------------------- #
def test_original_archiver_ddl_is_a_no_op_on_migrated_db():
    """生产上表已由 archiver 建好：再跑一遍原 DDL（IF NOT EXISTS）不报错、不改结构。"""
    module = _migration_module()

    def columns(conn):
        return conn.execute(
            text(
                "select table_name, column_name, data_type, is_nullable, column_default "
                "from information_schema.columns where table_name like 'xueqiu_archiver%' "
                "order by 1, 2"
            )
        ).all()

    with engine.connect() as conn:
        before = columns(conn)
        for statement in module.ARCHIVER_DDL:
            conn.execute(text(statement))
        after = columns(conn)
        conn.rollback()
    assert before == after
    assert not module.SEED_AUTHORS, "公开镜像不带个人关注名单（PUBLICATION 规则）"


# --------------------------------------------------------------------------- #
# upsert 与存量行兼容
# --------------------------------------------------------------------------- #
def _insert_archiver_rows(db):
    """原仓库写入的形态：已补全文的帖、评论回复、主页发言、扫描状态。"""
    db.execute(
        text(
            "insert into xueqiu_archiver_posts (post_id, url, title, author_id, author_name, text, "
            " created_at, created_at_ms, source, detail_enriched, first_seen_at, last_seen_at, "
            " last_detail_fetch_at) values ('900001', 'https://xueqiu.com/111/900001', '', '111', "
            " '原作者', '全文正文（archiver 抓的）', '2026-09-20 10:00:00', 1789869600000, "
            " 'monitor-retweeted:222', true, '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z', "
            " '2026-09-01T00:00:00Z')"
        )
    )
    db.execute(
        text(
            "insert into xueqiu_archiver_replies (reply_key, target_user_id, post_id, post_url, "
            " comment_id, created_at, author_id, author_name, text, like_count, created_at_ms, "
            " reply_to, first_seen_at, last_seen_at) values ('777', '222', '900001', "
            " 'https://xueqiu.com/111/900001', '777', '2026-09-20 11:00:00', '222', '作者', "
            " '旧回复文本', 1, 1789873200000, '', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')"
        )
    )
    db.execute(
        text(
            "insert into xueqiu_archiver_utterances (utterance_key, target_user_id, source, "
            " source_id, kind, post_id, post_url, created_at, created_at_ms, author_id, "
            " author_name, text, first_seen_at, last_seen_at) values "
            " ('profile:222:900002', '222', 'profile_timeline', '900002', 'homepage_post', "
            "  '900002', 'https://xueqiu.com/222/900002', '2026-09-20 12:00:00', 1789876800000, "
            "  '222', '作者', '主页帖', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z'),"
            " ('comment:777', '222', 'comment_scan', '777', 'comment_reply', '900001', "
            "  'https://xueqiu.com/111/900001', '2026-09-20 11:00:00', 1789873200000, '222', "
            "  '作者', '旧回复文本', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')"
        )
    )
    db.execute(
        text(
            "insert into xueqiu_archiver_post_scan_state (target_user_id, post_id, last_scanned_at, "
            " last_max_page, last_checked_page_count, last_waf_at) values "
            " ('222', '900001', '2026-09-01T00:00:00Z', 3, 2, '2026-08-01T00:00:00Z')"
        )
    )
    db.commit()


def _count(db, table):
    return db.execute(text(f"select count(*) from {table}")).scalar()


def test_upserts_are_compatible_with_existing_archiver_rows(db):
    _insert_archiver_rows(db)
    store = ArchiverStore(db)

    # 1) 候选帖未补全文的新版本：已补全的正文不被候选摘要覆盖（原冲突语义）
    candidate = CandidatePost(
        post_id="900001",
        url="https://xueqiu.com/111/900001",
        author_id="111",
        author_name="原作者改名",
        text="候选摘要...",
        created_at="2026-09-20 10:00:00",
        created_at_ms=1789869600000,
        source="monitor-retweeted:222",
    )
    merged = store.merge_candidates([candidate])
    assert merged[0].text == "全文正文（archiver 抓的）"  # 取库内已补全版本
    store.upsert_post(candidate, detail_enriched=False)
    store.commit()
    post = db.execute(
        text(
            "select text, author_name, detail_enriched, first_seen_at, last_seen_at "
            "from xueqiu_archiver_posts where post_id='900001'"
        )
    ).one()
    assert post.text == "全文正文（archiver 抓的）"
    assert post.author_name == "原作者改名"
    assert post.detail_enriched is True
    assert post.first_seen_at.year == 2026 and post.first_seen_at.month == 9
    assert post.last_seen_at > post.first_seen_at

    # 2) 同一条评论回复：主键不变，只刷新 like_count/reply_to/post_url，正文不动
    reply = MatchedReply(
        post_id="900001",
        post_url="https://xueqiu.com/111/900001",
        comment_id="777",
        created_at="2026-09-20 11:00:00",
        author_id="222",
        author_name="作者",
        text="新抓到的文本",
        like_count=9,
        created_at_ms=1789873200000,
        reply_to="某人: 原话",
    )
    assert store.upsert_reply("222", reply) is False  # xmax<>0：已存在
    store.upsert_utterance(utterance_from_reply("222", reply, merged[0]))
    store.commit()
    row = db.execute(
        text("select text, like_count, reply_to from xueqiu_archiver_replies where reply_key='777'")
    ).one()
    assert (row.text, row.like_count, row.reply_to) == ("旧回复文本", 9, "某人: 原话")

    # 3) 主页发言：同键覆盖内容、保留 first_seen_at
    status = {
        "id": 900002,
        "user_id": 222,
        "user": {"id": 222, "screen_name": "作者"},
        "created_at": 1789876800000,
        "text": "主页帖（编辑后）",
        "target": "/222/900002",
    }
    store.upsert_utterance(profile_utterance_from_status(status, "222"))
    store.commit()
    utt = db.execute(
        text(
            "select text, created_at, first_seen_at from xueqiu_archiver_utterances "
            "where utterance_key='profile:222:900002'"
        )
    ).one()
    assert utt.text == "主页帖（编辑后）"
    assert utt.created_at == "2026-09-20 12:00:00"  # 业务时区文本与存量逐字一致
    assert utt.first_seen_at.month == 9 and utt.first_seen_at.day == 1

    # 4) 扫描状态：last_waf_at 只进不退
    store.update_scan_state("222", "900001", page_count=1, max_page=5)
    store.commit()
    scan = db.execute(
        text(
            "select last_max_page, last_checked_page_count, last_waf_at "
            "from xueqiu_archiver_post_scan_state where post_id='900001'"
        )
    ).one()
    assert (scan.last_max_page, scan.last_checked_page_count) == (5, 1)
    assert scan.last_waf_at is not None

    # 无重复：每张表的行数与存量一致
    assert _count(db, "xueqiu_archiver_posts") == 1
    assert _count(db, "xueqiu_archiver_replies") == 1
    assert _count(db, "xueqiu_archiver_utterances") == 2
    assert _count(db, "xueqiu_archiver_post_scan_state") == 1


def test_new_rows_insert_and_cooldown(db):
    store = ArchiverStore(db)
    post = CandidatePost(post_id="900010", url="https://xueqiu.com/1/900010", text="t")
    store.merge_candidates([post])
    reply = MatchedReply(
        post_id="900010",
        post_url=post.url,
        comment_id="",
        created_at="x",
        author_id="5",
        author_name="n",
        text="无评论 ID 的回复",
        like_count=0,
        created_at_ms=123,
    )
    assert store.upsert_reply("5", reply) is True
    store.update_scan_state("5", "900010", 1, 1)
    store.commit()
    assert db.execute(text("select reply_key from xueqiu_archiver_replies")).scalar() == (
        "900010|123|5|无评论 ID 的回复"
    )
    assert store.should_skip_recent_scan("5", "900010", cooldown_hours=12) is True
    assert store.should_skip_recent_scan("5", "900010", cooldown_hours=0) is False
    assert store.should_skip_recent_scan("5", "nope", cooldown_hours=12) is False


def test_dry_run_store_writes_nothing(db):
    store = ArchiverStore(db, dry_run=True)
    post = CandidatePost(post_id="900020", url="u")
    store.merge_candidates([post])
    store.upsert_post(post, detail_enriched=True)
    store.update_scan_state("5", "900020", 1, 1)
    store.commit()
    assert _count(db, "xueqiu_archiver_posts") == 0
    assert _count(db, "xueqiu_archiver_post_scan_state") == 0
    assert store.captured["posts"] == [("900020", False), ("900020", True)]
