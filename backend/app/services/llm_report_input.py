"""LLM 复盘报告的输入压缩层（产品目的③）。

只做字段裁剪与数组封顶，不做任何新计算——全部数据来自既有服务函数：
build_portfolio_snapshot / calculate_performance_analytics / get_statistics_by_time。
权益仓与实验口径的标记（calculation_status / methodology_notes / status 等）
按 CLAUDE.md 约定逐字保留，由 meta.estimate_semantics 向模型解释含义。
"""

import json
from typing import Any, Dict, List

from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from .statistics import (
    build_portfolio_snapshot,
    calculate_performance_analytics,
    get_statistics_by_time,
)

# 序列化字符预算（紧凑 JSON，中英混排 ≈ 10–15k token）
CHAR_BUDGET = 40_000

# 一级 / 二级（超预算时）数组封顶
PRIMARY_CAPS = {"holdings": 30, "realized": 15, "dividends": 10, "monthly": 24, "curve": 36}
SHRUNK_CAPS = {"holdings": 20, "realized": 10, "dividends": 10, "monthly": 12, "curve": 24}

# 报告附带的基准指数（字符预算考量取 2 个：本土主基准 + 美股参照）
LLM_BENCHMARKS = ["000300.SH", "SPX"]

ESTIMATE_SEMANTICS = {
    "exact + invested_securities_only/权益仓口径": (
        "账户级收益为权益仓口径：仅统计投入证券的资金，口径内精确；"
        "账户闲置现金与外部出入金按设计不计入、不稀释收益率。转述时须说明口径，不得当作全账户收益。"
    ),
    "experimental/实验": "该指标为实验性统计（如按笔胜率、TTWR、风险指标），仅供参考。",
    "methodology_notes": "计算方法的口径说明，转述相关数字时必须一并说明。",
    "stale": "价格早于 7 天（含手工维护价），据其估值的市值可能过时。",
    "benchmark/基准": (
        "analytics.benchmarks 为基准指数数据：价格指数原币收益率（不含股息、"
        "不折汇），excess_return_rate 为算术差（百分点），仅供相对参考。"
        "alignment=first_available 表示基准数据晚于组合区间起点、计量区间不一致，"
        "此时 comparison 为空，不得自行相减推算超额收益。"
    ),
}


def _month_end_downsample(curve: List[Dict[str, Any]], cap: int) -> List[Dict[str, Any]]:
    """月末点降采样：保留每月最后一个点，首末必留，最多 cap 个点。"""
    if not curve:
        return []
    points = []
    for index, point in enumerate(curve):
        is_last = index == len(curve) - 1
        next_month = curve[index + 1]["date"][:7] if not is_last else None
        if index == 0 or is_last or point["date"][:7] != next_month:
            points.append({
                "date": point["date"],
                "cumulative_return": point.get("cumulative_return_rate"),
            })
    if len(points) > cap:
        # 均匀抽稀中段，首末保留
        step = (len(points) - 2) / (cap - 2)
        kept = [points[0]] + [points[1 + int(i * step)] for i in range(cap - 2)] + [points[-1]]
        points = kept
    return points


def _compact(snapshot: Dict, analytics: Dict, monthly: List[Dict], caps: Dict[str, int]) -> Dict:
    performance = snapshot.get("performance", {})
    current = performance.get("current_performance", {})
    total_mv = float(current.get("current_market_value_cny") or 0)

    holdings_detail = sorted(
        current.get("holdings_detail", []),
        key=lambda h: h.get("market_value_cny", 0),
        reverse=True,
    )
    holdings = [
        {
            "symbol": h["symbol"],
            "name": h.get("name"),
            "market": h["market"],
            "quantity": h.get("quantity"),
            "current_price": h.get("current_price"),
            "market_value_cny": h.get("market_value_cny"),
            "unrealized_pnl_cny": h.get("unrealized_pnl_cny"),
            "unrealized_pnl_rate": h.get("unrealized_pnl_rate"),
            "weight_pct": round(h.get("market_value_cny", 0) / total_mv * 100, 2)
            if total_mv > 0
            else None,
        }
        for h in holdings_detail[: caps["holdings"]]
    ]
    tail = holdings_detail[caps["holdings"]:]
    if tail:
        holdings.append({
            "name": f"其他{len(tail)}只合计",
            "market_value_cny": round(sum(h.get("market_value_cny", 0) for h in tail), 2),
            "unrealized_pnl_cny": round(sum(h.get("unrealized_pnl_cny", 0) for h in tail), 2),
        })

    realized = performance.get("realized_pnl", {})
    realized_top = sorted(
        realized.get("trades_detail", []),
        key=lambda t: abs(t.get("realized_pnl_cny", 0)),
        reverse=True,
    )[: caps["realized"]]
    realized_compact = {
        "realized_pnl_cny": realized.get("realized_pnl_cny"),
        "sold_cost_cny": realized.get("sold_cost_cny"),
        "realized_pnl_rate": realized.get("realized_pnl_rate"),
        "data_quality": realized.get("data_quality"),
        "top_symbols": [
            {
                "symbol": t["symbol"],
                "market": t.get("market"),
                "realized_pnl_cny": t.get("realized_pnl_cny"),
                "realized_pnl_rate": t.get("realized_pnl_rate"),
            }
            for t in realized_top
        ],
    }

    dividends = performance.get("dividend_summary", {})
    dividends_compact = {
        "total_dividend_gross_cny": dividends.get("total_dividend_gross_cny"),
        "total_tax_cny": dividends.get("total_tax_cny"),
        "total_dividend_net_cny": dividends.get("total_dividend_net_cny"),
        "missing_rate_currencies": dividends.get("missing_rate_currencies", []),
        "top_symbols": [
            {
                "symbol": d["symbol"],
                "name": d.get("name"),
                "total_net_cny": d.get("total_net_cny"),
                "count": d.get("count"),
            }
            for d in sorted(
                dividends.get("by_symbol", []),
                key=lambda d: d.get("total_net_cny", 0),
                reverse=True,
            )[: caps["dividends"]]
        ],
    }

    prices = snapshot.get("prices", {})
    price_quality = {
        "stale_keys": prices.get("stale_keys", [])[:20],
        "missing_keys": prices.get("missing_keys", [])[:20],
        "stale_count": len(prices.get("stale_keys", [])),
        "missing_count": len(prices.get("missing_keys", [])),
    }

    accounts = [
        {
            "account_name": account.get("account_name"),
            "broker": account.get("broker"),
            "latest_reconciliation": (
                {
                    "snapshot_date": rec.get("snapshot_date"),
                    "status": rec.get("status"),
                    "scopes": [
                        {"statement_scope": s.get("statement_scope"), "status": s.get("status")}
                        for s in rec.get("scopes", [])
                    ],
                }
                if (rec := account.get("latest_reconciliation"))
                else None
            ),
        }
        for account in snapshot.get("accounts", [])
    ]

    analytics_compact = {
        "date_range": analytics.get("date_range"),
        "calculation_level": analytics.get("calculation_level"),
        "metrics": analytics.get("metrics"),
        "trade_skill": analytics.get("trade_skill"),
        "range_summary": analytics.get("range_summary"),
        "methodology": analytics.get("methodology"),
        "warnings": (analytics.get("data_quality") or {}).get("warnings", [])[:20],
        "curve_month_end": _month_end_downsample(analytics.get("curve", []), caps["curve"]),
        # 基准指数：曲线同法月末降采样（点键名与组合曲线一致，零改动复用）
        "benchmarks": [
            {
                "code": block.get("code"),
                "name": block.get("name"),
                "status": block.get("status"),
                "alignment": block.get("alignment"),
                "alignment_gap_days": block.get("alignment_gap_days"),
                "fx_basis": block.get("fx_basis"),
                "return_basis": block.get("return_basis"),
                "total_return_rate": block.get("total_return_rate"),
                "comparison": block.get("comparison"),
                "curve_month_end": _month_end_downsample(
                    block.get("points", []), caps["curve"]
                ),
            }
            for block in analytics.get("benchmarks", [])
        ],
    }

    return {
        "meta": {
            "as_of": snapshot.get("generated_at"),
            "base_currency": snapshot.get("base_currency", "CNY"),
            "estimate_semantics": ESTIMATE_SEMANTICS,
        },
        "account_return": performance.get("account_return"),
        "holdings": holdings,
        "realized_pnl": realized_compact,
        "dividends": dividends_compact,
        "price_quality": price_quality,
        "markets": snapshot.get("markets", []),
        "recent_transactions": snapshot.get("recent_transactions", []),
        "accounts": accounts,
        "data_quality": {
            "warnings": (snapshot.get("data_quality") or {}).get("warnings", [])[:20],
        },
        "analytics": analytics_compact,
        "monthly": monthly[-caps["monthly"]:],
    }


def serialize_input(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def build_llm_report_input(db: Session, user_id: int) -> Dict[str, Any]:
    snapshot = build_portfolio_snapshot(db, user_id)
    analytics = calculate_performance_analytics(
        db, user_id, snapshot.get("prices", {}).get("map", {}),
        benchmarks=LLM_BENCHMARKS,
    )
    monthly = get_statistics_by_time(db, user_id, "month")

    payload = _compact(snapshot, analytics, monthly, PRIMARY_CAPS)
    if len(serialize_input(payload)) > CHAR_BUDGET:
        payload = _compact(snapshot, analytics, monthly, SHRUNK_CAPS)
    # 归一为 JSON 原生类型（Decimal/date 等）：payload 要落 JSON 列（llm_reports
    # .input_payload），SQLAlchemy 的 json 序列化不吃 Decimal——与 job store 的
    # jsonable_encoder 先例一致。
    return jsonable_encoder(payload)
