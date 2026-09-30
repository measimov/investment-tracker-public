"""采集一轮的编排：完整流程写库、再跑一轮的增量跳过、WAF 中止、advisory lock 互斥、
未配置 Cookie 显式降级、dry-run 零写入、scan_runs / 作者行 / 状态行记录。

HTTP 全部打桩（按 URL 路由到真实响应 fixtures），限速与作者间隔用假时钟。
"""

import copy
import json
import random
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from app.database import SessionLocal, engine
from app.models.xueqiu_collector import (
    XueqiuArchiverScanRun,
    XueqiuCollectorAuthor,
    XueqiuCollectorState,
)
from app.services.xueqiu_collector import client as client_mod
from app.services.xueqiu_collector import runner
from app.services.xueqiu_collector import state as st

FIXTURES = Path(__file__).parent / "fixtures" / "xueqiu_collector"
AUTHOR = "1000000001"
OTHER = "1000000002"
# 公开镜像的迁移不播种作者名单：测试自带五位合成作者（私有仓由迁移播种）
SYNTHETIC_AUTHORS = (
    (AUTHOR, "某作者"),
    (OTHER, "示例作者乙"),
    ("1000000003", "示例作者丙"),
    ("1000000004", "示例作者丁"),
    ("1000000005", "示例作者戊"),
)
ARCHIVER_TABLES = (
    "xueqiu_archiver_posts, xueqiu_archiver_replies, xueqiu_archiver_post_scan_state, "
    "xueqiu_archiver_scan_runs, xueqiu_archiver_utterances"
)
TIMELINE = json.loads((FIXTURES / "timeline_user_1000000001_p1.json").read_text("utf-8"))
COMMENTS = json.loads((FIXTURES / "comments_post_61213445_p1.json").read_text("utf-8"))
DETAIL_HTML = (FIXTURES / "detail_7911779762_61213445.html").read_text("utf-8")
WAF_HTML = (FIXTURES / "waf_challenge.html").read_text("utf-8")


def _seed_authors(db):
    for uid, name in SYNTHETIC_AUTHORS:
        db.execute(
            text(
                "INSERT INTO xueqiu_collector_authors (xueqiu_user_id, display_name) "
                "VALUES (:uid, :name) ON CONFLICT (xueqiu_user_id) DO NOTHING"
            ),
            {"uid": uid, "name": name},
        )


def _unseed_authors(db):
    db.execute(
        text("DELETE FROM xueqiu_collector_authors WHERE xueqiu_user_id = ANY(:ids)"),
        {"ids": [uid for uid, _ in SYNTHETIC_AUTHORS]},
    )
    db.commit()


def _reset(db):
    db.execute(text(f"TRUNCATE {ARCHIVER_TABLES} RESTART IDENTITY CASCADE"))
    db.execute(
        text(
            "UPDATE xueqiu_collector_state SET heartbeat_at=NULL, run_requested_at=NULL, "
            "last_cycle_started_at=NULL, last_cycle_finished_at=NULL, last_cycle_status='', "
            "last_cycle_message='', last_waf_at=NULL WHERE id=1"
        )
    )
    db.execute(
        text(
            "UPDATE xueqiu_collector_authors SET last_run_at=NULL, last_status='', last_message=''"
        )
    )
    db.commit()


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(runner.settings, "xueqiu_cookies", '{"xq_a_token": "test"}')
    monkeypatch.setattr(runner.settings, "xueqiu_cookie_file", "")
    monkeypatch.setattr(runner.settings, "xueqiu_collector_push_url", "")
    session = SessionLocal()
    _seed_authors(session)
    _reset(session)
    try:
        yield session
    finally:
        session.rollback()
        _reset(session)
        _unseed_authors(session)
        session.close()


class FakeResponse:
    def __init__(self, body, status_code=200, content_type="application/json"):
        self.text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        self.content = self.text.encode("utf-8")
        self.status_code = status_code
        self.headers = {"content-type": content_type}

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(str(self.status_code))


class NoWaitThrottle(client_mod.PoliteThrottle):
    def wait(self):
        pass

    def mark(self, min_delay, max_delay):
        pass


class FakeSite:
    """按 URL 路由的假雪球：时间线第 1 页 = 真实 fixture（时间戳平移到"现在"），
    第 2 页空；评论只有 410662398（被作者转发/回复的原帖）有内容——取真实评论页 fixture，
    第一条改造成目标作者的回复。"""

    def __init__(self, *, now_ms, waf_on=None, override=None):
        self.calls = []
        self.waf_on = waf_on or (lambda url: False)
        # override(url) -> FakeResponse | None：按 URL 注入失败响应
        self.override = override or (lambda url: None)
        timeline = copy.deepcopy(TIMELINE)
        shift = now_ms - max(int(s["created_at"]) for s in timeline["statuses"]) - 3600_000
        for status in timeline["statuses"]:
            if int(status["created_at"]) > 1_700_000_000_000:  # 置顶 2015 旧帖不平移
                status["created_at"] = int(status["created_at"]) + shift
        self.timeline = timeline
        comments = copy.deepcopy(COMMENTS)
        mine = comments["comments"][0]
        mine["user"] = {"id": int(AUTHOR), "screen_name": "某作者"}
        mine["user_id"] = int(AUTHOR)
        mine["created_at"] = now_ms - 7200_000
        self.comments = comments

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        forced = self.override(url)
        if forced is not None:
            return forced
        if self.waf_on(url):
            return FakeResponse(WAF_HTML, content_type="text/html")
        if "user_timeline.json" in url:
            if "page=1&" in url:
                return FakeResponse(self.timeline)
            return FakeResponse({"statuses": []})
        if "comments.json" in url:
            if "id=410662398&" in url and "page=1&" in url:
                return FakeResponse(self.comments)
            return FakeResponse({"comments": [], "maxPage": 0})
        return FakeResponse(DETAIL_HTML, content_type="text/html")


class RecordingEvent(threading.Event):
    """作者间隔/冷却的"等待"立即返回并记录时长。"""

    def __init__(self):
        super().__init__()
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.is_set()


def _factory(site):
    def build(cookies):
        return client_mod.XueqiuWebClient(cookies, throttle=NoWaitThrottle(), http_get=site)

    return build


def _now_ms():
    return int(st.utcnow().timestamp() * 1000)


def _count(db, table, where=""):
    return db.execute(text(f"select count(*) from {table} {where}")).scalar()


def test_full_cycle_writes_everything_then_second_run_is_incremental(db):
    site = FakeSite(now_ms=_now_ms())
    result = runner.run_authors_cycle(
        db, author_ids=[AUTHOR], client_factory=_factory(site), stop_event=RecordingEvent()
    )
    assert result.status == runner.CYCLE_OK, result.message
    author = result.authors[0]
    assert author.status == "ok"
    # 5 条窗口内发言（置顶旧帖被 30 天窗口挡掉）
    assert author.utterance_count == 5
    assert author.reply_count == 1

    keys = {
        row[0] for row in db.execute(text("select utterance_key from xueqiu_archiver_utterances"))
    }
    assert f"profile:{AUTHOR}:410664844" in keys
    assert "comment:424028717" in keys
    assert _count(db, "xueqiu_archiver_replies") == 1
    assert _count(db, "xueqiu_archiver_posts", "where detail_enriched") == author.candidate_count

    run = db.query(XueqiuArchiverScanRun).one()
    assert (run.status, run.target_user_id, run.author_user_id) == ("ok", AUTHOR, AUTHOR)
    assert run.finished_at is not None and run.waf_hit is False
    assert (run.candidate_count, run.reply_count, run.utterance_count) == (
        author.candidate_count,
        1,
        5,
    )
    db.expire_all()
    state = db.get(XueqiuCollectorState, 1)
    assert state.last_cycle_status == "ok" and state.last_cycle_finished_at is not None
    seeded = db.get(XueqiuCollectorAuthor, AUTHOR)
    assert seeded.last_status == "ok" and seeded.last_run_at is not None

    # 第二轮：帖子已补全文（不再抓全文页）、评论在 12h 冷却内（不再翻评论）
    first_calls = len(site.calls)
    runner.run_authors_cycle(
        db, author_ids=[AUTHOR], client_factory=_factory(site), stop_event=RecordingEvent()
    )
    second = site.calls[first_calls:]
    assert all("user_timeline.json" in url for url in second), second
    assert _count(db, "xueqiu_archiver_utterances") == len(keys)  # 无重复
    assert _count(db, "xueqiu_archiver_scan_runs") == 2


def test_waf_on_timeline_stops_the_cycle(db):
    site = FakeSite(now_ms=_now_ms(), waf_on=lambda url: "user_timeline.json" in url)
    event = RecordingEvent()
    result = runner.run_authors_cycle(
        db,
        author_ids=[AUTHOR, OTHER],
        client_factory=_factory(site),
        stop_event=event,
    )
    assert result.status == runner.CYCLE_WAF
    assert [a.author_id for a in result.authors] == [AUTHOR]  # 第二位作者没跑
    assert len(site.calls) == 1
    run = db.query(XueqiuArchiverScanRun).one()
    assert run.status == "waf" and run.waf_hit is True and run.stopped_early is True
    db.expire_all()
    state = db.get(XueqiuCollectorState, 1)
    assert state.last_cycle_status == "waf" and state.last_waf_at is not None
    due, reason = st.cycle_due(state, st.utcnow(), interval_minutes=0, waf_cooldown_seconds=1800)
    assert (due, reason) == (False, "waf_cooldown")
    # 冷却结束后恢复调度
    due, _ = st.cycle_due(
        state,
        st.utcnow() + timedelta(seconds=1801),
        interval_minutes=30,
        waf_cooldown_seconds=1800,
    )
    assert due is True


def test_waf_on_comments_stops_author_and_keeps_profile_data(db):
    site = FakeSite(now_ms=_now_ms(), waf_on=lambda url: "comments.json" in url)
    result = runner.run_authors_cycle(
        db, author_ids=[AUTHOR], client_factory=_factory(site), stop_event=RecordingEvent()
    )
    assert result.status == runner.CYCLE_WAF
    assert result.authors[0].waf is True
    # 主页发言在评论阶段之前已落库
    assert _count(db, "xueqiu_archiver_utterances") == 5
    comment_calls = [url for url in site.calls if "comments.json" in url]
    assert len(comment_calls) == 1  # max_waf_hits=1：第一次 WAF 即停


def test_author_gap_is_random_within_bounds(db, monkeypatch):
    knobs = runner.CollectorKnobs.from_settings()
    knobs.author_gap_min, knobs.author_gap_max = 180, 600
    site = FakeSite(now_ms=_now_ms())
    event = RecordingEvent()
    result = runner.run_authors_cycle(
        db,
        author_ids=[AUTHOR, OTHER],
        client_factory=_factory(site),
        stop_event=event,
        knobs=knobs,
        rng=random.Random(7),
    )
    assert [a.status for a in result.authors] == ["ok", "ok"]
    total_gap = sum(event.waits)
    assert 180 <= total_gap <= 600
    assert _count(db, "xueqiu_archiver_scan_runs") == 2


def test_stop_signal_interrupts_between_authors(db):
    site = FakeSite(now_ms=_now_ms())
    event = RecordingEvent()
    event.set()
    result = runner.run_authors_cycle(
        db,
        author_ids=[AUTHOR, OTHER],
        client_factory=_factory(site),
        stop_event=event,
    )
    assert result.status == runner.CYCLE_INTERRUPTED


def test_advisory_lock_makes_cycles_mutually_exclusive(db):
    site = FakeSite(now_ms=_now_ms())
    holder = engine.connect()
    try:
        assert holder.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:n))"), {"n": runner.CYCLE_LOCK_NAME}
        ).scalar()
        result = runner.run_authors_cycle(
            db, author_ids=[AUTHOR], client_factory=_factory(site), stop_event=RecordingEvent()
        )
        assert result.status == runner.CYCLE_LOCKED
        assert site.calls == []
        assert _count(db, "xueqiu_archiver_scan_runs") == 0
    finally:
        holder.execute(
            text("SELECT pg_advisory_unlock(hashtext(:n))"), {"n": runner.CYCLE_LOCK_NAME}
        )
        holder.close()
    # 锁释放后可以正常跑（上一轮自己的锁也在 finally 里释放了）
    result = runner.run_authors_cycle(
        db, author_ids=[AUTHOR], client_factory=_factory(site), stop_event=RecordingEvent()
    )
    assert result.status == runner.CYCLE_OK


def test_unconfigured_cookie_degrades_explicitly(db, monkeypatch):
    monkeypatch.setattr(runner.settings, "xueqiu_cookies", "")
    monkeypatch.setattr(runner.settings, "xueqiu_cookie_file", "")
    pushed = []
    monkeypatch.setattr(runner.settings, "xueqiu_collector_push_url", "https://kuma/push/x")
    monkeypatch.setattr(
        runner.cookie_health,
        "push_status",
        lambda url, *, status, message, timeout=10: pushed.append((status, message)),
    )
    site = FakeSite(now_ms=_now_ms())
    result = runner.run_authors_cycle(db, author_ids=[AUTHOR], stop_event=RecordingEvent())
    assert result.status == runner.CYCLE_UNAVAILABLE
    assert "未配置" in result.message
    assert site.calls == []
    assert _count(db, "xueqiu_archiver_scan_runs") == 0
    db.expire_all()
    assert db.get(XueqiuCollectorState, 1).last_cycle_status == "unavailable"
    assert pushed and pushed[0][0] == "down"


def test_dry_run_fetches_but_writes_nothing(db):
    site = FakeSite(now_ms=_now_ms())
    result = runner.run_authors_cycle(
        db,
        author_ids=[AUTHOR],
        dry_run=True,
        client_factory=_factory(site),
        stop_event=RecordingEvent(),
    )
    assert result.status == runner.CYCLE_OK
    assert f"profile:{AUTHOR}:410664844" in result.authors[0].utterance_keys
    assert "comment:424028717" in result.authors[0].utterance_keys
    assert site.calls
    for table in (
        "xueqiu_archiver_utterances",
        "xueqiu_archiver_posts",
        "xueqiu_archiver_scan_runs",
        "xueqiu_archiver_replies",
    ):
        assert _count(db, table) == 0, table
    db.expire_all()
    assert db.get(XueqiuCollectorState, 1).last_cycle_status == ""


def test_pick_authors_round_robin_and_enabled_only(db):
    authors = st.pick_authors(db, 5)
    assert len(authors) == 5
    db.get(XueqiuCollectorAuthor, authors[0]).enabled = False
    db.get(XueqiuCollectorAuthor, authors[1]).last_run_at = st.utcnow()
    db.commit()
    try:
        picked = st.pick_authors(db, 2)
        assert authors[0] not in picked
        assert authors[1] not in picked  # 最近刚跑过的排到后面
    finally:
        db.get(XueqiuCollectorAuthor, authors[0]).enabled = True
        db.commit()


def test_run_request_is_consumed_by_the_next_cycle_start(db):
    state = st.request_run(db)
    assert st.run_request_pending(state) is True
    due, reason = st.cycle_due(state, st.utcnow(), interval_minutes=60, waf_cooldown_seconds=0)
    assert (due, reason) == (True, "requested")
    st.mark_cycle_started(db)
    state = st.get_state(db)
    assert st.run_request_pending(state) is False
    due, reason = st.cycle_due(state, st.utcnow(), interval_minutes=60, waf_cooldown_seconds=0)
    assert (due, reason) == (False, "waiting")


def test_heartbeat_file_and_db(db, tmp_path):
    beat = st.Heartbeat(engine, path=tmp_path / "hb")
    assert st.heartbeat_age_seconds(tmp_path / "hb") is None
    beat.beat(force_db=True)
    assert st.heartbeat_age_seconds(tmp_path / "hb") < 5
    db.expire_all()
    assert db.get(XueqiuCollectorState, 1).heartbeat_at is not None
    disabled = st.Heartbeat(engine, path=tmp_path / "hb2", enabled=False)
    disabled.beat(force_db=True)
    assert not (tmp_path / "hb2").exists()


def test_orphan_running_rows_are_closed_by_the_next_cycle(db):
    """上一个进程被杀：running 行不会永远挂着，下一轮持锁后标成 interrupted。"""
    st.start_scan_run(db, OTHER)
    site = FakeSite(now_ms=_now_ms())
    runner.run_authors_cycle(
        db, author_ids=[AUTHOR], client_factory=_factory(site), stop_event=RecordingEvent()
    )
    rows = {run.author_user_id: run.status for run in db.query(XueqiuArchiverScanRun).all()}
    assert rows == {OTHER: "interrupted", AUTHOR: "ok"}


# --------------------------------------------------------------------------- #
# PR #236 评审 P2：失败响应不能被当成合法空页（否则整轮记成功、刷新观点页活性）
# --------------------------------------------------------------------------- #
from app.services import xueqiu_opinion_source as opinion_src  # noqa: E402

ERROR_HTML = "<!doctype html><html><body>系统繁忙，请稍后再试</body></html>"


def _html(status_code=200):
    return FakeResponse(ERROR_HTML, status_code=status_code, content_type="text/html")


def _run(db, site, authors=(AUTHOR,)):
    return runner.run_authors_cycle(
        db, author_ids=list(authors), client_factory=_factory(site), stop_event=RecordingEvent()
    )


def _scan_run(db):
    db.expire_all()
    return db.query(XueqiuArchiverScanRun).order_by(XueqiuArchiverScanRun.run_id.desc()).first()


def _seed_old_ok_run(db, hours_ago=100):
    finished = st.utcnow() - timedelta(hours=hours_ago)
    db.execute(
        text(
            "INSERT INTO xueqiu_archiver_scan_runs (target_user_id, author_user_id, status, "
            "finished_at) VALUES ('1', '1', 'ok', :finished)"
        ),
        {"finished": finished},
    )
    db.commit()
    return finished


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(lambda: _html(200), id="http200-html"),
        pytest.param(
            lambda: FakeResponse(
                {"error_code": "400016", "error_description": "遇到错误，请刷新页面"}
            ),
            id="json-error-object",
        ),
        pytest.param(lambda: FakeResponse([]), id="json-array"),
        pytest.param(lambda: FakeResponse({"statuses": None}), id="statuses-null"),
        pytest.param(lambda: _html(502), id="http502"),
    ],
)
def test_first_timeline_page_failure_is_an_error_not_success(db, response):
    site = FakeSite(
        now_ms=_now_ms(),
        override=lambda url: response() if "user_timeline.json" in url else None,
    )
    result = _run(db, site)
    author = result.authors[0]
    assert author.status == "error", author
    assert author.error
    assert result.status == runner.CYCLE_FAILED
    run = _scan_run(db)
    assert run.status == "error" and run.error_message
    assert _count(db, "xueqiu_archiver_utterances") == 0
    # 从未接入的数据源不能因为一轮失败被标成 available
    assert opinion_src.is_opinion_source_available(db) is False
    assert opinion_src.source_freshness(db)["available"] is False
    db.expire_all()
    assert db.get(XueqiuCollectorAuthor, AUTHOR).last_status == "error"


def test_error_json_message_is_kept_in_scan_run(db):
    site = FakeSite(
        now_ms=_now_ms(),
        override=lambda url: (
            FakeResponse({"error_code": "400016"}) if "user_timeline.json" in url else None
        ),
    )
    _run(db, site)
    assert "400016" in _scan_run(db).error_message


def test_failed_run_does_not_refresh_liveness(db):
    old_ok = _seed_old_ok_run(db)
    site = FakeSite(
        now_ms=_now_ms(), override=lambda url: _html() if "user_timeline.json" in url else None
    )
    _run(db, site)
    fresh = opinion_src.source_freshness(db)
    assert fresh["available"] is True  # 之前成功过
    assert opinion_src.latest_successful_scan_at(db) == old_ok
    assert fresh["stale"] is True  # 100h 前的成功，失败轮次不续命


def test_first_page_valid_empty_is_a_quiet_author(db):
    """只有合法的 statuses=[] 才是「作者近期无动态」：记 ok，并证明采集在流动。"""
    site = FakeSite(
        now_ms=_now_ms(),
        override=lambda url: (
            FakeResponse({"statuses": [], "maxPage": 0}) if "user_timeline.json" in url else None
        ),
    )
    result = _run(db, site)
    author = result.authors[0]
    assert (author.status, author.candidate_count, author.utterance_count) == ("ok", 0, 0)
    assert _scan_run(db).status == "ok"
    assert opinion_src.is_opinion_source_available(db) is True
    assert opinion_src.source_freshness(db)["stale"] is False


def test_second_timeline_page_failure_keeps_first_page_as_partial(db):
    site = FakeSite(
        now_ms=_now_ms(),
        override=lambda url: _html() if "user_timeline.json" in url and "page=2&" in url else None,
    )
    result = _run(db, site)
    author = result.authors[0]
    assert author.status == "partial"
    assert "第 2 页" in author.error
    assert author.utterance_count == 5  # 首页照常入库
    assert _count(db, "xueqiu_archiver_utterances", "where source = 'profile_timeline'") == 5
    run = _scan_run(db)
    assert run.status == "partial" and "第 2 页" in run.error_message
    assert result.status == runner.CYCLE_PARTIAL


def test_comment_page_failure_marks_post_for_rescan(db):
    """评论页中途失败：不当作「评论翻完了」，帖子记失败、清掉扫描时间，下一轮重扫。"""
    target_post = "410662398"

    def override(url):
        if "comments.json" in url and f"id={target_post}&" in url:
            return _html()
        return None

    site = FakeSite(now_ms=_now_ms(), override=override)
    result = _run(db, site)
    author = result.authors[0]
    assert author.status == "partial"
    assert target_post in author.error
    row = db.execute(
        text("select last_scanned_at from xueqiu_archiver_post_scan_state where post_id = :p"),
        {"p": target_post},
    ).first()
    assert row is None or row[0] is None

    # 下一轮：其他帖在冷却期内跳过，失败的那帖重扫（这次成功）
    site.override = lambda url: None
    before = len(site.calls)
    second = _run(db, site)
    comment_calls = [u for u in site.calls[before:] if "comments.json" in u]
    assert comment_calls and all(f"id={target_post}&" in u for u in comment_calls)
    assert second.authors[0].status == "ok"
    assert second.authors[0].reply_count == 1


def test_comment_page_failure_on_second_page(db):
    """第 2 页失败：第 1 页的命中已提交，帖子仍记失败。"""
    target_post = "410662398"

    def override(url):
        if "comments.json" in url and f"id={target_post}&" in url and "page=2&" in url:
            return _html()
        return None

    site = FakeSite(now_ms=_now_ms(), override=override)
    # 让第 1 页看起来还有下一页
    site.comments["maxPage"] = 3
    result = _run(db, site)
    assert result.authors[0].status == "partial"
    assert _count(db, "xueqiu_archiver_replies") == 1  # 第 1 页的命中没丢


def test_detail_failure_is_not_marked_enriched(db):
    site = FakeSite(
        now_ms=_now_ms(),
        override=lambda url: (
            _html(500) if "user_timeline.json" not in url and "comments.json" not in url else None
        ),
    )
    result = _run(db, site)
    assert result.authors[0].status == "partial"
    assert "全文" in result.authors[0].error
    assert _count(db, "xueqiu_archiver_posts", "where detail_enriched") == 0

    site.override = lambda url: None
    before = len(site.calls)
    _run(db, site)
    detail_calls = [
        u for u in site.calls[before:] if "user_timeline.json" not in u and "comments.json" not in u
    ]
    assert detail_calls  # 下一轮重抓全文
    assert _count(db, "xueqiu_archiver_posts", "where not detail_enriched") == 0


def test_max_posts_shadow_run_is_low_cost_and_writes_nothing(db):
    """影子运行（--dry-run --max-posts N）：只翻主页第 1 页、只处理最新 N 个候选帖、
    评论每帖 1 页，且零写入——验证签名/解析/键形态，不对整位作者跑一轮。"""
    site = FakeSite(now_ms=_now_ms())
    knobs = runner.CollectorKnobs.from_settings()
    knobs.max_posts = 1
    knobs.max_comment_pages = 1
    result = runner.run_authors_cycle(
        db,
        author_ids=[AUTHOR],
        dry_run=True,
        knobs=knobs,
        client_factory=_factory(site),
        stop_event=RecordingEvent(),
    )
    author = result.authors[0]
    assert author.candidate_count == 1
    timeline_calls = [url for url in site.calls if "user_timeline.json" in url]
    assert len(timeline_calls) == 1 and "page=1&" in timeline_calls[0]
    assert sum("comments.json" in url for url in site.calls) <= 1
    assert _count(db, "xueqiu_archiver_utterances") == 0
    assert _count(db, "xueqiu_archiver_scan_runs") == 0
