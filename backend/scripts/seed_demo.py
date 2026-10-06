"""演示库灌数（README 截图与演示视频用）。

只写**库名含 demo** 的数据库，且先清空再写——不会碰开发库或生产库。所有账本数据
（账户、交易、分红、资金流水、对账快照、自选）与行情/汇率都是**虚构**的：代码借用常见
标的以便读者认得，价格路径由固定种子生成、首尾贴近真实量级，与真实走势无关。

    DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/investment_demo \\
        python scripts/seed_demo.py [--keep-research]

账本经 HTTP API 写入（进程内 TestClient，走与界面相同的校验与持仓重算），全局参考数据
（行情、汇率、指数、行业、事件）直接写表。标的详情页的档案/公告/AI 分析不在这里造：
那些内容描述的是真实公司，只能由系统从公开数据源真实抓取与生成，见 docs/media/README.md。
"""

from __future__ import annotations

import math
import os
import random
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

DEMO_USERNAME = "demo"
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "demo-password")

# 进程内调用 API：不起 worker、不跑周期任务、不带任何外部凭证
os.environ.setdefault("SECRET_KEY", "demo-secret-key")
os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "demo-admin-password")
os.environ.setdefault("DEMO_INITIAL_PASSWORD", "demo-unused-password")
for _flag in ("BACKGROUND_WORKER_ENABLED", "PERIODIC_TASKS_ENABLED", "QUOTE_AUTO_REFRESH_ENABLED"):
    os.environ[_flag] = "false"
os.environ["EVENT_NOTIFICATIONS_ENABLED"] = "false"
os.environ["REQUIRE_HTTPS"] = "false"
for _secret in ("LLM_REPORT_API_KEY", "TUSHARE_TOKEN", "TIINGO_API_TOKEN", "XUEQIU_COOKIES"):
    os.environ[_secret] = ""

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, make_url, text  # noqa: E402

from app.core.security import get_password_hash  # noqa: E402
from app.core.timeutil import local_today  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    ExchangeRate,
    Holding,
    SecurityEvent,
    SecurityPrice,
    User,
    WatchlistItem,
)
from app.models.reference_rate import ReferenceRate  # noqa: E402
from app.models.security_industry import SecurityIndustry  # noqa: E402

END = local_today()
START = END - timedelta(days=730)

# (代码, 市场, 名称, 币种, 起点价, 终点价, 日波动)
SECURITIES = [
    ("600519", "A股", "贵州茅台", "CNY", 1510, 1452, 0.016),
    ("000333", "A股", "美的集团", "CNY", 61.5, 76.2, 0.018),
    ("600900", "A股", "长江电力", "CNY", 27.6, 28.4, 0.011),
    ("510300", "A股", "沪深300ETF", "CNY", 3.92, 4.63, 0.012),
    ("00700", "港股", "腾讯控股", "HKD", 382, 641, 0.020),
    ("00883", "港股", "中国海洋石油", "HKD", 17.6, 19.9, 0.019),
    ("AAPL", "美股", "Apple Inc.", "USD", 226, 254, 0.017),
    ("MSFT", "美股", "Microsoft Corp.", "USD", 416, 511, 0.015),
    # 只在观察清单里
    ("601899", "A股", "紫金矿业", "CNY", 15.2, 26.4, 0.022),
    ("09988", "港股", "阿里巴巴-W", "HKD", 86, 168, 0.026),
    ("NVDA", "美股", "NVIDIA Corp.", "USD", 121, 182, 0.030),
]
INDICES = [
    ("000300.SH", 3890, 4610, 0.012),
    ("HSI", 20100, 26400, 0.015),
    ("SPX", 5720, 6660, 0.010),
]
INDUSTRIES = {
    ("600519", "A股"): "白酒",
    ("000333", "A股"): "家用电器",
    ("600900", "A股"): "水力发电",
    ("00700", "港股"): "互联网服务",
    ("00883", "港股"): "石油开采",
    ("AAPL", "美股"): "电子计算机",
    ("MSFT", "美股"): "软件服务",
    ("601899", "A股"): "黄金",
    ("09988", "港股"): "互联网零售",
    ("NVDA", "美股"): "半导体",
}


def trading_days(start: date, end: date) -> list[date]:
    days, day = [], start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def price_path(seed: str, days: list[date], start: float, end: float, vol: float) -> dict:
    """首尾固定的对数布朗桥：可复现、量级贴近真实，但与真实走势无关。"""
    rng = random.Random(seed)
    # 桥的偏离约为 vol·√n/2：按真实日波动的一半取，免得两年里凭空翻倍再腰斩
    steps = [rng.gauss(0, vol * 0.5) for _ in days]
    walk, total = [], 0.0
    for step in steps:
        total += step
        walk.append(total)
    n = len(days) - 1
    drift = math.log(end / start)
    closes = {}
    for i, day in enumerate(days):
        frac = i / n
        bridge = walk[i] - frac * walk[-1]
        closes[day] = start * math.exp(drift * frac + bridge)
    return closes


def q(value, places="0.01") -> Decimal:
    return Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP)


# 标的研究数据（demo_research.py 真实抓取生成，有费用）：--keep-research 时保留
RESEARCH_TABLES = {
    "security_profile_data",
    "security_analyses",
    "security_announcements",
    "scheduled_task_state",
}


def reset_database(url: str, keep_research: bool) -> None:
    name = make_url(url).database or ""
    if "demo" not in name:
        raise SystemExit(f"拒绝写入：库名 {name!r} 不含 demo（本脚本会清空整库）")
    engine = create_engine(url)
    with engine.begin() as conn:
        tables = [
            row[0]
            for row in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            if row[0] != "alembic_version" and not (keep_research and row[0] in RESEARCH_TABLES)
        ]
        if tables:
            quoted = ", ".join(f'"{t}"' for t in tables)
            conn.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))
    engine.dispose()


def seed_reference_data(days: list[date]) -> dict:
    closes: dict[tuple[str, str], dict[date, float]] = {}
    db = SessionLocal()
    try:
        for symbol, market, _name, currency, start, end, vol in SECURITIES:
            path = price_path(f"{symbol}:{market}", days, start, end, vol)
            closes[(symbol, market)] = path
            prev = None
            for day, close in path.items():
                db.add(
                    SecurityPrice(
                        symbol=symbol,
                        market=market,
                        price_date=day,
                        currency=currency,
                        open_price=q(prev or close, "0.0001"),
                        high_price=q(max(prev or close, close) * 1.006, "0.0001"),
                        low_price=q(min(prev or close, close) * 0.994, "0.0001"),
                        close_price=q(close, "0.0001"),
                        pre_close_price=q(prev, "0.0001") if prev else None,
                        source="demo",
                    )
                )
                prev = close
        for code, start, end, vol in INDICES:
            for day, close in price_path(code, days, start, end, vol).items():
                db.add(
                    SecurityPrice(
                        symbol=code,
                        market="指数",
                        price_date=day,
                        currency="CNY" if code.endswith(".SH") else "HKD",
                        close_price=q(close),
                        source="demo",
                    )
                )
        for currency, start, end in (("USD", 7.128, 7.103), ("HKD", 0.9152, 0.9127)):
            for day, rate in price_path(f"fx:{currency}", days, start, end, 0.0015).items():
                db.add(
                    ExchangeRate(
                        from_currency=currency,
                        to_currency="CNY",
                        rate=q(rate, "0.000001"),
                        effective_date=day,
                        source="cfets-ccpr",
                        is_active=True,
                    )
                )
        for day, value in price_path("SHIBOR_3M", days, 1.92, 1.58, 0.004).items():
            db.add(
                ReferenceRate(
                    series="SHIBOR_3M", rate_date=day, value=q(value, "0.0001"), source="demo"
                )
            )
        now = datetime.now(timezone.utc)
        for (symbol, market), industry in INDUSTRIES.items():
            db.add(
                SecurityIndustry(
                    symbol=symbol,
                    market=market,
                    source="tushare" if market == "A股" else "eastmoney",
                    industry=industry,
                    fetched_at=now,
                )
            )
        # 近期事件：持仓页/仪表盘的事件徽标
        for symbol, market, event_type, offset, payload in (
            ("600519", "A股", "EARNINGS_DISCLOSURE", 22, {"report": "2026 三季报"}),
            ("000333", "A股", "EARNINGS_DISCLOSURE", 30, {"report": "2026 三季报"}),
            ("00883", "港股", "DIVIDEND_PLAN", 12, {"cash_div": 0.73, "currency": "HKD"}),
        ):
            db.add(
                SecurityEvent(
                    symbol=symbol,
                    market=market,
                    event_type=event_type,
                    event_date=END + timedelta(days=offset),
                    source="demo",
                    payload=payload,
                )
            )
        db.commit()
    finally:
        db.close()
    return closes


def create_user() -> None:
    db = SessionLocal()
    try:
        db.add(
            User(
                username=DEMO_USERNAME,
                hashed_password=get_password_hash(DEMO_PASSWORD),
                is_active=True,
                is_admin=False,
            )
        )
        db.commit()
    finally:
        db.close()


class Api:
    def __init__(self) -> None:
        self.client = TestClient(app)
        token = self.call(
            "post", "/api/auth/token", json={"username": DEMO_USERNAME, "password": DEMO_PASSWORD}
        )["access_token"]
        self.headers = {"Authorization": f"Bearer {token}"}

    def call(self, method: str, path: str, **kwargs):
        response = getattr(self.client, method)(
            path, headers=getattr(self, "headers", None), **kwargs
        )
        if response.status_code >= 400:
            raise SystemExit(f"{method.upper()} {path} -> {response.status_code}: {response.text}")
        return response.json() if response.content else None


def close_on(closes: dict, day: date) -> float:
    while day not in closes:
        day -= timedelta(days=1)
    return closes[day]


def business_day(offset_days: int) -> date:
    day = START + timedelta(days=offset_days)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def seed_ledger(api: Api, closes: dict) -> dict:
    names = {(s, m): (n, c) for s, m, n, c, *_ in SECURITIES}
    a_share = api.call(
        "post",
        "/api/broker-accounts",
        json={
            "broker": "示例证券",
            "account_name": "示例证券 · A股",
            "account_number_masked": "****2468",
            "base_currency": "CNY",
        },
    )
    intl = api.call(
        "post",
        "/api/broker-accounts",
        json={
            "broker": "IBKR",
            "account_name": "IBKR · 港美股",
            "account_number_masked": "U***1357",
            "base_currency": "USD",
        },
    )
    accounts = {"a": a_share["id"], "intl": intl["id"]}

    cash = [
        ("a", "DEPOSIT", 800000, "CNY", 0, "入金"),
        ("intl", "DEPOSIT", 120000, "USD", 2, "入金"),
        ("intl", "FX_OUT", 40000, "USD", 24, "换汇：美元 → 港元"),
        ("intl", "FX_IN", 312400, "HKD", 24, "换汇：美元 → 港元"),
        ("intl", "FX_OUT", 15000, "USD", 295, "换汇：美元 → 港元"),
        ("intl", "FX_IN", 117500, "HKD", 295, "换汇：美元 → 港元"),
        ("a", "INTEREST", 312.45, "CNY", 450, "账户利息"),
        ("a", "WITHDRAWAL", 50000, "CNY", 600, "出金"),
    ]
    for account, event_type, amount, currency, offset, note in cash:
        api.call(
            "post",
            "/api/cash-events",
            json={
                "broker_account_id": accounts[account],
                "event_type": event_type,
                "amount": str(amount),
                "currency": currency,
                "event_date": business_day(offset).isoformat(),
                "notes": note,
            },
        )

    # (账户, 代码, 市场, 方向, 数量, 距起点天数)
    trades = [
        ("a", "510300", "A股", "BUY", 20000, 1),
        ("a", "600519", "A股", "BUY", 100, 3),
        ("a", "000333", "A股", "BUY", 2000, 35),
        ("intl", "00700", "港股", "BUY", 400, 38),
        ("a", "600900", "A股", "BUY", 3000, 62),
        ("intl", "AAPL", "美股", "BUY", 80, 66),
        ("intl", "00883", "港股", "BUY", 6000, 128),
        ("intl", "MSFT", "美股", "BUY", 40, 150),
        ("a", "600519", "A股", "BUY", 100, 185),
        ("a", "600900", "A股", "BUY", 2000, 250),
        ("intl", "00700", "港股", "BUY", 200, 300),
        ("a", "000333", "A股", "SELL", 1000, 352),
        ("intl", "AAPL", "美股", "SELL", 30, 460),
        ("a", "600519", "A股", "SELL", 50, 520),
        ("a", "510300", "A股", "SELL", 10000, 590),
        ("intl", "MSFT", "美股", "BUY", 20, 650),
    ]
    for account, symbol, market, side, qty, offset in trades:
        day = business_day(offset)
        name, currency = names[(symbol, market)]
        price = close_on(closes[(symbol, market)], day)
        amount = price * qty
        if market == "A股":
            fee = max(5, amount * 0.00025) + (amount * 0.0005 if side == "SELL" else 0)
        elif market == "港股":
            fee = max(18, amount * 0.0013)
        else:
            fee = max(1, qty * 0.005)
        api.call(
            "post",
            "/api/transactions",
            json={
                "broker_account_id": accounts[account],
                "symbol": symbol,
                "name": name,
                "market": market,
                "transaction_type": side,
                "quantity": str(qty),
                "price": str(q(price, "0.001")),
                "fee": str(q(fee)),
                "transaction_date": day.isoformat(),
                "currency": currency,
            },
        )

    # (账户, 代码, 市场, 每股, 持股, 距起点天数, 税率)
    dividends = [
        ("intl", "AAPL", "美股", 0.25, 80, 125, 0.10),
        ("intl", "00700", "港股", 4.50, 400, 220, 0.0),
        ("intl", "MSFT", "美股", 0.83, 40, 225, 0.10),
        ("intl", "00883", "港股", 0.75, 6000, 245, 0.0),
        ("a", "600519", "A股", 27.67, 200, 262, 0.0),
        ("a", "600900", "A股", 0.70, 5000, 283, 0.0),
        ("intl", "MSFT", "美股", 0.83, 40, 323, 0.10),
        ("intl", "AAPL", "美股", 0.26, 80, 314, 0.10),
    ]
    for account, symbol, market, dps, shares, offset, tax_rate in dividends:
        day = business_day(offset)
        name, currency = names[(symbol, market)]
        total = q(dps * shares)
        tax = q(float(total) * tax_rate)
        api.call(
            "post",
            "/api/corporate-actions",
            json={
                "broker_account_id": accounts[account],
                "symbol": symbol,
                "name": name,
                "market": market,
                "action_type": "CASH_DIVIDEND",
                "receipt_confirmed": True,
                "ex_date": day.isoformat(),
                "payment_date": (day + timedelta(days=7)).isoformat(),
                "dividend_per_share": str(dps),
                "total_dividend": str(total),
                "tax_withheld": str(tax),
                "net_dividend": str(total - tax),
                "currency": currency,
            },
        )

    for symbol, market, note, days_ago in (
        ("601899", "A股", "铜金量价齐升，关注资本开支节奏", 75),
        ("09988", "港股", "云业务增速拐点，等回购落地", 120),
        ("NVDA", "美股", "数据中心需求能否延续到明年", 40),
    ):
        api.call(
            "post",
            "/api/watchlist",
            json={
                "symbol": symbol,
                "market": market,
                "name": names[(symbol, market)][0],
                "note": note,
            },
        )
        _backdate_watchlist(symbol, market, days_ago, closes)
    return accounts


def _backdate_watchlist(symbol: str, market: str, days_ago: int, closes: dict) -> None:
    added = END - timedelta(days=days_ago)
    while added.weekday() >= 5:
        added -= timedelta(days=1)
    db = SessionLocal()
    try:
        item = db.query(WatchlistItem).filter_by(symbol=symbol, market=market).one()
        item.created_at = datetime.combine(added, time(10, 0), tzinfo=timezone.utc)
        item.added_price = q(close_on(closes[(symbol, market)], added), "0.0001")
        item.added_price_date = added
        item.added_price_basis = "close_on_add"
        db.commit()
    finally:
        db.close()


def mark_prices_current(closes: dict) -> None:
    """把持仓与自选的现价对齐到最新收盘（生产上由行情刷新任务写入）。"""
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        for model in (Holding, WatchlistItem):
            for row in db.query(model).all():
                series = closes.get((row.symbol, row.market))
                if not series:
                    continue
                last_day = max(series)
                row.current_price = q(series[last_day], "0.0001")
                row.price_as_of = last_day
                row.price_updated_at = now
                row.price_source = "demo"
        db.commit()
    finally:
        db.close()


def seed_reconciliation(api: Api, accounts: dict) -> None:
    holdings = api.call("get", "/api/holdings")
    for key, account_id in accounts.items():
        positions = [
            {"symbol": h["symbol"], "market": h["market"], "quantity": str(h["quantity"])}
            for h in holdings
            if h.get("broker_account_id") == account_id and Decimal(str(h["quantity"])) > 0
        ]
        snapshot = api.call(
            "post",
            "/api/reconciliation-snapshots",
            json={
                "broker_account_id": account_id,
                "snapshot_date": END.isoformat(),
                "source_filename": f"demo-statement-{key}.pdf",
                "positions": positions,
                "cash_balances": {},
            },
        )
        compared = api.call("post", f"/api/reconciliation-snapshots/{snapshot['id']}/compare")
        system_cash = _system_cash(compared.get("diff_detail") or {})
        api.call(
            "put",
            f"/api/reconciliation-snapshots/{snapshot['id']}",
            json={"cash_balances": {k: str(v) for k, v in system_cash.items()}},
        )
        api.call("post", f"/api/reconciliation-snapshots/{snapshot['id']}/compare")

    # 一份历史快照演示差异定位：现金对平，00700 券商比系统多 100 股（系统漏记一笔买入）
    as_of = END - timedelta(days=90)
    old_positions = []
    for h in holdings:
        if h.get("broker_account_id") != accounts["intl"] or Decimal(str(h["quantity"])) <= 0:
            continue
        quantity = Decimal(str(h["quantity"]))
        if h["symbol"] == "MSFT":
            quantity = Decimal("40")  # 快照日之后才加仓 20 股
        if h["symbol"] == "00700":
            quantity += 100
        old_positions.append(
            {"symbol": h["symbol"], "market": h["market"], "quantity": str(quantity)}
        )
    old = api.call(
        "post",
        "/api/reconciliation-snapshots",
        json={
            "broker_account_id": accounts["intl"],
            "snapshot_date": as_of.isoformat(),
            "source_filename": "demo-statement-intl-q2.pdf",
            "positions": old_positions,
            "cash_balances": {},
        },
    )
    compared = api.call("post", f"/api/reconciliation-snapshots/{old['id']}/compare")
    api.call(
        "put",
        f"/api/reconciliation-snapshots/{old['id']}",
        json={
            "cash_balances": {
                k: str(v) for k, v in _system_cash(compared.get("diff_detail") or {}).items()
            }
        },
    )
    api.call("post", f"/api/reconciliation-snapshots/{old['id']}/compare")


def _system_cash(diff_detail: dict) -> dict:
    """比对结果里系统推导的各币种现金（快照把它当券商余额填回去即对平）。"""
    return {row["currency"]: q(row["derived_balance"]) for row in diff_detail.get("cash") or []}


def main() -> None:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise SystemExit("请设置 DATABASE_URL（库名须含 demo）")
    reset_database(url, keep_research="--keep-research" in sys.argv)
    days = trading_days(START, END)
    closes = seed_reference_data(days)
    create_user()
    api = Api()
    accounts = seed_ledger(api, closes)
    mark_prices_current(closes)
    seed_reconciliation(api, accounts)
    print(f"演示库已就绪：用户 {DEMO_USERNAME}，{START} → {END}，{len(days)} 个交易日")


if __name__ == "__main__":
    main()
