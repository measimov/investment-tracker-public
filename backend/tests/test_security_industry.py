"""行业分类：东方财富/EDGAR 解析（真实响应金样）、SIC 映射、读取优先级、同步新鲜度与
失败隔离、GET /api/securities/industries 的用户范围。"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import requests

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.holding import Holding
from app.models.security_industry import SecurityIndustry
from app.models.security_rule import SecurityRule
from app.models.user import User
from app.models.watchlist_item import WatchlistItem
from app.services import security_industry_service as svc

from .helpers import reset_tables

FIXTURES = Path(__file__).parent / "fixtures"
RESET_MODELS = [WatchlistItem, Holding, SecurityIndustry]
PASSWORD = "industry-api-password"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _clean_miss_cache():
    svc.reset_miss_cache()
    yield
    svc.reset_miss_cache()


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        reset_tables(session, RESET_MODELS)
        session.query(SecurityRule).filter(SecurityRule.rule_type == "INDUSTRY").delete()
        session.commit()
        yield session
        session.rollback()
        reset_tables(session, RESET_MODELS)
        session.query(SecurityRule).filter(SecurityRule.rule_type == "INDUSTRY").delete()
        session.commit()
    finally:
        session.close()


def _user_id(db, username="demo"):
    return db.query(User).filter(User.username == username).one().id


def _hold(db, user_id, symbol, market, quantity="100"):
    db.add(
        Holding(
            user_id=user_id,
            symbol=symbol,
            market=market,
            quantity=Decimal(quantity),
            avg_cost=Decimal("1"),
            total_cost=Decimal(quantity),
            currency="CNY",
        )
    )


def _row(db, symbol, market, source, industry, *, age_days=0):
    db.add(
        SecurityIndustry(
            symbol=symbol,
            market=market,
            source=source,
            industry=industry,
            raw={},
            fetched_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        )
    )


def _rule(db, user_id, symbol, market, industry):
    db.add(
        SecurityRule(
            user_id=user_id,
            rule_type="INDUSTRY",
            symbol=symbol,
            market=market,
            payload={"industry": industry},
        )
    )


# ---------------------------------------------------------------- 纯函数


def test_parse_eastmoney_hk_fixture():
    parsed = svc.parse_eastmoney_industries("港股", _fixture("eastmoney/hkf10_orgprofile.json"))
    assert {symbol: item["industry"] for symbol, item in parsed.items()} == {
        "00700": "软件服务",
        "00883": "石油及天然气",
        "02313": "纺织及服饰",
    }
    assert parsed["00700"]["raw"]["SECUCODE"] == "00700.HK"


def test_parse_eastmoney_us_fixture_maps_back_to_ledger_symbol():
    parsed = svc.parse_eastmoney_industries("美股", _fixture("eastmoney/usf10_orgprofile.json"))
    assert parsed["PDD"]["industry"] == "多品类零售"
    assert parsed["AAPL"]["industry"] == "电脑硬件、储存设备及电脑周边"
    assert set(parsed) == {"PDD", "BABA", "AAPL"}


def test_parse_eastmoney_a_and_b_shares_take_em2016_second_level():
    payload = _fixture("eastmoney/f10_basic_orginfo.json")
    a_shares = svc.parse_eastmoney_industries("A股", payload)
    assert a_shares["600036"]["industry"] == "银行"
    assert a_shares["600036"]["raw"]["EM2016"] == "金融-银行-股份制与城商行"
    b_shares = svc.parse_eastmoney_industries("B股", payload)
    assert b_shares["900901"]["industry"] == "计算机软件"
    assert b_shares["200002"]["industry"] == "房地产开发"


def test_eastmoney_industry_falls_back_to_csrc():
    row = {"SECURITY_CODE": "600000", "EM2016": None, "INDUSTRYCSRC1": "金融业-货币金融服务"}
    assert svc.eastmoney_industry("A股", row) == "货币金融服务"
    assert svc.eastmoney_industry("A股", {"SECURITY_CODE": "600000"}) is None


def test_eastmoney_9201_is_empty_result_not_error():
    assert svc.parse_eastmoney_rows(_fixture("eastmoney/empty_9201.json")) == []


@pytest.mark.parametrize(
    "payload",
    [
        _fixture("eastmoney/unknown_report_9501.json"),
        {"success": True, "result": None},
        {"success": True, "result": {"data": "x"}},
        {"result": {"data": []}},  # 没有 success 字段 = 未知结构
        ["not", "an", "object"],
        None,
    ],
)
def test_eastmoney_errors_are_explicit(payload):
    with pytest.raises(svc.EastmoneyError):
        svc.parse_eastmoney_rows(payload)


def test_build_eastmoney_params_batches_with_in_filter():
    params = svc.build_eastmoney_params("港股", ["00700", "00883"])
    assert params["reportName"] == "RPT_HKF10_INFO_ORGPROFILE"
    assert params["filter"] == '(SECUCODE in ("00700.HK","00883.HK"))'
    us = svc.build_eastmoney_params("美股", ["PDD", "BRK.B"])
    assert us["filter"] == '(SECURITY_CODE in ("PDD","BRK_B"))'
    assert svc.eastmoney_symbol("美股", {"SECURITY_CODE": "BRK_B"}) == "BRK.B"
    b = svc.build_eastmoney_params("B股", ["900901"])
    assert b["reportName"] == "RPT_F10_BASIC_ORGINFO"


def test_eastmoney_get_wraps_http_and_json_failures(monkeypatch):
    monkeypatch.setattr(svc, "EASTMONEY_MIN_INTERVAL_SECONDS", 0)

    class _Response:
        def __init__(self, status, body):
            self.status_code = status
            self._body = body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"{self.status_code} error")

        def json(self):
            if isinstance(self._body, Exception):
                raise self._body
            return self._body

    monkeypatch.setattr(svc.requests, "get", lambda *a, **k: _Response(503, {}))
    with pytest.raises(svc.EastmoneyError, match="请求失败"):
        svc.fetch_eastmoney_industries("港股", ["00700"])
    monkeypatch.setattr(svc.requests, "get", lambda *a, **k: _Response(200, ValueError("no json")))
    with pytest.raises(svc.EastmoneyError, match="JSON"):
        svc.fetch_eastmoney_industries("港股", ["00700"])

    def _timeout(*a, **k):
        raise requests.Timeout("slow")

    monkeypatch.setattr(svc.requests, "get", _timeout)
    with pytest.raises(svc.EastmoneyError):
        svc.fetch_eastmoney_industries("港股", ["00700"])
    monkeypatch.setattr(
        svc.requests,
        "get",
        lambda *a, **k: _Response(200, _fixture("eastmoney/hkf10_orgprofile.json")),
    )
    assert svc.fetch_eastmoney_industries("港股", ["00700"])["00700"]["industry"] == "软件服务"


@pytest.mark.parametrize(
    "sic,label",
    [
        ("7372", "软件"),
        ("7389", "商业服务"),
        ("7370", "信息技术服务"),
        ("7379", "信息技术服务"),  # 3 位 737
        ("2834", "医药"),
        ("2833", "医药"),  # 3 位 283
        ("3845", "医疗器械"),
        ("3674", "半导体"),
        ("3663", "通信设备"),
        ("3690", "电子电气设备"),
        ("4911", "公用事业"),
        ("4813", "电信服务"),
        ("6022", "银行"),
        ("6311", "保险"),
        ("1311", "石油天然气"),
        ("5961", "电商零售"),
        ("5812", "餐饮"),
        ("100", "农林牧渔"),  # 前导零被 JSON 数字化后仍按 0100 取大类
        ("", None),
        (None, None),
        ("abc", None),
        ("0500", None),  # 不存在的大类不猜
    ],
)
def test_sic_industry_label(sic, label):
    assert svc.sic_industry_label(sic) == label


def test_parse_edgar_submissions_fixture():
    parsed = svc.parse_edgar_submissions(_fixture("edgar/pdd_submissions_min.json"))
    assert parsed == {"sic": "7389", "sic_description": "Services-Business Services, NEC"}
    assert svc.parse_edgar_submissions({"sic": "", "name": "x"}) is None
    with pytest.raises(ValueError):
        svc.parse_edgar_submissions([])


# ---------------------------------------------------------------- 读取优先级


def test_resolve_priority_rule_over_official_over_eastmoney(db):
    uid = _user_id(db)
    admin = _user_id(db, "admin")
    _row(db, "600036", "A股", "tushare", "银行")
    _row(db, "600036", "A股", "eastmoney", "股份制与城商行")
    _row(db, "00700", "港股", "eastmoney", "软件服务")
    _row(db, "PDD", "美股", "edgar", "商业服务")
    _row(db, "PDD", "美股", "eastmoney", "多品类零售")
    _rule(db, uid, "PDD", "美股", "电商")
    _rule(db, uid, "700", "港股", "互联网")  # 未补零的规则代码按账本口径归一
    _rule(db, admin, "600036", "A股", "别人的规则")
    db.commit()

    keys = [("600036", "A股"), ("00700", "港股"), ("PDD", "美股"), ("ZZZZ", "美股")]
    resolved = svc.resolve_industries(db, keys, uid)
    assert resolved[("600036", "A股")]["industry"] == "银行"
    assert resolved[("600036", "A股")]["source"] == "tushare"
    assert resolved[("00700", "港股")] == {
        "industry": "互联网",
        "source": "rule",
        "fetched_at": None,
    }
    assert resolved[("PDD", "美股")]["industry"] == "电商"
    assert resolved[("PDD", "美股")]["source"] == "rule"
    assert resolved[("ZZZZ", "美股")] == {"industry": None, "source": None, "fetched_at": None}

    # 无用户上下文：只看来源表，官方优先
    anonymous = svc.resolve_industries(db, keys, None)
    assert anonymous[("PDD", "美股")]["source"] == "edgar"
    assert anonymous[("00700", "港股")]["source"] == "eastmoney"
    # admin 的规则只影响 admin
    assert svc.resolve_industries(db, keys, admin)[("600036", "A股")]["source"] == "rule"


# ---------------------------------------------------------------- 同步


class _Sources:
    """可编排的三个来源桩：记录调用，按字典应答。"""

    def __init__(self, monkeypatch, *, tushare=None, edgar=None, eastmoney=None):
        self.tushare = tushare if tushare is not None else {}
        self.edgar = edgar if edgar is not None else {}
        self.eastmoney = eastmoney if eastmoney is not None else {}
        self.calls = {"tushare": 0, "edgar": [], "eastmoney": []}
        monkeypatch.setattr(svc, "load_tushare_industry_map", self._tushare)
        monkeypatch.setattr(svc, "fetch_edgar_sic", self._edgar)

    def _tushare(self):
        self.calls["tushare"] += 1
        if isinstance(self.tushare, Exception):
            raise self.tushare
        return dict(self.tushare)

    def _edgar(self, symbol):
        self.calls["edgar"].append(symbol)
        value = self.edgar.get(symbol)
        if isinstance(value, Exception):
            raise value
        return value

    def fetch_eastmoney(self, market, symbols):
        self.calls["eastmoney"].append((market, tuple(symbols)))
        value = self.eastmoney.get(market, {})
        if isinstance(value, Exception):
            raise value
        return {
            symbol: {"industry": industry, "raw": {"BELONG_INDUSTRY": industry}}
            for symbol, industry in value.items()
            if symbol in symbols
        }


KEYS = [
    ("600036", "A股"),
    ("000001", "A股"),
    ("900901", "B股"),
    ("00700", "港股"),
    ("PDD", "美股"),
    ("ZZZZ", "美股"),
]


def _stored(db):
    db.expire_all()
    return {
        (row.symbol, row.market, row.source): row.industry
        for row in db.query(SecurityIndustry).all()
    }


def _sources_for_full_scope(monkeypatch):
    return _Sources(
        monkeypatch,
        tushare={"600036": "银行"},
        edgar={
            "PDD": {
                "sic": "7389",
                "sic_description": "Services-Business Services, NEC",
                "cik": 1737806,
            }
        },
        eastmoney={
            "A股": {"000001": "银行"},
            "B股": {"900901": "计算机软件"},
            "港股": {"00700": "软件服务"},
            "美股": {"ZZZZ": "多品类零售", "PDD": "不应被请求"},
        },
    )


def test_sync_official_first_then_eastmoney_fills_gaps(db, monkeypatch):
    sources = _sources_for_full_scope(monkeypatch)
    result = svc.sync_security_industries(db, keys=KEYS, eastmoney_fetcher=sources.fetch_eastmoney)

    assert _stored(db) == {
        ("600036", "A股", "tushare"): "银行",
        ("000001", "A股", "eastmoney"): "银行",
        ("900901", "B股", "eastmoney"): "计算机软件",
        ("00700", "港股", "eastmoney"): "软件服务",
        ("PDD", "美股", "edgar"): "商业服务",
        ("ZZZZ", "美股", "eastmoney"): "多品类零售",
    }
    # 东方财富只补官方缺口：600036 / PDD 不进请求
    requested = {symbol for _, chunk in sources.calls["eastmoney"] for symbol in chunk}
    assert requested == {"000001", "900901", "00700", "ZZZZ"}
    assert result["sources"]["tushare"]["missing"] == ["000001:A股"]
    assert result["sources"]["edgar"]["missing"] == ["ZZZZ:美股"]
    assert all(item["status"] == "ok" for item in result["sources"].values())
    assert result["resolved"] == 6 and result["unresolved"] == []
    edgar_row = db.query(SecurityIndustry).filter_by(symbol="PDD", source="edgar").one()
    assert edgar_row.raw["sic_description"] == "Services-Business Services, NEC"


def test_sync_skips_fresh_rows_and_refetches_stale_or_forced(db, monkeypatch):
    sources = _sources_for_full_scope(monkeypatch)
    svc.sync_security_industries(db, keys=KEYS, eastmoney_fetcher=sources.fetch_eastmoney)

    again = _sources_for_full_scope(monkeypatch)
    result = svc.sync_security_industries(db, keys=KEYS, eastmoney_fetcher=again.fetch_eastmoney)
    assert again.calls == {"tushare": 0, "edgar": [], "eastmoney": []}
    assert all(item["status"] == "skipped" for item in result["sources"].values())

    # 港股行过期 → 只重拉它
    row = db.query(SecurityIndustry).filter_by(symbol="00700").one()
    row.fetched_at = datetime.now(timezone.utc) - timedelta(days=31)
    db.commit()
    stale = _sources_for_full_scope(monkeypatch)
    svc.sync_security_industries(db, keys=KEYS, eastmoney_fetcher=stale.fetch_eastmoney)
    assert stale.calls["eastmoney"] == [("港股", ("00700",))]
    assert stale.calls["tushare"] == 0 and stale.calls["edgar"] == []

    forced = _sources_for_full_scope(monkeypatch)
    svc.sync_security_industries(
        db, keys=KEYS, force=True, eastmoney_fetcher=forced.fetch_eastmoney
    )
    assert forced.calls["tushare"] == 1
    assert forced.calls["edgar"] == ["PDD", "ZZZZ"]


def test_sync_source_failures_are_isolated_and_keep_old_rows(db, monkeypatch):
    _row(db, "00700", "港股", "eastmoney", "旧行业", age_days=40)
    db.commit()
    sources = _Sources(
        monkeypatch,
        tushare=RuntimeError("tushare token 无效"),
        edgar={"PDD": requests.ConnectionError("edgar down")},
        eastmoney={
            "A股": {"600036": "银行"},
            "港股": svc.EastmoneyError("东方财富返回失败 code=500"),
            "B股": {"900901": "计算机软件"},
        },
    )
    keys = [("600036", "A股"), ("00700", "港股"), ("900901", "B股"), ("PDD", "美股")]
    result = svc.sync_security_industries(db, keys=keys, eastmoney_fetcher=sources.fetch_eastmoney)

    assert result["sources"]["tushare"]["status"] == "failed"
    assert "tushare token 无效" in result["sources"]["tushare"]["errors"][0]
    assert result["sources"]["edgar"]["status"] == "failed"
    # 官方失败的 A股/美股 由东方财富补缺；港股这一批失败不影响 A股/B股 批次
    assert result["sources"]["eastmoney"]["status"] == "partial"
    assert any("港股" in error for error in result["sources"]["eastmoney"]["errors"])
    stored = _stored(db)
    assert stored[("600036", "A股", "eastmoney")] == "银行"
    assert stored[("900901", "B股", "eastmoney")] == "计算机软件"
    # 失败绝不写空、也不删旧行
    assert stored[("00700", "港股", "eastmoney")] == "旧行业"
    assert ("PDD", "美股", "edgar") not in stored
    assert result["unresolved"] == ["PDD:美股"]


def test_sync_remembers_misses_within_window(db, monkeypatch):
    sources = _Sources(monkeypatch, eastmoney={"港股": {}})
    keys = [("09999", "港股")]
    first = svc.sync_security_industries(db, keys=keys, eastmoney_fetcher=sources.fetch_eastmoney)
    assert first["sources"]["eastmoney"]["missing"] == ["09999:港股"]
    assert first["unresolved"] == ["09999:港股"]
    assert _stored(db) == {}

    svc.sync_security_industries(db, keys=keys, eastmoney_fetcher=sources.fetch_eastmoney)
    assert len(sources.calls["eastmoney"]) == 1
    svc.sync_security_industries(
        db, keys=keys, force=True, eastmoney_fetcher=sources.fetch_eastmoney
    )
    assert len(sources.calls["eastmoney"]) == 2


def test_sync_eastmoney_batches(db, monkeypatch):
    sources = _Sources(monkeypatch, eastmoney={"港股": {}})
    keys = [(f"{i:05d}", "港股") for i in range(1, svc.EASTMONEY_BATCH_SIZE + 6)]
    svc.sync_security_industries(db, keys=keys, eastmoney_fetcher=sources.fetch_eastmoney)
    assert [len(chunk) for _, chunk in sources.calls["eastmoney"]] == [svc.EASTMONEY_BATCH_SIZE, 5]


def test_sync_edgar_stops_after_consecutive_failures(db, monkeypatch):
    symbols = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    sources = _Sources(
        monkeypatch,
        edgar={s: requests.ConnectionError("down") for s in symbols},
        eastmoney={"美股": {}},
    )
    result = svc.sync_security_industries(
        db, keys=[(s, "美股") for s in symbols], eastmoney_fetcher=sources.fetch_eastmoney
    )
    assert sources.calls["edgar"] == symbols[: svc.EDGAR_MAX_CONSECUTIVE_FAILURES]
    assert result["sources"]["edgar"]["status"] == "failed"
    assert "本轮其余美股跳过" in result["sources"]["edgar"]["errors"][-1]


def test_scope_keys_active_users_holdings_and_watchlist(db):
    uid = _user_id(db)
    _hold(db, uid, "600036", "A股")
    _hold(db, uid, "00001", "港股", quantity="0")  # 已清仓
    _hold(db, uid, "D05", "新加坡股")  # 不支持的市场
    db.add(WatchlistItem(user_id=uid, symbol="PDD", market="美股"))
    inactive = User(username="industry_inactive", hashed_password="x", is_active=False)
    db.add(inactive)
    db.flush()
    _hold(db, inactive.id, "00700", "港股")
    db.commit()
    try:
        assert svc.scope_keys(db) == [("600036", "A股"), ("PDD", "美股")]
    finally:
        db.delete(inactive)
        db.commit()


# ---------------------------------------------------------------- API


@pytest.fixture
def api_users():
    session = SessionLocal()
    try:
        users = session.query(User).filter(User.username.in_(["demo", "admin"])).all()
        originals = {user.username: user.hashed_password for user in users}
        for user in users:
            user.hashed_password = get_password_hash(PASSWORD)
        session.commit()
        yield {user.username: user.id for user in users}
        for user in users:
            user.hashed_password = originals[user.username]
        session.commit()
    finally:
        session.close()


async def _auth(client, username="demo"):
    login = await client.post("/api/auth/token", json={"username": username, "password": PASSWORD})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.mark.anyio
async def test_industries_endpoint_scoped_to_current_user(db, api_users):
    uid, admin = api_users["demo"], api_users["admin"]
    _hold(db, uid, "00700", "港股")
    db.add(WatchlistItem(user_id=uid, symbol="PDD", market="美股"))
    _hold(db, uid, "D05", "新加坡股")
    _hold(db, admin, "600036", "A股")
    _row(db, "00700", "港股", "eastmoney", "软件服务")
    _row(db, "PDD", "美股", "edgar", "商业服务")
    _row(db, "600036", "A股", "tushare", "银行")
    _rule(db, uid, "D05", "新加坡股", "银行")
    _rule(db, admin, "00700", "港股", "admin 的规则")
    db.commit()

    async with _client() as client:
        response = await client.get("/api/securities/industries", headers=await _auth(client))
        assert response.status_code == 200, response.text
        body = {(item["symbol"], item["market"]): item for item in response.json()}
        assert set(body) == {("00700", "港股"), ("PDD", "美股"), ("D05", "新加坡股")}
        assert body[("00700", "港股")]["industry"] == "软件服务"
        assert body[("00700", "港股")]["source"] == "eastmoney"
        assert body[("PDD", "美股")]["source"] == "edgar"
        assert body[("D05", "新加坡股")] == {
            "symbol": "D05",
            "market": "新加坡股",
            "industry": "银行",
            "source": "rule",
            "fetched_at": None,
        }

        admin_body = (
            await client.get("/api/securities/industries", headers=await _auth(client, "admin"))
        ).json()
        assert [(item["symbol"], item["industry"]) for item in admin_body] == [("600036", "银行")]

        assert (await client.get("/api/securities/industries")).status_code == 401


@pytest.mark.anyio
async def test_industry_rule_api_validation(db, api_users):
    async with _client() as client:
        auth = await _auth(client)

        async def post(body):
            return await client.post("/api/security-rules", headers=auth, json=body)

        ok = await post(
            {
                "rule_type": "INDUSTRY",
                "symbol": "00700",
                "market": "港股",
                "payload": {"industry": "  互联网 "},
            }
        )
        assert ok.status_code == 201, ok.text
        assert ok.json()["payload"] == {"industry": "互联网"}
        for body in (
            {"rule_type": "INDUSTRY", "symbol": "00883", "market": "港股"},
            {
                "rule_type": "INDUSTRY",
                "symbol": "00883",
                "market": "港股",
                "payload": {"industry": "   "},
            },
            {
                "rule_type": "INDUSTRY",
                "symbol": "00883",
                "market": "港股",
                "payload": {"industry": "x" * 51},
            },
            {"rule_type": "INDUSTRY", "symbol": "00883", "payload": {"industry": "能源"}},
            {
                "rule_type": "INDUSTRY",
                "symbol": "00883",
                "market": "港股",
                "payload": {"industry": "能源", "extra": 1},
            },
        ):
            assert (await post(body)).status_code == 422, body
