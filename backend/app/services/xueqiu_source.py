"""雪球数据源（stock.xueqiu.com）薄 wrapper —— 所有雪球逻辑集中于此。

对外只暴露本项目既有契约：行情返回 `PriceResult`，基本面返回 profile 行
（`list[dict]`，每行带 `period_key`）。雪球 symbol 格式（SH600519 / 00700 /
AAPL）只存在于本模块内部，边界一律经 `to_xueqiu` 转换——本项目主键是
「裸码 + 中文市场」，雪球 symbol 绝不落库。

四条硬约束：

1. **Cookie 由调用方供给**：库不获取也不刷新 Cookie。xq_a_token 约 15 天过期，
   过期后库抛 ApiError / WafChallengeError。未配置 Cookie 时本模块所有入口
   显式降级（行情返回 success=False，数据集抛 XueqiuUnavailable），绝不返回
   空列表冒充成功——那会让"没配 Cookie"和"公司没披露"在下游长得一模一样。
2. **单例 + 全局锁**：库的限速时钟（`_next_request_at`）与 `requests.Session`
   都不是线程安全的，而 `update_all_holdings_prices` 跑 `price_refresh_max_workers`
   个线程。共享一个 client 限速才生效，共享就必须串行化——`_call_lock` 覆盖
   每一次调用（含库内部 2-4s 的限速 sleep）。
3. **接口的市场边界**：finance 与 f10 端点全部是 `/cn/` 口径（见库
   `endpoints.py`），只支持 A 股；港/美股财务本库并未覆盖。对应入口显式抛错，
   不静默返回空。行情 quote 则四个市场都有。
4. **A/B 股交易所前缀不用库的推断**：库的 `infer_a_exchange` 把 "9" 开头一律
   判 SH，而北交所 920xxx 也以 9 开头（它自己的 BJ 分支对 92 前缀永不可达），
   会生成不存在的 SH920599。这里改用本项目的 `get_exchange_type`。
"""

import json
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..config import settings
from ..core.logging import get_app_logger
from ..core.timeutil import business_timezone
from .stock_price_service import (
    Market,
    PriceResult,
    get_exchange_type,
    positive_decimal_price,
    price_result,
)

logger = get_app_logger(__name__)

QUOTE_SOURCE = "xueqiu-quote"

# 行情：雪球对 A/B/港/美股都有报价快照
QUOTE_MARKETS = frozenset({Market.A_STOCK, Market.B_STOCK, Market.HK_STOCK, Market.US_STOCK})
# 基本面：finance/f10 端点是 /cn/ 口径，只有 A 股。capital/* 虽未带 /cn/，但
# 未经非 A 股实测，同样保守限定——宁可显式降级，不可静默返回空。
FUNDAMENTAL_MARKETS = frozenset({Market.A_STOCK.value})

# 财务取数期数：Q4（年报）口径下 8 期 ≈ 八年，与 Tushare income 的 PROFILE_CAPS 对齐
FINANCIAL_COUNT = 8
# 资金流历史取数天数
CAPITAL_HISTORY_COUNT = 20

_client = None
_client_lock = threading.Lock()
_call_lock = threading.Lock()


class XueqiuUnavailable(RuntimeError):
    """雪球数据源不可用（未配置 Cookie / 市场不支持 / 上游错误）。"""


def _cookies_from_json(raw: str) -> Dict[str, str]:
    """把 `XUEQIU_COOKIES` 的 JSON 文本规整为 {name: value}。

    不能直接把解析结果丢给库的 `load_cookies`：它的 dict 分支排在 "cookies"
    键分支之前、对 dict 入参原样返回，于是 J2Team 形状的 dict 会变成一个名为
    "cookies" 的假 Cookie，请求照发但登录态为空。
    """
    data = json.loads(raw)
    if isinstance(data, dict) and "cookies" in data:
        data = data["cookies"]
    if isinstance(data, list):
        return {str(item["name"]): str(item["value"]) for item in data}
    if isinstance(data, dict):
        return {str(key): str(value) for key, value in data.items()}
    raise XueqiuUnavailable("XUEQIU_COOKIES 结构无法识别（需 {name: value} 或 J2Team 导出）")


def _import_library():
    """按需导入私有库 `xueqiu-market`；未安装时抛 XueqiuUnavailable（显式降级）。

    该库是私有仓依赖，公开发布的快照不带它：所有入口都经这里导入，缺库时行情返回
    success=False、数据集抛 XueqiuUnavailable、`to_xueqiu` 对港/美股抛错——与"未配置
    Cookie"同一套降级语义，绝不让 ImportError 冒成"未预期的错误"。"""
    try:
        import xueqiu_market
    except ImportError as exc:
        raise XueqiuUnavailable(
            "雪球库 xueqiu-market 未安装（私有依赖；未安装时雪球行情/A股雪球数据集不可用）"
        ) from exc
    return xueqiu_market


def _build_client():
    """按 settings 构造 client；未配置 Cookie 返回 None（调用方显式降级）。"""
    raw = (settings.xueqiu_cookies or "").strip()
    path = (settings.xueqiu_cookie_file or "").strip()
    if not raw and not path:
        return None
    library = _import_library()
    cookies = _cookies_from_json(raw) if raw else library.load_cookies(path)
    return library.XueqiuMarketClient(
        cookies=cookies,
        min_delay=settings.xueqiu_min_delay_seconds,
        max_delay=settings.xueqiu_max_delay_seconds,
        timeout=settings.xueqiu_timeout_seconds,
    )


def get_client():
    """模块级单例。未配置 Cookie 时返回 None，并且每次重试构造——构造不发请求，
    重试的代价是零，换来改配置后无需重启进程。"""
    global _client
    with _client_lock:
        if _client is None:
            _client = _build_client()
        return _client


def reset_client() -> None:
    """丢弃单例（测试与轮换 Cookie 后用）。"""
    global _client
    with _client_lock:
        _client = None


def is_configured() -> bool:
    return bool((settings.xueqiu_cookies or "").strip() or (settings.xueqiu_cookie_file or "").strip())


def _call(method: str, *args: Any, **kwargs: Any) -> Any:
    """串行执行一次雪球调用。

    锁覆盖库内部 2-4s 的限速 sleep，因此并发线程会在此排队——这是共享限速
    时钟的必然代价，也正是不让批量刷新绕过限速的手段。
    """
    client = get_client()
    if client is None:
        raise XueqiuUnavailable("未配置雪球 Cookie（XUEQIU_COOKIES / XUEQIU_COOKIE_FILE）")
    with _call_lock:
        return getattr(client, method)(*args, **kwargs)


def to_xueqiu(symbol: str, market: str) -> str:
    """裸码 + 中文市场 -> 雪球 symbol。

    A/B 股的交易所前缀走本项目的 `get_exchange_type`（见模块 docstring 第 4 条），
    其余市场交给库：港股补零到 5 位，美股 ticker 原样。
    """
    text = str(symbol or "").strip().upper()
    if market in (Market.A_STOCK.value, Market.B_STOCK.value) and text.isdigit():
        return f"{get_exchange_type(text).upper()}{text}"

    # 港股补零到 5 位 / 美股 ticker 原样：交给库，保持单一事实来源；库未安装
    # （公开发布的快照）时按库同样的规则本地兜底——匹配键只是字符串格式化，
    # 观点匹配（读同库归档表）不该因为缺一个私有包而对港/美股全部失明
    try:
        library = _import_library()
    except XueqiuUnavailable:
        if market == Market.HK_STOCK.value:
            return "".join(ch for ch in text if ch.isdigit()).zfill(5)
        return text
    return library.to_xueqiu_symbol(text, market)


def fetch_xueqiu_stock_price(symbol: str, market: Market) -> PriceResult:
    """雪球实时报价 -> PriceResult。任何失败都返回 success=False，绝不抛出。

    批量刷新在 ThreadPoolExecutor 里跑，异常冒泡会让该标的记成"未预期的错误"
    而丢掉 provider 归因，因此这里兜住全部异常（库的 XueqiuError 家族、网络、
    JSON 解析）。
    """
    if market not in QUOTE_MARKETS:
        return price_result(
            price=None,
            source=QUOTE_SOURCE,
            success=False,
            error=f"雪球行情不支持市场类型: {market.value}",
        )
    try:
        snapshot = _call("quote", to_xueqiu(symbol, market.value)) or {}
        raw = _import_library().quote_to_price_result(
            snapshot.get("quote") or {}, source=QUOTE_SOURCE
        )
        if not raw.get("success"):
            raise ValueError(raw.get("error") or "雪球未返回报价")
        # adapter 只判 price is not None；本项目要求正有限价——停牌/异常标的
        # 的 current 可能是 0 或负，那在 adapter 眼里是"成功"。
        price = positive_decimal_price(raw["price"])
        logger.info("✓ 雪球行情 %s %s 成功: %s", market.value, symbol, price)
        return price_result(price=price, source=QUOTE_SOURCE, success=True)
    except Exception as exc:
        error_msg = f"雪球行情获取失败: {str(exc)[:200]}"
        logger.warning("  %s %s %s", market.value, symbol, error_msg)
        return price_result(price=None, source=QUOTE_SOURCE, success=False, error=error_msg)


def _require_fundamental_market(market: str, dataset: str) -> None:
    if market not in FUNDAMENTAL_MARKETS:
        raise XueqiuUnavailable(
            f"雪球 {dataset} 数据集仅支持 A股（上游端点为 /cn/ 口径），当前市场：{market}"
        )


def _business_date(milliseconds: Any) -> Optional[str]:
    """雪球的毫秒时间戳 -> 业务时区的 YYYYMMDD。

    **不能用库 adapter 算出来的日期**：它走 `time.localtime`，即进程系统时区。
    生产后端容器是 UTC，而雪球的报告期是东八区的当日零点，于是 2025 年报的
    1767110400000 在容器里会算成 20251230——每个 period_key 都偏一天。
    period_key 是身份键，偏一天不只是显示难看：换一次部署时区，同一个报告期
    就会以两个键各存一份。core/timeutil 的第 1 条铁律就是为这类事写的。
    """
    if not isinstance(milliseconds, (int, float)) or not milliseconds:
        return None
    moment = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    return moment.astimezone(business_timezone()).strftime("%Y%m%d")


def fetch_income_rows(symbol: str, market: str) -> List[Dict[str, Any]]:
    """利润表 -> profile 行（每行含 end_date / period_key）。

    [值, 同比] 二元组的拆分交给库的 adapter（那是它的本职），只把它派生的
    日期字段按业务时区重算——理由见 `_business_date`。原始 `report_date`(ms)
    被 adapter 当标量原样保留，所以这里能重算而不必二次解析报表。
    """
    _require_fundamental_market(market, "xueqiu_income")
    data = _call("financial", to_xueqiu(symbol, market), "income", count=FINANCIAL_COUNT)
    rows = _import_library().financial_rows(data or {})
    for row in rows:
        day = _business_date(row.get("report_date"))
        if day:
            row["end_date"] = day
            row["period_key"] = day
    return rows


def fetch_capital_flow_rows(symbol: str, market: str) -> List[Dict[str, Any]]:
    """资金流历史 -> profile 行（每行一日主力净额，period_key 为交易日）。

    直接读原始 items 而不过库的 `capital_flow_rows`：该 adapter 只做两件事
    ——取 amount、把 timestamp 转成日期，而后者正是必须按业务时区重算的那步，
    且它不保留原始 timestamp，套用后就再也算不回去了。
    """
    _require_fundamental_market(market, "xueqiu_capital_flow")
    data = _call("capital_history", to_xueqiu(symbol, market), count=CAPITAL_HISTORY_COUNT) or {}
    rows = []
    for item in data.get("items") or []:
        day = _business_date(item.get("timestamp"))
        rows.append({"date": day, "amount": item.get("amount"), "period_key": day})
    return rows


def fetch_holder_rows(symbol: str, market: str) -> List[Dict[str, Any]]:
    """十大流通股东 -> profile 行（period_key = 报告期|名次）。

    不走库的 `holder_rows`：它把 period_key 设成 holder_name，而本项目的身份键
    是 (symbol, market, dataset, period_key)。那样同一标的只存得住最新一期——
    下季度同步会就地覆盖每个股东，退出前十的则永久残留且与在册者无法区分；
    `_key_of` 还会把键截到 40 字，两个前缀相同的基金全称直接撞车。

    报告期不在行里，在响应顶层的 `times[0]`（= 本次 items 所属的期）；名次即
    items 的顺序（按持股数降序）。两者组合后每期恰好十行、重同步幂等、长度恒定。
    """
    _require_fundamental_market(market, "xueqiu_holders")
    data = _call("top_holders", to_xueqiu(symbol, market)) or {}
    periods = data.get("times") or []
    period = periods[0] if periods else {}
    report_date = _business_date(period.get("value"))
    if not report_date:
        # 没有报告期就构造不出幂等的自然键。宁可整集失败，也不能退回
        # holder_name 作键——那会把上面那些覆盖/残留问题悄悄带进库里。
        raise XueqiuUnavailable("雪球十大股东响应缺少报告期（times），无法构造幂等自然键")
    rows = []
    for rank, item in enumerate(data.get("items") or [], start=1):
        rows.append({
            "report_date": report_date,
            "report_name": period.get("name"),
            "holder_rank": rank,
            "holder_name": item.get("holder_name"),
            "held_num": item.get("held_num"),
            "held_ratio": item.get("held_ratio"),
            "chg": item.get("chg"),
            "period_key": f"{report_date}|{rank:02d}",
        })
    return rows


def probe() -> Dict[str, Any]:
    """探活：用一次最便宜的调用确认 Cookie 仍有效。

    返回 {ok, detail}；不抛出。供 scripts/check_xueqiu_cookie_expiry.py 与
    运维巡检使用——Cookie 过期是"接口全部失败"的最常见单点原因，而失败在
    profile 同步里只表现为逐数据集的 failed 条目，不探活就看不出根因。
    """
    if not is_configured():
        return {"ok": False, "detail": "未配置雪球 Cookie"}
    result = fetch_xueqiu_stock_price("600519", Market.A_STOCK)
    if result["success"]:
        return {"ok": True, "detail": f"探活成功，贵州茅台报价 {result['price']}"}
    return {"ok": False, "detail": result["error"] or "未知错误"}
