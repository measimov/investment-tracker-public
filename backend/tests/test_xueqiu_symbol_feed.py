"""按标的监控（公告/讨论、组合调仓）：解析、幂等 upsert、标的范围、每日一轮编排、
调度判据与 API。市场热帖的采集与展示已于 2026-09-28 下线：这里只守住「不再请求、
不再展示、旧待重试项静默丢弃」，以及旧 Markdown 热帖快照导入仍写 `xueqiu_hot_posts`。

fixture 取自原 monitor_symbols 对真实响应的 Markdown 导出（ids/时间/正文/计数为真实值），
结构按 PR-1 真实 status 对象对齐——见各 JSON 的 `_note`。HTTP 全部打桩、限速用空节流。
"""

import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import xueqiu_collector as api_module
from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.holding import Holding
from app.models.security_rule import SecurityRule
from app.models.user import User
from app.models.watchlist_item import WatchlistItem
from app.models.xueqiu_collector import (
    XueqiuCollectorCube,
    XueqiuCubeRebalancing,
    XueqiuHotPost,
    XueqiuSymbolPost,
)
from app.services.xueqiu_collector import client as client_mod
from app.services.xueqiu_collector.client import CollectorFetchError
from app.services.xueqiu_collector import feed_parsing as fp
from app.services.xueqiu_collector import feed_store
from app.services.xueqiu_collector import runner
from app.services.xueqiu_collector import state as st
from app.services.xueqiu_collector import symbols

from .helpers import reset_tables

FIXTURES = Path(__file__).parent / "fixtures" / "xueqiu_collector"
ANNOUNCEMENTS = json.loads((FIXTURES / "symbol_announcement_SH600519.json").read_text("utf-8"))
DISCUSSIONS = json.loads((FIXTURES / "symbol_discussion_SH600519.json").read_text("utf-8"))
HOTS = json.loads((FIXTURES / "hots_day.json").read_text("utf-8"))
CUBE = json.loads((FIXTURES / "cube_rebalancing_ZH000001.json").read_text("utf-8"))
WAF_HTML = (FIXTURES / "waf_challenge.html").read_text("utf-8")

# 雪球 symbol 形态：A/B 股交易所前缀 + 6 位、HK 前缀——一个都不许出现在身份键列里
XUEQIU_SYMBOL_RE = re.compile(r"^(SH|SZ|BJ)\d{6}$|^HK\d+$")
FEED_TABLES = (
    "xueqiu_symbol_posts, xueqiu_hot_posts, xueqiu_cube_rebalancing, xueqiu_collector_cubes"
)


def _reset(db):
    db.execute(text(f"TRUNCATE {FEED_TABLES} RESTART IDENTITY"))
    db.execute(text("TRUNCATE xueqiu_archiver_scan_runs RESTART IDENTITY"))
    db.execute(text(
        "UPDATE xueqiu_collector_state SET symbols_run_requested_at=NULL, "
        "symbols_last_started_at=NULL, symbols_last_finished_at=NULL, symbols_last_status='', "
        "symbols_last_message='', symbols_last_business_date=NULL, symbols_last_stats='{}', "
        "symbols_pending=NULL, last_waf_at=NULL, run_requested_at=NULL WHERE id=1"
    ))
    db.commit()
    reset_tables(db, [SecurityRule, WatchlistItem, Holding])


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(symbols.settings, "xueqiu_cookies", '{"xq_a_token": "test"}')
    monkeypatch.setattr(symbols.settings, "xueqiu_cookie_file", "")
    session = SessionLocal()
    _reset(session)
    try:
        yield session
    finally:
        session.rollback()
        _reset(session)
        session.close()


def _hold(db, symbol, market, user_id=1, quantity="100"):
    db.add(Holding(
        user_id=user_id, symbol=symbol, name=symbol, market=market,
        quantity=Decimal(quantity), avg_cost=Decimal("10"), total_cost=Decimal("1000"),
        currency="CNY",
    ))
    db.commit()


def _watch(db, symbol, market, user_id=1):
    db.add(WatchlistItem(user_id=user_id, symbol=symbol, market=market))
    db.commit()


# --------------------------------------------------------------------------- #
# 解析（纯函数）
# --------------------------------------------------------------------------- #
def test_request_urls_match_original_parameters():
    assert fp.announcement_url("SH600519", 20) == (
        "https://xueqiu.com/statuses/stock_timeline.json?symbol_id=SH600519&count=20"
        "&source=%E5%85%AC%E5%91%8A"
    )
    assert fp.discussion_url("00700", 20) == (
        "https://xueqiu.com/query/v1/symbol/search/status?count=20&comment=0&symbol=00700"
        "&hl=0&source=all&sort=&page=1"
    )
    assert fp.rebalancing_url("ZH000001", 20).endswith("cube_symbol=ZH000001&count=20&page=1")
    with pytest.raises(ValueError):
        fp.feed_url("hots", "SH600519", 20)
    # 市场热帖端点已下线：请求构造与端点类型都不再存在
    assert not hasattr(fp, "hots_url") and not hasattr(fp, "ENDPOINT_HOTS")
    with pytest.raises(ValueError, match="未知的端点类型"):
        fp.validate_feed_payload([], "hots")


def test_validate_accepts_known_structures_including_empty():
    assert len(fp.validate_feed_payload(DISCUSSIONS, fp.KIND_DISCUSSION)) == 4
    assert len(fp.validate_feed_payload(CUBE, fp.ENDPOINT_REBALANCING)) == 3
    # 只有这些已知的空结构才是「成功的空结果」
    assert fp.validate_feed_payload({"list": []}, fp.KIND_ANNOUNCEMENT) == []
    assert fp.validate_feed_payload({"count": 0, "statuses": []}, fp.KIND_DISCUSSION) == []
    assert fp.validate_feed_payload({"list": [], "maxPage": 0}, fp.ENDPOINT_REBALANCING) == []


@pytest.mark.parametrize("payload, endpoint, reason", [
    # 评审复现：错误对象不能被当成成功空列表
    ({"error_code": "AUTH_FAILED", "error_description": "unauthorized"},
     "announcement", "AUTH_FAILED"),
    ({"error_code": 400016, "error_description": "遇到错误，请刷新页面或者重新登录帐号后再试"},
     "discussion", "400016"),
    ({"error_description": "登录后查看"}, "announcement", "登录后查看"),
    ({"success": False, "message": "cookie expired"}, "rebalancing", "success=false"),
    (None, "announcement", "JSON null"),
    ({"data": {"items": []}}, "discussion", "缺少列表字段"),
    ({}, "discussion", "缺少列表字段"),
    ({"list": None}, "announcement", "缺少 list 数组"),
    ({"statuses": []}, "rebalancing", "缺少列表字段"),  # 调仓只认 list
    ([], "discussion", "顶层是数组"),  # 唯一允许顶层数组的热帖端点已下线
    ([{"id": 1}], "rebalancing", "顶层是数组"),
    ({"list": [{"id": 1}, "junk"]}, "announcement", "非对象元素"),
    ("oops", "discussion", "不是 JSON 对象"),
])
def test_validate_rejects_error_objects_and_unknown_structures(payload, endpoint, reason):
    with pytest.raises(CollectorFetchError, match=reason):
        fp.validate_feed_payload(payload, endpoint)
    # 解析函数吃原始响应对象时走同一套严格校验（条目列表视为已校验，不在此列）
    if isinstance(payload, list):
        return
    with pytest.raises(CollectorFetchError):
        fp.parse_statuses(payload, endpoint) if endpoint != "rebalancing" else fp.parse_rebalancings(
            payload
        )


def test_parse_announcements_keeps_offsite_attachment_link():
    posts = fp.parse_statuses(ANNOUNCEMENTS)
    assert [post.post_id for post in posts] == ["405111930", "405111926", "405111922"]
    first = posts[0]
    assert first.text == "贵州茅台：贵州茅台关于召开2026年半年度业绩说明会的公告 网页链接"
    assert first.payload["links"] == ["https://notice.example.invalid/405111930.PDF"]
    assert first.payload["reply_count"] == 42 and first.payload["fav_count"] == 3
    # 2026-08-14 20:46:28 +08:00
    assert first.created_at_ms == int(
        datetime(2026, 8, 14, 20, 46, 28, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000
    )
    # 公告不带作者：保留 target 原链接（能打开），不是身份键
    assert first.author_id == "" and first.url == "https://xueqiu.com/S/SH600519/405111930"


def test_parse_discussion_drops_cashtag_links_and_builds_user_url():
    posts = fp.parse_statuses(DISCUSSIONS)
    assert len(posts) == 4
    first = posts[0]
    assert first.url == "https://xueqiu.com/9731668124/410650580"
    assert first.author_id == "9731668124" and first.author_name == "fixture_user_8124"
    assert first.text.startswith("$贵州茅台(SH600519)$ 茅台机场")  # 正文原样（纯文本）
    assert "links" not in first.payload  # cashtag 是站内 /S/ 链接，不进附件
    assert "五粮液(SZ000858)" in posts[1].text


def test_status_url_rewrites_symbol_target_when_author_known():
    item = {"id": 5, "user_id": 42, "target": "/S/SH600519/5"}
    assert fp.parse_status(item).url == "https://xueqiu.com/42/5"
    assert fp.parse_status({"id": None}) is None


def test_extract_links_filters_site_links_and_images():
    html = (
        '<a href="https://xueqiu.com/S/SH600519">$茅台$</a>'
        '<a href="https://xueqiu.com/n/someone">@someone</a>'
        '<a href="//xqimg.imedao.com/a.jpg" class="co-img-link">查看图片</a>'
        '<a href="http://static.cninfo.com.cn/x.PDF">网页链接</a>'
        '<a href="http://static.cninfo.com.cn/x.PDF">重复</a>'
    )
    assert fp.extract_links(html) == ["http://static.cninfo.com.cn/x.PDF"]


def test_parse_statuses_accepts_prevalidated_item_list():
    """已校验的条目列表直接解析（fixture 取自原热帖响应的顶层 list，status 对象同构）。"""
    posts = fp.parse_statuses(HOTS)
    assert [post.post_id for post in posts] == ["410628734", "410622571", "410628809", "410633103"]
    assert posts[0].payload["hot"] is False and posts[0].payload["fav_count"] == 58


@pytest.mark.parametrize("xueqiu, expected", [
    ("SH600519", ("600519", "A股")),
    ("SZ000858", ("000858", "A股")),
    ("BJ920001", ("920001", "A股")),
    ("SH900901", ("900901", "B股")),
    ("SZ200596", ("200596", "B股")),
    ("00700", ("00700", "港股")),
    ("PDD", ("PDD", "美股")),
    ("BRK.B", ("BRK.B", "美股")),
    ("", None),
    ("600519", None),
])
def test_from_xueqiu(xueqiu, expected):
    assert fp.from_xueqiu(xueqiu) == expected


def test_parse_rebalancing_replaces_xueqiu_symbols():
    records = fp.parse_rebalancings(CUBE)
    assert len(records) == 3
    history = records[0].payload["histories"][0]
    assert history == {
        "stock_name": "中国平安", "symbol": "601318", "market": "A股",
        "prev_weight": 13.01, "target_weight": 14.65, "prev_weight_adjusted": 13.01,
        "weight": 13.01,
    }
    assert "SH601318" not in json.dumps(records[0].payload, ensure_ascii=False)
    assert records[0].payload["exe_strategy"] == "market_all"


# --------------------------------------------------------------------------- #
# 幂等 upsert
# --------------------------------------------------------------------------- #
def test_symbol_post_upsert_is_idempotent(db):
    posts = fp.parse_statuses(DISCUSSIONS)
    assert feed_store.upsert_symbol_posts(db, "600519", "A股", "discussion", posts) == (4, 0)
    db.commit()
    first_seen = {
        row.post_id: row.first_seen_at for row in db.query(XueqiuSymbolPost).all()
    }
    assert feed_store.upsert_symbol_posts(db, "600519", "A股", "discussion", posts) == (0, 4)
    db.commit()
    rows = db.query(XueqiuSymbolPost).all()
    assert len(rows) == 4
    assert {row.post_id: row.first_seen_at for row in rows} == first_seen

    # 空正文不抹掉旧内容；互动计数跟着最新值走
    blank = [fp.FeedPost(post_id=posts[0].post_id, created_at_ms=posts[0].created_at_ms,
                         payload={"reply_count": 9})]
    feed_store.upsert_symbol_posts(db, "600519", "A股", "discussion", blank)
    db.commit()
    db.expire_all()
    row = db.query(XueqiuSymbolPost).filter_by(post_id=posts[0].post_id).one()
    assert row.text == posts[0].text and row.url == posts[0].url
    assert row.payload == {"reply_count": 9}


def test_same_post_under_two_symbols_is_kept_for_both(db):
    """同一帖提及两只被跟踪的标的：两只的讨论流各有一行，不来回改归属。"""
    post = fp.parse_statuses(DISCUSSIONS)[1]  # 同时提及茅台与五粮液
    feed_store.upsert_symbol_posts(db, "600519", "A股", "discussion", [post])
    feed_store.upsert_symbol_posts(db, "000858", "A股", "discussion", [post])
    db.commit()
    owners = {(r.symbol, r.market) for r in db.query(XueqiuSymbolPost).filter_by(post_id=post.post_id)}
    assert owners == {("600519", "A股"), ("000858", "A股")}


def test_hot_posts_upsert_refreshes_rank_and_snapshot(db):
    """热帖表只剩旧 Markdown 导入在写（采集与展示已下线），upsert 语义保持不变。"""
    posts = fp.parse_statuses(HOTS)
    first = datetime(2026, 9, 26, 23, 30, tzinfo=timezone.utc)
    feed_store.upsert_hot_posts(db, "day", posts, first)
    db.commit()
    second = first + timedelta(days=1)
    reordered = [posts[2], posts[0]]
    assert feed_store.upsert_hot_posts(db, "day", reordered, second) == (0, 2)
    db.commit()
    assert db.query(XueqiuHotPost).count() == 4
    latest = (
        db.query(XueqiuHotPost)
        .filter_by(scope="day", snapshot_at=second)
        .order_by(XueqiuHotPost.rank)
        .all()
    )
    assert [(row.rank, row.post_id) for row in latest] == [
        (1, posts[2].post_id), (2, posts[0].post_id),
    ]


def test_rebalancing_upsert_is_idempotent(db):
    records = fp.parse_rebalancings(CUBE)
    assert feed_store.upsert_rebalancing(db, "ZH000001", records) == (3, 0)
    assert feed_store.upsert_rebalancing(db, "ZH000001", records) == (0, 3)
    db.commit()
    assert db.query(XueqiuCubeRebalancing).count() == 3


# --------------------------------------------------------------------------- #
# 标的范围
# --------------------------------------------------------------------------- #
def test_universe_unions_users_and_applies_rules(db):
    _hold(db, "600519", "A股", user_id=1)
    _hold(db, "00700", "港股", user_id=1)
    _hold(db, "511990", "A股", user_id=1)  # 现金管理
    _hold(db, "000001", "A股", user_id=1)  # 排除
    _hold(db, "AAPL", "美股", user_id=1, quantity="0")  # 已清仓
    _hold(db, "000001", "场外开基", user_id=1)  # 市场不支持
    _watch(db, "900901", "B股", user_id=2)
    _watch(db, "600519", "A股", user_id=2)  # 与 user1 持仓重复
    _hold(db, "000001", "A股", user_id=2)  # user1 排除但 user2 仍持有 → 照采
    db.add(SecurityRule(user_id=1, rule_type="EXCLUDE", symbol="000001", market="A股", payload={}))
    db.add(SecurityRule(
        user_id=1, rule_type="CASH_MANAGEMENT", symbol="511990", market="A股", payload={},
    ))
    db.commit()

    targets = symbols.compute_symbol_universe(db)
    pairs = [(t.symbol, t.market) for t in targets]
    assert sorted(pairs) == sorted([
        ("600519", "A股"), ("000001", "A股"), ("00700", "港股"), ("900901", "B股"),
    ])
    assert pairs[:3] == [("000001", "A股"), ("900901", "B股"), ("00700", "港股")]  # 市场轮转
    mapping = {(t.symbol, t.market): t.xueqiu for t in targets}
    assert mapping[("600519", "A股")] == "SH600519"
    assert mapping[("900901", "B股")] == "SH900901"
    assert mapping[("00700", "港股")] == "00700"


def test_universe_excludes_rule_for_single_holder(db):
    _hold(db, "000001", "A股", user_id=1)
    db.add(SecurityRule(user_id=1, rule_type="EXCLUDE", symbol="000001", market="A股", payload={}))
    db.commit()
    assert symbols.compute_symbol_universe(db) == []


def test_explicit_targets_normalize_and_validate():
    targets = symbols.explicit_targets(["700", " pdd "], "港股")
    assert [(t.symbol, t.xueqiu) for t in targets][0] == ("00700", "00700")
    with pytest.raises(ValueError):
        symbols.explicit_targets(["000001"], "场外开基")


# --------------------------------------------------------------------------- #
# 每日一轮
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, body, status_code=200, content_type="application/json"):
        self.text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        self.status_code = status_code
        self.headers = {"content-type": content_type}

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code} Server Error")


class NoWaitThrottle(client_mod.PoliteThrottle):
    def wait(self):
        pass

    def mark(self, min_delay, max_delay):
        pass


class FakeSite:
    """按 URL 路由：茅台 = 真实 fixture；五粮液讨论流 500（单项失败）；其余空列表。"""

    def __init__(self, waf_on=None, overrides=None, wuliangye_ok=False):
        self.calls = []
        self.waf_on = waf_on or (lambda url: False)
        # [(url 子串, FakeResponse)]：命中即返回（注入错误对象/未知结构等）
        self.overrides = overrides or []
        self.wuliangye_ok = wuliangye_ok

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        if self.waf_on(url):
            return FakeResponse(WAF_HTML, content_type="text/html")
        for needle, response in self.overrides:
            if needle in url:
                return response
        if self.wuliangye_ok and "&symbol=SZ000858" in url:
            return FakeResponse({"count": 0, "list": []})
        if "stock_timeline.json" in url:
            if "symbol_id=SH600519" in url:
                return FakeResponse(ANNOUNCEMENTS)
            return FakeResponse({"count": 0, "list": []})
        if "symbol/search/status" in url:
            if "&symbol=SH600519" in url:
                return FakeResponse(DISCUSSIONS)
            if "&symbol=SZ000858" in url:
                return FakeResponse("upstream error", status_code=500, content_type="text/plain")
            return FakeResponse({"list": []})
        if "cubes/rebalancing" in url:
            return FakeResponse(CUBE)
        return FakeResponse({"error": "unknown"}, status_code=404)


def _factory(site):
    return lambda cookies: client_mod.XueqiuWebClient(
        cookies, throttle=NoWaitThrottle(), http_get=site, sleep=lambda s: None
    )


def _seed_universe(db):
    _hold(db, "600519", "A股")
    _watch(db, "000858", "A股", user_id=2)
    db.add(XueqiuCollectorCube(cube_id="ZH000001", display_name="样例组合"))
    db.commit()


def test_cycle_writes_our_keys_only_and_is_idempotent(db):
    _seed_universe(db)
    site = FakeSite()
    result = symbols.run_symbols_cycle(
        db, client_factory=_factory(site), business_date=date(2026, 9, 27)
    )
    # 五粮液讨论流 500：单项失败继续，其余照常 → partial
    assert result.status == runner.CYCLE_PARTIAL
    assert len(result.failures) == 1 and "000858" in result.failures[0]
    assert result.stats["announcement"] == {"fetched": 3, "new": 3}
    assert result.stats["discussion"] == {"fetched": 4, "new": 4}
    assert result.stats["cubes"] == 1 and result.stats["rebalancing_new"] == 3
    # 市场热帖已下线：不再请求、不再统计、不再写表
    assert "hots" not in result.stats and "热帖" not in result.message
    assert len(site.calls) == 2 * 2 + 1  # 两只 × 两类 + 组合
    assert not any("hots" in url for url in site.calls)
    assert db.query(XueqiuHotPost).count() == 0

    symbols_in_db = {
        row[0] for row in db.execute(text(
            "SELECT symbol FROM xueqiu_symbol_posts UNION SELECT market FROM xueqiu_symbol_posts"
        ))
    }
    assert symbols_in_db == {"600519", "A股"}
    for (value,) in db.execute(text("SELECT symbol FROM xueqiu_symbol_posts")):
        assert not XUEQIU_SYMBOL_RE.match(value)
    payloads = " ".join(
        json.dumps(row[0], ensure_ascii=False)
        for row in db.execute(text(
            "SELECT payload FROM xueqiu_symbol_posts UNION ALL "
            "SELECT payload FROM xueqiu_cube_rebalancing"
        ))
    )
    assert not re.search(r"\b(SH|SZ)\d{6}\b", payloads)

    state = st.get_state(db)
    assert state.symbols_last_status == runner.CYCLE_PARTIAL
    # 有失败：业务日不记已跑，只把失败项留作当天待重试
    assert state.symbols_last_business_date is None
    assert state.symbols_pending == {
        "date": "2026-09-27", "attempts": 1,
        "items": [{"type": "feed", "symbol": "000858", "market": "A股", "kind": "discussion"}],
    }
    assert "分钟后重试未成功的 1 项" in state.symbols_last_message
    assert state.symbols_last_stats["failures"] == 1
    cube = db.get(XueqiuCollectorCube, "ZH000001")
    assert cube.last_status == "ok" and cube.last_run_at is not None
    # 按标的采集不写 scan_runs：观点页活性判据只看作者轮次
    assert db.execute(text("SELECT count(*) FROM xueqiu_archiver_scan_runs")).scalar() == 0

    again = symbols.run_symbols_cycle(
        db, client_factory=_factory(FakeSite(wuliangye_ok=True)), business_date=date(2026, 9, 27)
    )
    assert again.status == runner.CYCLE_OK
    assert again.stats["announcement"]["new"] == 0 and again.stats["discussion"]["new"] == 0
    assert again.stats["rebalancing_new"] == 0
    assert db.query(XueqiuSymbolPost).count() == 7
    assert db.query(XueqiuHotPost).count() == 0
    state = st.get_state(db)
    assert state.symbols_last_business_date == date(2026, 9, 27)
    assert state.symbols_pending is None


def _seed_one(db):
    _hold(db, "600519", "A股")


@pytest.mark.parametrize("response, reason", [
    # 评审复现：HTTP 200 的错误对象此前被当成成功空列表、当天标为已跑
    (FakeResponse({"error_code": "AUTH_FAILED", "error_description": "unauthorized"}),
     "AUTH_FAILED"),
    (FakeResponse({"success": False, "message": "cookie expired"}), "success=false"),
    (FakeResponse({"data": {"whatever": []}}), "缺少列表字段"),
    (FakeResponse("null"), "JSON null"),
    (FakeResponse("<html><body>登录</body></html>", content_type="text/html"), "不是 JSON"),
])
def test_error_or_unknown_response_is_a_failure_not_empty(db, response, reason):
    _seed_one(db)
    site = FakeSite(overrides=[("stock_timeline.json", response)])
    result = symbols.run_symbols_cycle(
        db, client_factory=_factory(site), business_date=date(2026, 9, 27)
    )
    assert result.status == runner.CYCLE_PARTIAL  # 讨论成功，公告失败
    assert len(result.failures) == 1 and reason in result.failures[0]
    assert "CollectorFetchError" in result.failures[0]
    state = st.get_state(db)
    assert state.symbols_last_business_date is None  # 当天仍会重试
    assert state.symbols_pending["items"] == [
        {"type": "feed", "symbol": "600519", "market": "A股", "kind": "announcement"}
    ]
    assert db.query(XueqiuSymbolPost).filter_by(kind="announcement").count() == 0


def test_all_items_failing_is_failed_and_retried(db):
    _seed_one(db)
    bad = FakeResponse({"error_code": "AUTH_FAILED", "error_description": "unauthorized"})
    site = FakeSite(overrides=[("xueqiu.com/", bad)])
    result = symbols.run_symbols_cycle(
        db, client_factory=_factory(site), business_date=date(2026, 9, 27)
    )
    assert result.status == runner.CYCLE_FAILED
    assert len(result.failures) == 2  # 公告/讨论
    state = st.get_state(db)
    assert state.symbols_last_business_date is None
    assert len(state.symbols_pending["items"]) == 2


def test_legit_empty_lists_are_success(db):
    _seed_one(db)
    site = FakeSite(overrides=[
        ("stock_timeline.json", FakeResponse({"count": 0, "list": []})),
        ("symbol/search/status", FakeResponse({"count": 0, "statuses": []})),
    ])
    result = symbols.run_symbols_cycle(
        db, client_factory=_factory(site), business_date=date(2026, 9, 27)
    )
    assert result.status == runner.CYCLE_OK and result.failures == []
    state = st.get_state(db)
    assert state.symbols_last_business_date == date(2026, 9, 27)
    assert state.symbols_pending is None


def test_retry_runs_only_failed_items_after_interval(db, monkeypatch):
    """首轮部分失败 → 间隔内不重试 → 到点只重试失败项 → 成功后当天记已跑。"""
    monkeypatch.setattr(symbols.settings, "xueqiu_collector_symbols_run_after", "00:00")
    monkeypatch.setattr(symbols.settings, "xueqiu_collector_symbols_retry_minutes", 60)
    _seed_universe(db)
    first = symbols.maybe_run_symbols_cycle(db, client_factory=_factory(FakeSite()))
    assert first is not None and first.status == runner.CYCLE_PARTIAL

    assert symbols.maybe_run_symbols_cycle(db, client_factory=_factory(FakeSite())) is None
    state = st.get_state(db)
    assert st.symbols_cycle_due(
        state, st.utcnow(), run_after=st.parse_run_after("00:00"), waf_cooldown_seconds=1800,
        retry_minutes=60,
    ) == (False, "retry_wait")

    db.execute(text(
        "UPDATE xueqiu_collector_state SET symbols_last_finished_at = now() - interval '2 hours'"
    ))
    db.commit()
    site = FakeSite(wuliangye_ok=True)
    retry = symbols.maybe_run_symbols_cycle(db, client_factory=_factory(site))
    assert retry is not None and retry.status == runner.CYCLE_OK
    assert len(site.calls) == 1 and "&symbol=SZ000858" in site.calls[0]  # 只重试失败项
    assert retry.stats["mode"] == "retry" and retry.stats["attempt"] == 2
    state = st.get_state(db)
    assert state.symbols_pending is None
    assert state.symbols_last_business_date is not None
    assert symbols.maybe_run_symbols_cycle(db, client_factory=_factory(FakeSite())) is None


def _store_todays_pending(db, items, attempts=1):
    """写一条当天的待重试记录，并把上一轮结束时间推到重试间隔之前。"""
    db.execute(
        text(
            "UPDATE xueqiu_collector_state SET symbols_pending = CAST(:p AS jsonb), "
            "symbols_last_finished_at = now() - interval '2 hours' WHERE id=1"
        ),
        {"p": json.dumps({
            "date": symbols.local_today().isoformat(), "attempts": attempts, "items": items,
        })},
    )
    db.commit()


def test_retry_drops_legacy_hots_items_silently(db, monkeypatch):
    """下线前写下的待重试记录里有热帖项：重试时静默丢弃——不请求、不算失败、
    不写回待重试记录，其余项照常重试。"""
    monkeypatch.setattr(symbols.settings, "xueqiu_collector_symbols_run_after", "00:00")
    _seed_universe(db)
    _store_todays_pending(db, [
        {"type": "feed", "symbol": "000858", "market": "A股", "kind": "discussion"},
        {"type": "hots", "scope": "day"},
    ])
    site = FakeSite(wuliangye_ok=True)
    retry = symbols.maybe_run_symbols_cycle(db, client_factory=_factory(site))
    assert retry is not None and retry.status == runner.CYCLE_OK
    assert retry.failures == [] and retry.remaining == []
    assert len(site.calls) == 1 and "&symbol=SZ000858" in site.calls[0]
    state = st.get_state(db)
    assert state.symbols_pending is None
    assert state.symbols_last_business_date == symbols.local_today()
    assert db.query(XueqiuHotPost).count() == 0


def test_retry_with_only_legacy_hots_items_completes_the_day(db, monkeypatch):
    """只剩热帖项的待重试记录：零请求即完成当天，不会被反复重试。"""
    monkeypatch.setattr(symbols.settings, "xueqiu_collector_symbols_run_after", "00:00")
    _store_todays_pending(db, [{"type": "hots", "scope": "day"}, "junk"], attempts=2)
    site = FakeSite()
    retry = symbols.maybe_run_symbols_cycle(db, client_factory=_factory(site))
    assert retry is not None and retry.status == runner.CYCLE_OK
    assert site.calls == []
    state = st.get_state(db)
    assert state.symbols_pending is None
    assert state.symbols_last_business_date == symbols.local_today()
    assert symbols.maybe_run_symbols_cycle(db, client_factory=_factory(FakeSite())) is None


def test_known_work_items_filters_retired_types():
    feed = {"type": "feed", "symbol": "600519", "market": "A股", "kind": "announcement"}
    cube = {"type": "cube", "cube_id": "ZH000001"}
    assert symbols.known_work_items(
        [feed, {"type": "hots", "scope": "week"}, cube, None, "junk", {}]
    ) == [feed, cube]


def test_attempts_exhausted_marks_the_day(db, monkeypatch):
    monkeypatch.setattr(symbols.settings, "xueqiu_collector_symbols_max_attempts", 2)
    _seed_universe(db)
    day = date(2026, 9, 27)
    first = symbols.run_symbols_cycle(db, client_factory=_factory(FakeSite()), business_date=day)
    assert first.status == runner.CYCLE_PARTIAL
    pending = st.get_state(db).symbols_pending
    second = symbols.run_symbols_cycle(
        db, client_factory=_factory(FakeSite()), business_date=day,
        retry_items=pending["items"],
    )
    assert second.status == runner.CYCLE_FAILED  # 五粮液讨论仍 500
    state = st.get_state(db)
    assert state.symbols_last_business_date == day
    assert state.symbols_pending is None
    assert "当日已尝试 2 轮" in state.symbols_last_message


def test_interrupt_keeps_pending_untouched(db):
    _seed_universe(db)
    day = date(2026, 9, 27)
    symbols.run_symbols_cycle(db, client_factory=_factory(FakeSite()), business_date=day)
    before = st.get_state(db).symbols_pending
    stop = threading.Event()
    stop.set()
    result = symbols.run_symbols_cycle(
        db, client_factory=_factory(FakeSite()), business_date=day,
        retry_items=before["items"], stop_event=stop,
    )
    assert result.status == runner.CYCLE_INTERRUPTED
    state = st.get_state(db)
    assert state.symbols_pending == before and state.symbols_last_business_date is None


def test_pending_from_another_day_is_ignored():
    state = _State(symbols_pending={"date": "2026-09-26", "attempts": 2, "items": []})
    assert _due(state, datetime(2026, 9, 27, 8, 0, tzinfo=SHANGHAI)) == (True, "scheduled")


def test_waf_aborts_cycle_and_sets_shared_cooldown(db):
    _seed_universe(db)
    site = FakeSite(waf_on=lambda url: "&symbol=SH600519" in url)
    result = symbols.run_symbols_cycle(db, client_factory=_factory(site))
    assert result.status == runner.CYCLE_WAF
    # 五粮液公告 → 五粮液讨论（500，单项失败继续）→ 茅台公告 → 茅台讨论（WAF）即止：
    # 组合不再请求
    assert len(site.calls) == 4
    state = st.get_state(db)
    assert state.last_waf_at is not None  # 作者轮次同样进入冷却
    due, reason = st.cycle_due(
        state, st.utcnow(), interval_minutes=60, waf_cooldown_seconds=1800
    )
    assert (due, reason) == (False, "waf_cooldown")
    assert db.query(XueqiuSymbolPost).count() == 3  # 已完成的公告保留


def test_cycle_without_cookie_degrades_explicitly(db, monkeypatch):
    monkeypatch.setattr(symbols.settings, "xueqiu_cookies", "")
    result = symbols.run_symbols_cycle(db, client_factory=_factory(FakeSite()))
    assert result.status == runner.CYCLE_UNAVAILABLE
    state = st.get_state(db)
    assert "未配置雪球 Cookie" in state.symbols_last_message
    # 不可用不是「当天跑完了」：不记业务日，待重试整轮（items=None）
    assert state.symbols_last_business_date is None
    assert state.symbols_pending["items"] is None and state.symbols_pending["attempts"] == 1


def test_cycle_respects_shared_advisory_lock(db):
    lock = runner._CycleLock(db)
    assert lock.acquire()
    try:
        result = symbols.run_symbols_cycle(db, client_factory=_factory(FakeSite()))
    finally:
        lock.release()
    assert result.status == runner.CYCLE_LOCKED
    assert st.get_state(db).symbols_last_started_at is None


def test_stop_signal_interrupts_without_marking_the_day(db):
    _seed_universe(db)
    stop = threading.Event()
    stop.set()
    result = symbols.run_symbols_cycle(db, client_factory=_factory(FakeSite()), stop_event=stop)
    assert result.status == runner.CYCLE_INTERRUPTED
    assert st.get_state(db).symbols_last_business_date is None


def test_explicit_targets_do_not_mark_the_day(db):
    site = FakeSite()
    result = symbols.run_symbols_cycle(
        db, targets=symbols.explicit_targets(["600519"], "A股"), include_market_wide=False,
        record_daily=False, client_factory=_factory(site),
    )
    assert result.status == runner.CYCLE_OK and len(site.calls) == 2
    assert st.get_state(db).symbols_last_business_date is None


# --------------------------------------------------------------------------- #
# 调度判据
# --------------------------------------------------------------------------- #
class _State:
    def __init__(self, **kwargs):
        self.last_waf_at = None
        self.symbols_run_requested_at = None
        self.symbols_last_started_at = None
        self.symbols_last_business_date = None
        self.symbols_last_finished_at = None
        self.symbols_pending = None
        self.__dict__.update(kwargs)


SHANGHAI = timezone(timedelta(hours=8))


def _due(state, local_dt):
    return st.symbols_cycle_due(
        state, local_dt.astimezone(timezone.utc),
        run_after=st.parse_run_after("07:30"), waf_cooldown_seconds=1800,
    )


def test_symbols_cycle_due_schedule():
    morning = datetime(2026, 9, 27, 7, 29, tzinfo=SHANGHAI)
    after = datetime(2026, 9, 27, 7, 31, tzinfo=SHANGHAI)
    assert _due(_State(), morning) == (False, "before_window")
    assert _due(_State(), after) == (True, "scheduled")
    assert _due(_State(symbols_last_business_date=date(2026, 9, 27)), after) == (
        False, "done_today",
    )
    # 业务日按东八区算：UTC 已是 9/26 23:31，本地是 9/27 07:31
    assert _due(_State(symbols_last_business_date=date(2026, 9, 26)), after) == (
        True, "scheduled",
    )
    requested = _State(
        symbols_run_requested_at=after.astimezone(timezone.utc),
        symbols_last_business_date=date(2026, 9, 27),
    )
    assert _due(requested, morning + timedelta(hours=3)) == (True, "requested")
    cooling = _State(last_waf_at=(after - timedelta(minutes=5)).astimezone(timezone.utc))
    assert _due(cooling, after) == (False, "waf_cooldown")
    # 当天有待重试：距上一轮结束不足 60 分钟等待，满了才重试
    pending = {"date": "2026-09-27", "attempts": 1, "items": []}
    waiting = _State(
        symbols_pending=pending,
        symbols_last_finished_at=(after - timedelta(minutes=30)).astimezone(timezone.utc),
    )
    assert _due(waiting, after) == (False, "retry_wait")
    assert _due(waiting, after + timedelta(minutes=31)) == (True, "retry")


def test_parse_run_after_falls_back():
    assert st.parse_run_after("08:15").strftime("%H:%M") == "08:15"
    assert st.parse_run_after("oops").strftime("%H:%M") == "07:30"
    assert st.parse_run_after(None).strftime("%H:%M") == "07:30"


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
PASSWORD = "symbol-feed-api-password"


@pytest.fixture
def clients(db):
    users = {u.username: u for u in db.query(User).filter(User.id.in_([1, 2])).all()}
    originals = {name: user.hashed_password for name, user in users.items()}
    for user in users.values():
        user.hashed_password = get_password_hash(PASSWORD)
    db.commit()
    result = {}
    for name in ("admin", "demo"):
        client = TestClient(app)
        token = client.post(
            "/api/auth/token", json={"username": name, "password": PASSWORD}
        ).json()["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        result[name] = client
    try:
        yield result
    finally:
        for name, user in users.items():
            user.hashed_password = originals[name]
        db.commit()


def test_cube_mutations_are_admin_only(clients):
    user, admin = clients["demo"], clients["admin"]
    payload = {"cube_id": "zh000001", "display_name": "样例组合"}
    assert user.post("/api/xueqiu-collector/cubes", json=payload).status_code == 403
    assert user.patch("/api/xueqiu-collector/cubes/ZH000001", json={}).status_code == 403
    assert user.delete("/api/xueqiu-collector/cubes/ZH000001").status_code == 403

    created = admin.post("/api/xueqiu-collector/cubes", json=payload)
    assert created.status_code == 201 and created.json()["cube_id"] == "ZH000001"
    assert admin.post("/api/xueqiu-collector/cubes", json=payload).status_code == 409
    assert user.get("/api/xueqiu-collector/cubes").json()[0]["cube_id"] == "ZH000001"

    patched = admin.patch("/api/xueqiu-collector/cubes/zh000001", json={"enabled": False})
    assert patched.status_code == 200 and patched.json()["enabled"] is False
    status_body = user.get("/api/xueqiu-collector/status").json()
    assert status_body["cubes"][0]["enabled"] is False
    assert admin.delete("/api/xueqiu-collector/cubes/ZH000001").status_code == 204
    assert admin.delete("/api/xueqiu-collector/cubes/ZH000001").status_code == 404


@pytest.mark.parametrize("bad", ["", "000001", "Z1", "ZH", "ZH-1", "https://xueqiu.com/P/ZH1"])
def test_cube_id_validation(clients, bad):
    response = clients["admin"].post("/api/xueqiu-collector/cubes", json={"cube_id": bad})
    assert response.status_code == 422


def test_symbol_feed_endpoint_and_hots_endpoint_removed(clients, db):
    feed_store.upsert_symbol_posts(
        db, "00700", "港股", "announcement", fp.parse_statuses(ANNOUNCEMENTS)
    )
    feed_store.upsert_symbol_posts(db, "00700", "港股", "discussion", fp.parse_statuses(DISCUSSIONS))
    # 存量热帖行保留在库里，但不再有读取入口
    feed_store.upsert_hot_posts(db, "day", fp.parse_statuses(HOTS), st.utcnow())
    db.commit()
    user = clients["demo"]

    body = user.get(
        "/api/xueqiu-collector/symbol-feed", params={"symbol": "700", "market": "港股", "limit": 2}
    ).json()
    assert body["symbol"] == "00700"  # 手工入口归一化
    assert [item["post_id"] for item in body["announcements"]] == ["405111930", "405111926"]
    assert len(body["discussions"]) == 2
    assert body["announcements"][0]["links"] == ["https://notice.example.invalid/405111930.PDF"]

    only = user.get(
        "/api/xueqiu-collector/symbol-feed",
        params={"symbol": "00700", "market": "港股", "kind": "discussion"},
    ).json()
    assert only["announcements"] == [] and len(only["discussions"]) == 4

    bad_market = user.get(
        "/api/xueqiu-collector/symbol-feed", params={"symbol": "000001", "market": "场外开基"}
    )
    assert bad_market.status_code == 422

    assert user.get("/api/xueqiu-collector/hots", params={"limit": 3}).status_code == 404
    assert db.query(XueqiuHotPost).count() == 4


def test_status_exposes_retry_pending(clients, db):
    today = symbols.local_today().isoformat()
    db.execute(
        text("UPDATE xueqiu_collector_state SET symbols_pending = CAST(:p AS jsonb) WHERE id=1"),
        {"p": json.dumps({"date": today, "attempts": 1, "items": [
            {"type": "feed", "symbol": "600519", "market": "A股", "kind": "discussion"},
            {"type": "hots"},  # 下线前留下的热帖项：与重试执行同口径，不计入
        ]})},
    )
    db.commit()
    body = clients["demo"].get("/api/xueqiu-collector/status").json()["symbols"]
    assert body["retry_pending"] is True
    assert body["retry_attempts"] == 1 and body["retry_item_count"] == 1


def test_run_now_symbols_target(clients, monkeypatch):
    admin = clients["admin"]
    assert admin.post("/api/xueqiu-collector/run-now?target=symbols").status_code == 409
    monkeypatch.setattr(api_module.settings, "xueqiu_collector_enabled", True)
    monkeypatch.setattr(api_module.settings, "xueqiu_collector_symbols_enabled", False)
    disabled = admin.post("/api/xueqiu-collector/run-now?target=symbols")
    assert disabled.status_code == 409 and "按标的" in disabled.json()["detail"]
    monkeypatch.setattr(api_module.settings, "xueqiu_collector_symbols_enabled", True)
    body = admin.post("/api/xueqiu-collector/run-now?target=symbols").json()
    assert body["symbols"]["run_pending"] is True
    assert body["run_pending"] is False  # 作者轮次的请求不受影响
    assert body["symbols"]["run_after"] == "07:30"
    assert clients["demo"].post("/api/xueqiu-collector/run-now?target=symbols").status_code == 403
    assert admin.post("/api/xueqiu-collector/run-now?target=bogus").status_code == 422


# --------------------------------------------------------------------------- #
# 旧仓库 Markdown 产物一次性导入（渲染格式逐字取自原 monitor_symbols 的真实导出）
# --------------------------------------------------------------------------- #
ANNOUNCEMENT_MD = """# 个股公告 · SH600519

> 数据源：xueqiu.com · 共 2 条 · 生成 2026-09-27 07:30:01

### 2026-08-14 20:46:28
[原文链接](https://xueqiu.com/S/SH600519/405111930) · 赞 3 · 评 42

贵州茅台：贵州茅台关于召开2026年半年度业绩说明会的公告 网页链接

### 2026-08-14 20:46:26
[原文链接](https://xueqiu.com/S/SH600519/405111926) · 赞 5 · 评 0

贵州茅台：贵州茅台第五届董事会2026年度第三次会议决议公告 网页链接
"""
DISCUSSION_MD_OLD = """# 个股讨论 · SH600519

### 2026-09-27 07:23:25
[原文链接](https://xueqiu.com/9731668124/410650580) · 赞 0 · 评 0

$贵州茅台(SH600519)$ 茅台机场茅台专卖店停业，关门大吉。
"""
DISCUSSION_MD_NEW = """# 个股讨论 · SH600519

### 2026-09-27 07:23:25
[原文链接](https://xueqiu.com/9731668124/410650580) · 赞 7 · 评 2

$贵州茅台(SH600519)$ 茅台机场茅台专卖店停业，关门大吉。

### 2026-09-27 07:12:40
[原文链接](https://xueqiu.com/4812418043/410650443) · 赞 0 · 评 0

（无正文）

### 2026-09-27 07:00:00
没有链接行的坏块
"""
HOTS_MD = """# 市场热帖快照（day）

### 2026-09-26 13:02:26 · 热度 False
[原文链接](https://xueqiu.com/8611009509/410628734) · 赞 58

第一条
多行正文

### 2026-09-26 10:11:40 · 热度 False
[原文链接](https://xueqiu.com/5124430882/410622571) · 赞 27

第二条
"""


def _write_exports(tmp_path):
    (tmp_path / "stock-announcement-SH600519-20260927-073001.md").write_text(ANNOUNCEMENT_MD, "utf-8")
    (tmp_path / "stock-discussion-SH600519-20260926-073019.md").write_text(DISCUSSION_MD_OLD, "utf-8")
    (tmp_path / "stock-discussion-SH600519-20260927-073019.md").write_text(DISCUSSION_MD_NEW, "utf-8")
    (tmp_path / "market-hots-day-20260927-073047.md").write_text(HOTS_MD, "utf-8")
    # 组合调仓没有调仓 ID：不导入
    (tmp_path / "cube-rebalancing-ZH000001-20260901-160350.md").write_text("# 组合调仓", "utf-8")
    (tmp_path / "user-1000000001-speech-last-30-days.md").write_text("# 无关", "utf-8")


def test_archive_markdown_parsing(tmp_path):
    from app.services.xueqiu_collector import archive_import as ai

    posts, malformed = ai.parse_markdown(DISCUSSION_MD_NEW)
    assert malformed == 1
    assert [(p.post_id, p.author_id, p.text) for p in posts] == [
        ("410650580", "9731668124", "$贵州茅台(SH600519)$ 茅台机场茅台专卖店停业，关门大吉。"),
        ("410650443", "4812418043", ""),
    ]
    assert posts[0].payload == {"archive_import": True, "fav_count": 7, "reply_count": 2}
    assert posts[0].created_at_ms == int(
        datetime(2026, 9, 27, 7, 23, 25, tzinfo=SHANGHAI).timestamp() * 1000
    )
    ann, _ = ai.parse_markdown(ANNOUNCEMENT_MD)
    assert ann[0].author_id == "" and ann[0].url == "https://xueqiu.com/S/SH600519/405111930"
    hots, _ = ai.parse_markdown(HOTS_MD)
    assert hots[0].text == "第一条\n多行正文" and hots[0].payload["fav_count"] == 58

    classified = ai.classify_file(tmp_path / "stock-discussion-SZ200596-20260927-073019.md")
    assert (classified.symbol, classified.market, classified.kind) == ("200596", "B股", "discussion")
    assert ai.classify_file(tmp_path / "cube-rebalancing-ZH000001-20260901-160350.md") is None


def test_archive_import_is_idempotent_and_insert_only(db, tmp_path):
    from app.services.xueqiu_collector import archive_import as ai

    _write_exports(tmp_path)
    # 采集器已写过的行更完整：导入不得覆盖
    collected = fp.parse_statuses(DISCUSSIONS)[:1]  # 410650580
    feed_store.upsert_symbol_posts(db, "600519", "A股", "discussion", collected)
    db.commit()

    dry = ai.import_archive_exports(db, tmp_path, dry_run=True)
    assert dry.files == 4 and dry.inserted == {}
    assert db.query(XueqiuSymbolPost).count() == 1

    stats = ai.import_archive_exports(db, tmp_path)
    assert stats.inserted == {"announcement": 2, "discussion": 1, "hots:day": 2}
    assert stats.symbols == {("600519", "A股")}
    rows = {(r.kind, r.post_id): r for r in db.query(XueqiuSymbolPost).all()}
    assert len(rows) == 4
    assert rows[("discussion", "410650580")].author_name == "fixture_user_8124"  # 采集器版本保留
    assert "archive_import" not in rows[("discussion", "410650580")].payload
    for (value,) in db.execute(text("SELECT DISTINCT symbol FROM xueqiu_symbol_posts")):
        assert value == "600519" and not XUEQIU_SYMBOL_RE.match(value)
    # 旧热帖快照照常导入（表保留，只是不再展示）
    hot_rows = db.query(XueqiuHotPost).filter_by(scope="day").order_by(XueqiuHotPost.rank).all()
    assert [row.rank for row in hot_rows] == [1, 2]
    assert {row.snapshot_at for row in hot_rows} == {
        datetime(2026, 9, 27, 7, 30, 47, tzinfo=SHANGHAI)
    }

    again = ai.import_archive_exports(db, tmp_path)
    assert again.inserted == {"announcement": 0, "discussion": 0, "hots:day": 0}
    assert db.query(XueqiuSymbolPost).count() == 4 and db.query(XueqiuHotPost).count() == 2


def test_archive_import_keeps_newest_snapshot_counts(db, tmp_path):
    from app.services.xueqiu_collector import archive_import as ai

    _write_exports(tmp_path)
    ai.import_archive_exports(db, tmp_path)
    row = db.query(XueqiuSymbolPost).filter_by(post_id="410650580").one()
    assert row.payload["fav_count"] == 7  # 9/27 快照（新）先处理，9/26 的 0 不覆盖
