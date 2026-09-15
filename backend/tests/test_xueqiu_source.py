"""雪球 wrapper 的离线单测：符号转换、降级语义、错误兜底。不联网。

client 一律用假对象注入（`_install_client`），因此这些用例在未安装
xueqiu-market 时也能跑——库只在函数内惰性导入，用例按需塞 fake 模块。
"""

import sys
import types
from decimal import Decimal

import pytest

from app.services import xueqiu_source as xs
from app.services.stock_price_service import Market


@pytest.fixture(autouse=True)
def _reset_singleton():
    xs.reset_client()
    yield
    xs.reset_client()


class FakeClient:
    """记录调用并按脚本返回/抛出。"""

    def __init__(self, **scripted):
        self.calls = []
        self._scripted = scripted

    def _run(self, name, args, kwargs):
        self.calls.append((name, args, kwargs))
        outcome = self._scripted.get(name)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            raise AssertionError(f"未脚本化的调用: {name}")
        return outcome

    def quote(self, *args, **kwargs):
        return self._run("quote", args, kwargs)

    def financial(self, *args, **kwargs):
        return self._run("financial", args, kwargs)

    def capital_history(self, *args, **kwargs):
        return self._run("capital_history", args, kwargs)

    def top_holders(self, *args, **kwargs):
        return self._run("top_holders", args, kwargs)


def _install_client(monkeypatch, client):
    monkeypatch.setattr(xs, "get_client", lambda: client)


def _install_fake_library(monkeypatch, **members):
    """把一个假的 xueqiu_market 模块塞进 sys.modules（覆盖真库，若已安装）。"""
    module = types.ModuleType("xueqiu_market")
    for name, value in members.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "xueqiu_market", module)


# --------------------------------------------------------------------------- #
# 符号转换
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600519", "SH600519"),
        ("000001", "SZ000001"),
        ("830799", "BJ830799"),
        # 北交所 920xxx：库的 infer_a_exchange 会判成 SH（"9" 开头先命中沪市
        # 分支，它自己的 BJ 分支对 92 前缀永不可达），必须由本项目的
        # get_exchange_type 纠正，否则请求的是不存在的 SH920599。
        ("920599", "BJ920599"),
    ],
)
def test_a_share_symbol_uses_project_exchange_rules(symbol, expected):
    assert xs.to_xueqiu(symbol, "A股") == expected


def test_non_a_share_symbol_delegates_to_library(monkeypatch):
    _install_fake_library(monkeypatch, to_xueqiu_symbol=lambda code, market: f"<{code}|{market}>")
    assert xs.to_xueqiu("700", "港股") == "<700|港股>"
    assert xs.to_xueqiu("AAPL", "美股") == "<AAPL|美股>"


# --------------------------------------------------------------------------- #
# Cookie 装载
# --------------------------------------------------------------------------- #
def test_cookies_from_json_flattens_j2team_export():
    """J2Team 形状必须被摊平。

    库的 load_cookies 对 dict 入参原样返回（dict 分支排在 "cookies" 键分支
    之前），直接透传会得到一个名叫 "cookies" 的假 Cookie，请求照发但无登录态。
    """
    raw = '{"cookies": [{"name": "xq_a_token", "value": "abc"}, {"name": "u", "value": "1"}]}'
    assert xs._cookies_from_json(raw) == {"xq_a_token": "abc", "u": "1"}


def test_cookies_from_json_accepts_plain_mapping():
    assert xs._cookies_from_json('{"xq_a_token": "abc"}') == {"xq_a_token": "abc"}


def test_cookies_from_json_rejects_unknown_shape():
    with pytest.raises(xs.XueqiuUnavailable):
        xs._cookies_from_json('"just-a-string"')


# --------------------------------------------------------------------------- #
# 行情：全部失败路径都返回 success=False，绝不抛出
# --------------------------------------------------------------------------- #
def test_quote_unsupported_market_degrades_explicitly():
    result = xs.fetch_xueqiu_stock_price("BTC", Market.CRYPTO)
    assert result["success"] is False
    # source 必须是具体 provider tag，不能是空/unknown
    assert result["source"] == "xueqiu-quote"
    assert "不支持" in result["error"]
    assert result["price"] is None


def test_quote_without_cookie_degrades_not_raises(monkeypatch):
    monkeypatch.setattr(xs.settings, "xueqiu_cookies", "")
    monkeypatch.setattr(xs.settings, "xueqiu_cookie_file", "")
    result = xs.fetch_xueqiu_stock_price("600519", Market.A_STOCK)
    assert result["success"] is False
    assert result["source"] == "xueqiu-quote"
    assert "Cookie" in result["error"]


def test_quote_happy_path(monkeypatch):
    _install_client(monkeypatch, FakeClient(quote={"quote": {"current": "1688.88"}}))
    _install_fake_library(
        monkeypatch,
        quote_to_price_result=lambda quote, source: {
            "price": Decimal(str(quote["current"])),
            "source": source,
            "success": True,
            "error": None,
        },
    )
    result = xs.fetch_xueqiu_stock_price("600519", Market.A_STOCK)
    assert result["success"] is True
    assert result["price"] == Decimal("1688.88")
    assert result["source"] == "xueqiu-quote"
    assert result["timestamp"].tzinfo is not None


def test_quote_library_error_is_caught(monkeypatch):
    _install_client(monkeypatch, FakeClient(quote=RuntimeError("命中阿里云 WAF 挑战页")))
    result = xs.fetch_xueqiu_stock_price("600519", Market.A_STOCK)
    assert result["success"] is False
    assert result["source"] == "xueqiu-quote"
    assert "WAF" in result["error"]


def test_quote_rejects_non_positive_price(monkeypatch):
    """停牌标的的 current 可能是 0——adapter 只判 not None，会当成成功。"""
    _install_client(monkeypatch, FakeClient(quote={"quote": {"current": "0"}}))
    _install_fake_library(
        monkeypatch,
        quote_to_price_result=lambda quote, source: {
            "price": Decimal(str(quote["current"])),
            "source": source,
            "success": True,
            "error": None,
        },
    )
    result = xs.fetch_xueqiu_stock_price("600519", Market.A_STOCK)
    assert result["success"] is False
    assert result["price"] is None


# --------------------------------------------------------------------------- #
# 数据集：不支持的市场必须显式抛错，不能返回空列表冒充成功
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("market", ["港股", "美股"])
def test_financial_rejects_unsupported_market(monkeypatch, market):
    _install_client(monkeypatch, FakeClient())
    with pytest.raises(xs.XueqiuUnavailable) as excinfo:
        xs.fetch_income_rows("00700", market)
    assert "A股" in str(excinfo.value)


@pytest.mark.parametrize("market", ["港股", "美股"])
def test_capital_flow_rejects_unsupported_market(monkeypatch, market):
    _install_client(monkeypatch, FakeClient())
    with pytest.raises(xs.XueqiuUnavailable):
        xs.fetch_capital_flow_rows("00700", market)


def test_income_rows_pass_through_adapter(monkeypatch):
    client = FakeClient(financial={"list": [{"report_date": 1, "revenue": [1.0, 0.1]}]})
    _install_client(monkeypatch, client)
    _install_fake_library(
        monkeypatch,
        financial_rows=lambda data: [{"period_key": "20251231", "revenue": 1.0}],
    )
    rows = xs.fetch_income_rows("600519", "A股")
    assert rows == [{"period_key": "20251231", "revenue": 1.0}]
    name, args, kwargs = client.calls[0]
    assert (name, args) == ("financial", ("SH600519", "income"))
    assert kwargs == {"count": xs.FINANCIAL_COUNT}


def test_capital_flow_calls_upstream_with_expected_args(monkeypatch):
    client = FakeClient(capital_history={"items": [{"timestamp": 1767110400000, "amount": 12.0}]})
    _install_client(monkeypatch, client)
    xs.fetch_capital_flow_rows("600519", "A股")
    name, args, kwargs = client.calls[0]
    assert (name, args) == ("capital_history", ("SH600519",))
    assert kwargs == {"count": xs.CAPITAL_HISTORY_COUNT}


# --------------------------------------------------------------------------- #
# 日期归一：库 adapter 走 time.localtime，UTC 容器里每个报告期都会偏一天
# --------------------------------------------------------------------------- #
def test_business_date_is_timezone_independent():
    """1767110400000 = 东八区 2025-12-31 00:00，在 UTC 下 localtime 会给 20251230。"""
    assert xs._business_date(1767110400000) == "20251231"
    assert xs._business_date(1782748800000) == "20260630"


@pytest.mark.parametrize("bad", [None, 0, "", "20251231"])
def test_business_date_rejects_non_timestamps(bad):
    assert xs._business_date(bad) is None


def test_income_period_key_overrides_adapter_localtime(monkeypatch):
    """adapter 的 end_date 必须被业务时区的重算覆盖。"""
    _install_client(monkeypatch, FakeClient(financial={"list": [{}]}))
    _install_fake_library(
        monkeypatch,
        # 模拟 UTC 容器里 adapter 的输出：偏了一天
        financial_rows=lambda data: [
            {"report_date": 1767110400000, "end_date": "20251230", "period_key": "20251230"}
        ],
    )
    row = xs.fetch_income_rows("600519", "A股")[0]
    assert row["end_date"] == "20251231"
    assert row["period_key"] == "20251231"


# --------------------------------------------------------------------------- #
# 十大股东：period_key 必须带报告期，不能是 holder_name
# --------------------------------------------------------------------------- #
_HOLDERS_RESPONSE = {
    "times": [
        {"name": "2026中报", "value": 1782748800000},
        {"name": "2026一季报", "value": 1774886400000},
    ],
    "items": [
        {"holder_name": "中国贵州茅台酒厂(集团)有限责任公司", "held_num": 681282935,
         "held_ratio": 54.5, "chg": 0.0},
        {"holder_name": "香港中央结算有限公司", "held_num": 100, "held_ratio": 1.0, "chg": -0.5},
    ],
}


def test_holder_rows_key_is_period_and_rank(monkeypatch):
    _install_client(monkeypatch, FakeClient(top_holders=_HOLDERS_RESPONSE))
    rows = xs.fetch_holder_rows("600519", "A股")

    assert [r["period_key"] for r in rows] == ["20260630|01", "20260630|02"]
    assert [r["holder_rank"] for r in rows] == [1, 2]
    assert rows[0]["report_date"] == "20260630"
    assert rows[0]["report_name"] == "2026中报"
    assert rows[0]["holder_name"] == "中国贵州茅台酒厂(集团)有限责任公司"
    # 名次入键、姓名入 payload：同一期重同步幂等，且退出前十不会残留成孤儿行
    assert all(len(r["period_key"]) <= 40 for r in rows)


def test_holder_rows_never_fall_back_to_holder_name_key(monkeypatch):
    """缺报告期时必须整集失败，不能退回 holder_name 作键。"""
    _install_client(monkeypatch, FakeClient(top_holders={"items": _HOLDERS_RESPONSE["items"]}))
    with pytest.raises(xs.XueqiuUnavailable) as excinfo:
        xs.fetch_holder_rows("600519", "A股")
    assert "报告期" in str(excinfo.value)


def test_holder_rows_reject_unsupported_market(monkeypatch):
    _install_client(monkeypatch, FakeClient())
    with pytest.raises(xs.XueqiuUnavailable):
        xs.fetch_holder_rows("00700", "港股")


def test_capital_flow_uses_business_timezone(monkeypatch):
    """资金流直接读原始 items：adapter 不保留 timestamp，套用后就算不回去了。"""
    _install_client(
        monkeypatch,
        FakeClient(capital_history={"items": [{"timestamp": 1767110400000, "amount": -12.5}]}),
    )
    rows = xs.fetch_capital_flow_rows("600519", "A股")
    assert rows == [{"date": "20251231", "amount": -12.5, "period_key": "20251231"}]


def test_datasets_without_cookie_raise_rather_than_return_empty(monkeypatch):
    monkeypatch.setattr(xs.settings, "xueqiu_cookies", "")
    monkeypatch.setattr(xs.settings, "xueqiu_cookie_file", "")
    with pytest.raises(xs.XueqiuUnavailable):
        xs.fetch_income_rows("600519", "A股")


# ---------------------------------------------------------------------------
# 私有库未安装：公开发布的快照不带 xueqiu-market，所有入口须显式降级
# ---------------------------------------------------------------------------


def _block_library(monkeypatch):
    """sys.modules 里放 None：import 语句立即抛 ImportError（模拟未安装）。"""
    monkeypatch.setitem(sys.modules, "xueqiu_market", None)


def test_missing_library_symbol_conversion_falls_back_to_same_rules(monkeypatch):
    """匹配键只是字符串格式化：库缺失时按库同样的规则本地兜底，观点匹配不失明。"""
    _block_library(monkeypatch)
    assert xs.to_xueqiu("600519", "A股") == "SH600519"  # A/B 股本来就不依赖库
    assert xs.to_xueqiu("700", "港股") == "00700"
    assert xs.to_xueqiu("aapl", "美股") == "AAPL"


def test_missing_library_quote_degrades_not_raises(monkeypatch):
    _block_library(monkeypatch)
    monkeypatch.setattr(xs.settings, "xueqiu_cookies", '{"xq_a_token": "t"}')
    result = xs.fetch_xueqiu_stock_price("600519", Market.A_STOCK)
    assert result["success"] is False and "未安装" in result["error"]
    assert xs.probe() == {"ok": False, "detail": result["error"]}


def test_missing_library_datasets_raise_unavailable(monkeypatch):
    _block_library(monkeypatch)
    monkeypatch.setattr(xs.settings, "xueqiu_cookies", '{"xq_a_token": "t"}')
    for fetch in (xs.fetch_income_rows, xs.fetch_capital_flow_rows, xs.fetch_holder_rows):
        with pytest.raises(xs.XueqiuUnavailable, match="未安装"):
            fetch("600519", "A股")


def test_missing_library_without_cookie_is_still_the_cookie_message(monkeypatch):
    """没配 Cookie 时不该去碰库：降级信息说的是 Cookie，而不是库。"""
    _block_library(monkeypatch)
    monkeypatch.setattr(xs.settings, "xueqiu_cookies", "")
    monkeypatch.setattr(xs.settings, "xueqiu_cookie_file", "")
    assert xs.get_client() is None
    with pytest.raises(xs.XueqiuUnavailable, match="Cookie"):
        xs.fetch_income_rows("600519", "A股")
