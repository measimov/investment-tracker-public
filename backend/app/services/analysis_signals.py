"""分析输入的服务端预计算信号（#265）：模型只引用、不自己算。

离线评测（2026-09，8 只留出标的双评审）显示，P3 提示词之后剩下的错误主要是模型自己算错：
漏计中期股息、ROE 写「数据不足」、下半年推算、存货升降方向写反、低基数的增速当信号。
这里把这些量在服务端按固定口径算好，每块带 `semantics`，数值带期间、币种与口径。

纯函数：不碰数据库、不取「今天」。取数在 `security_profile_service.load_signal_inputs`，
组装在 `security_analysis_jobs.build_analysis_input`。

原则（防过拟合，#265 计划）：
- 口径来自定义（报表科目、Tushare 字段语义、线上 prompt 已写明的标签条件），不针对任何标的；
- 阈值是带理由的具名常量；没有公认阈值的判断（如存贷双高）只给比例，不下结论；
- 缺数据就省略或给 None + 原因，**不猜**；币种不同的两期不做比较。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .graham_screen import total_debt
from .periods import consecutive_run, prior

# 低基数：科目占总资产不足 1% 时，其增速不作信号——与线上 prompt「相关科目占总资产不足 1% 的
# 增速差不算」同一定义（security_analysis_prompts.supplementary_rules）
LOW_BASE_SHARE = 0.01
# 逐年块最多给几年（输入预算；利润质量指标另有 8 年）
MAX_YEARS = 5
YI = 100_000_000

_A_SHARE_FP = {"1231": "FY", "0630": "H1", "0331": "Q1", "0930": "Q3"}
_BALANCE_ITEMS = (
    ("inventories", "存货"),
    ("accounts_receiv", "应收账款"),
    ("money_cap", "货币资金"),
    ("interest_bearing_debt", "有息负债"),
    ("st_borr", "短期借款"),
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _num(row: Optional[Dict[str, Any]], *fields: str) -> Optional[float]:
    for field in fields:
        value = (row or {}).get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def _yi(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value / YI, 2)


def _pct(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator in (None, 0):
        return None
    return round(numerator / denominator * 100, 1) + 0.0  # + 0.0：-0.0 → 0.0


def _growth(current: Optional[float], prior: Optional[float]) -> Optional[float]:
    """同比 %，按 (本期 − 上期) ÷ |上期| 计：两期都亏损时正数表示亏损收窄。上期为 0 或两期异号
    （亏损转盈等）时百分比没有意义，返回 None。"""
    if current is None or prior is None or prior == 0 or (current < 0) != (prior < 0):
        return None
    return round((current - prior) / abs(prior) * 100, 1) + 0.0


def _direction(current: Optional[float], prior: Optional[float]) -> Optional[str]:
    if current is None or prior is None:
        return None
    if current > prior:
        return "上升"
    if current < prior:
        return "下降"
    return "持平"


def _parse_date(value: Any) -> Optional[date]:
    text = str(value or "").replace("-", "")[:8]
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except (ValueError, IndexError):
        return None


def _same_currency(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return bool(a.get("currency")) and a.get("currency") == b.get("currency")


# ---------------------------------------------------------------------------
# 归一：三个市场的报表行 → 统一的期间行
# ---------------------------------------------------------------------------


def _period_row(end_date: str, fp: str, currency: Optional[str], **values: Any) -> Dict[str, Any]:
    return {"end_date": end_date, "fp": fp, "currency": currency, **values}


def normalize_a_share(datasets: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Tushare 各表按 end_date 合并。利润表/现金流量表是年初至今**累计**值（0630 = 上半年）。"""
    by_end: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for kind in ("income", "balancesheet", "cashflow", "fina_indicator"):
        for row in datasets.get(kind) or []:
            end = str(row.get("end_date") or "")
            if len(end) == 8 and end[4:] in _A_SHARE_FP:
                by_end.setdefault(end, {}).setdefault(kind, row)
    rows = []
    for end, parts in by_end.items():
        income, balance = parts.get("income"), parts.get("balancesheet")
        cashflow, indicator = parts.get("cashflow"), parts.get("fina_indicator")
        rows.append(
            _period_row(
                end,
                _A_SHARE_FP[end[4:]],
                "CNY",  # Tushare A股 报表按构造为人民币
                revenue=_num(income, "total_revenue", "revenue"),
                net_income=_num(income, "n_income_attr_p"),
                net_income_dedt=_num(indicator, "profit_dedt"),
                basic_eps=_num(income, "basic_eps"),
                total_assets=_num(balance, "total_assets"),
                equity=_num(balance, "total_hldr_eqy_exc_min_int"),
                money_cap=_num(balance, "money_cap"),
                inventories=_num(balance, "inventories"),
                accounts_receiv=_num(balance, "accounts_receiv"),
                st_borr=_num(balance, "st_borr"),
                debt=total_debt(balance, "A股") if balance else None,
                cfo=_num(cashflow, "n_cashflow_act"),
                capex=_num(cashflow, "c_pay_acq_const_fiolta"),
                capex_label="购建固定资产、无形资产和其他长期资产支付的现金",
                fcf_reported=_num(cashflow, "free_cashflow"),
                fcf_reported_label="Tushare free_cashflow",
                roe={
                    key: indicator[key]
                    for key in ("roe", "roe_waa", "roe_dt")
                    if indicator and isinstance(indicator.get(key), (int, float))
                },
                cumulative=True,
            )
        )
    return sorted(rows, key=lambda row: row["end_date"], reverse=True)


def _normalize_statement_rows(
    rows: Iterable[Dict[str, Any]], market: str, *, capex_label: Optional[str]
) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        end = str(row.get("end_date") or "")
        fp = str(row.get("fp") or "")
        if len(end) != 8 or not fp:
            continue
        out.append(
            _period_row(
                end,
                fp,
                row.get("currency"),
                revenue=_num(row, "total_revenue"),
                net_income=_num(row, "n_income_attr_p"),
                net_income_dedt=None,
                basic_eps=_num(row, "basic_eps"),
                total_assets=_num(row, "total_assets"),
                equity=_num(row, "total_hldr_eqy_exc_min_int"),
                money_cap=_num(row, "money_cap"),
                inventories=_num(row, "inventories"),
                accounts_receiv=_num(row, "accounts_receiv"),
                st_borr=_num(row, "st_borr"),
                debt=total_debt(row, market),
                cfo=_num(row, "n_cashflow_act"),
                capex=_num(row, "capex"),
                capex_label=capex_label,
                fcf_reported=_num(row, "free_cashflow"),
                fcf_reported_label="报表/雅虎给出的自由现金流",
                div_paid=_num(row, "div_paid_owners"),
                roe={},
                cumulative=False,
            )
        )
    return sorted(out, key=lambda r: (r["end_date"], r["fp"] == "FY"), reverse=True)


def normalize_hk(merged_rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """港股：merge_hk_statement_rows 的结果（PDF 行优先、雅虎补缺，FY 与 H1）。"""
    return _normalize_statement_rows(merged_rows, "港股", capex_label="资本开支")


def normalize_us(edgar_rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """美股：EDGAR 透视行（FY 与单季）。EDGAR 概念链没有资本开支 → 不给自由现金流。"""
    return _normalize_statement_rows(edgar_rows, "美股", capex_label=None)


# ---------------------------------------------------------------------------
# 期间信号
# ---------------------------------------------------------------------------


def _year_ago(rows: Sequence[Dict[str, Any]], row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """同类期间里期末**恰好早一年**的那期（``periods.prior``：350–380 天，兼容 52/53 周与非日历
    财年）；没有就是 None——绝不拿列表里的下一行顶替：抽取缺年、存疑年度被过滤时，下一行可能是
    两年前（PR #333 评审 P2：2025 对 2023 被标成「同比 100%」）。"""
    return prior(rows, row)


def _compare(current: Dict[str, Any], prior: Optional[Dict[str, Any]], fields: Sequence[str]):
    block: Dict[str, Any] = {"period": current["end_date"], "fp": current["fp"]}
    block["currency"] = current.get("currency")
    if prior is None:
        block["comparison"] = None
        block["note"] = "缺恰好上一年的同期数据（不跨年比较）"
    elif not _same_currency(current, prior):
        block["comparison"] = None
        block["note"] = "两期报告币种不同或未知，不作比较"
    else:
        block["prior_period"] = prior["end_date"]
    for field in fields:
        value = current.get(field)
        entry: Dict[str, Any] = {"value_yi": _yi(value)}
        if prior is not None and _same_currency(current, prior):
            prior_value = prior.get(field)
            entry["prior_value_yi"] = _yi(prior_value)
            entry["yoy_pct"] = _growth(value, prior_value)
            # 方向按原值判断：盈亏反转时百分比没有意义（为 None），但方向是确定的
            entry["direction"] = _direction(value, prior_value)
        block[field] = entry
    fcf = _fcf(current)
    block["free_cashflow"] = {"value_yi": _yi(fcf["value"]), "basis": fcf["basis"]} if fcf else None
    return block


def _first_half_of(rows: Sequence[Dict[str, Any]], fy_row: Dict[str, Any]):
    """该财年的上半年行：H1 期末在 FY 期末前约 6 个月（兼容非日历财年）。"""
    fy_end = _parse_date(fy_row["end_date"])
    for row in rows:
        end = _parse_date(row["end_date"])
        if row["fp"] == "H1" and fy_end and end and 150 <= (fy_end - end).days <= 215:
            return row
    return None


def period_signals(rows: Sequence[Dict[str, Any]], market: str) -> Dict[str, Any]:
    fields = ("revenue", "net_income", "net_income_dedt")
    annual = [row for row in rows if row["fp"] == "FY"]
    interim = [row for row in rows if row["fp"] != "FY"]
    out: Dict[str, Any] = {}
    if annual:
        out["latest_fy"] = _compare(annual[0], _year_ago(annual, annual[0]), fields)
    if interim:
        latest = interim[0]
        out["latest_interim"] = {
            **_compare(latest, _year_ago(interim, latest), fields),
            "basis": "年初至今累计" if latest.get("cumulative") else "单期",
        }
    halves = []
    for fy_row in annual[:3]:
        first = _first_half_of(interim, fy_row)
        if first is None or not _same_currency(first, fy_row):
            continue
        halves.append(
            {
                "fiscal_year_end": fy_row["end_date"],
                "revenue_h2_yi": _yi(_sub(fy_row.get("revenue"), first.get("revenue"))),
                "net_income_h2_yi": _yi(_sub(fy_row.get("net_income"), first.get("net_income"))),
                "_revenue_h2": _sub(fy_row.get("revenue"), first.get("revenue")),
                "_net_income_h2": _sub(fy_row.get("net_income"), first.get("net_income")),
                "currency": fy_row.get("currency"),
            }
        )
    for newer, older in zip(halves, halves[1:]):
        newer_end, older_end = (
            _parse_date(newer["fiscal_year_end"]),
            _parse_date(older["fiscal_year_end"]),
        )
        adjacent = newer_end and older_end and 350 <= (newer_end - older_end).days <= 380
        if adjacent and newer["currency"] == older["currency"]:
            newer["revenue_h2_yoy_pct"] = _growth(newer["_revenue_h2"], older["_revenue_h2"])
            newer["net_income_h2_yoy_pct"] = _growth(
                newer["_net_income_h2"], older["_net_income_h2"]
            )
    if halves:
        out["second_half"] = [
            {k: v for k, v in item.items() if not k.startswith("_")} for item in halves[:2]
        ]
    if market == "A股" and interim:
        quarter = _latest_single_quarter(rows)
        if quarter:
            out["latest_single_quarter"] = quarter
    return out


def _sub(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or b is None else a - b


_PREVIOUS_CUMULATIVE = {"H1": "0331", "Q3": "0630", "FY": "0930"}


def _single_quarter(rows_by_end: Dict[str, Dict[str, Any]], row: Dict[str, Any]):
    """A股 累计值 → 单季：Q1 即累计；其余 = 本期累计 − 同年上一季累计。"""
    if row["fp"] == "Q1":
        return row.get("revenue"), row.get("net_income")
    previous = rows_by_end.get(row["end_date"][:4] + _PREVIOUS_CUMULATIVE.get(row["fp"], ""))
    if previous is None:
        return None, None
    return (
        _sub(row.get("revenue"), previous.get("revenue")),
        _sub(row.get("net_income"), previous.get("net_income")),
    )


def _latest_single_quarter(rows: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    by_end = {row["end_date"]: row for row in rows}
    latest = rows[0]
    revenue, net_income = _single_quarter(by_end, latest)
    if revenue is None and net_income is None:
        return None
    prior = by_end.get(str(int(latest["end_date"][:4]) - 1) + latest["end_date"][4:])
    prior_revenue, prior_net = _single_quarter(by_end, prior) if prior else (None, None)
    return {
        "quarter_end": latest["end_date"],
        "revenue_yi": _yi(revenue),
        "net_income_yi": _yi(net_income),
        "revenue_yoy_pct": _growth(revenue, prior_revenue),
        "net_income_yoy_pct": _growth(net_income, prior_net),
        "basis": "累计值相减推算的单季",
    }


# ---------------------------------------------------------------------------
# 股东回报
# ---------------------------------------------------------------------------


def _revision_key(row: Dict[str, Any]) -> Tuple[str, str]:
    """同一次分配多条「实施」记录的修订先后：实施公告日优先，其次公告日（晚者为准）。"""
    return (str(row.get("imp_ann_date") or ""), str(row.get("ann_date") or ""))


def implemented_dividends(dividend_rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A股 分红「实施」记录去重的**唯一定义**（signals 汇总与送模型的分红记录共用，PR #333 后评审：
    两处曾各用一套修订规则，同一次分配会选出两个金额）。

    Tushare 对同一次分配常有多条「实施」（不同公告日；2026-09 生产 25 只里 18 只存在）：按
    (end_date, ex_date) 归为一次分配，取 `_revision_key` 最晚的一条；null 与 0 视为同值，两条非空
    金额不同时 conflict=True。跨报告期有相同的明确除息日、实施公告日与分配金额时再合并，
    归属最新报告期并保留 source_end_dates。返回 [{end_date, ex_date, row, conflict}]。"""
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for row in dividend_rows:
        if row.get("div_proc") != "实施":
            continue
        key = (str(row.get("end_date") or ""), str(row.get("ex_date") or ""))
        groups.setdefault(key, []).append(row)
    out = []
    for (end_date, ex_date), items in groups.items():
        items.sort(key=_revision_key)
        amounts = {_num(r, "cash_div_tax") or 0.0 for r in items}
        out.append(
            {
                "end_date": end_date,
                "ex_date": ex_date,
                "row": items[-1],
                "conflict": len(amounts) > 1,
            }
        )
    # 同一次实施公告可能挂在两个报告期下（#385）。只在除息日、实施公告日和分配
    # 金额都明确且一致时跨期合并；缺信息或有修订冲突时保留，不猜是哪一次分配。
    distributions: Dict[tuple, Dict[str, Any]] = {}
    deduped = []
    for item in out:
        row = item["row"]
        ex_date = item["ex_date"]
        imp_ann_date = str(row.get("imp_ann_date") or "")
        amount = _num(row, "cash_div_tax")
        if (
            item["conflict"]
            or len(ex_date) != 8
            or not ex_date.isdigit()
            or len(imp_ann_date) != 8
            or not imp_ann_date.isdigit()
            or amount is None
            or amount <= 0
        ):
            deduped.append(item)
            continue
        key = (ex_date, imp_ann_date, amount, _num(row, "stk_div") or 0.0)
        previous = distributions.get(key)
        if previous is None:
            distributions[key] = item
            deduped.append(item)
            continue
        periods = sorted(
            set(previous.get("source_end_dates") or [previous["end_date"]]) | {item["end_date"]}
        )
        if item["end_date"] > previous["end_date"]:
            previous.update(item)
        previous["source_end_dates"] = periods
    return deduped


def a_share_dividends_per_share(dividend_rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """A股 分红「实施」记录 → 按财年汇总的每股税前现金分红（含中期），去重见 implemented_dividends。"""
    rows = list(dividend_rows)
    by_year: Dict[str, float] = {}
    conflicts = []
    for item in implemented_dividends(rows):
        if item["conflict"]:
            conflicts.append({"end_date": item["end_date"], "ex_date": item["ex_date"]})
        amount = _num(item["row"], "cash_div_tax") or 0.0
        if amount > 0 and len(item["end_date"]) >= 4:
            year = item["end_date"][:4]
            by_year[year] = by_year.get(year, 0.0) + amount
    return {"by_year": by_year, "conflicts": conflicts, "has_records": bool(rows)}


def _fcf(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    cfo, capex = row.get("cfo"), row.get("capex")
    if cfo is not None and capex is not None and row.get("capex_label"):
        value = cfo - abs(capex)
        result = {"value": value, "basis": f"经营现金流 − |{row['capex_label']}|"}
        reported = row.get("fcf_reported")
        if reported is not None and value and abs(reported - value) > abs(value) * 0.01:
            result["alternative"] = {"value_yi": _yi(reported), "basis": row["fcf_reported_label"]}
        return result
    if row.get("fcf_reported") is not None:
        return {"value": row["fcf_reported"], "basis": row["fcf_reported_label"]}
    return None


def shareholder_returns(
    rows: Sequence[Dict[str, Any]],
    market: str,
    *,
    dividend_rows: Sequence[Dict[str, Any]] = (),
    daily_basic: Optional[Dict[str, Any]] = None,
    graham: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    all_annual = [row for row in rows if row["fp"] == "FY"]
    annual = all_annual[:MAX_YEARS]
    a_share = a_share_dividends_per_share(dividend_rows) if market == "A股" else None
    years = []
    for row in annual:
        year = row["end_date"][:4]
        entry: Dict[str, Any] = {"fiscal_year_end": row["end_date"], "currency": row["currency"]}
        net_income, eps = row.get("net_income"), row.get("basic_eps")
        total: Optional[float] = None
        if a_share is not None:
            dps = a_share["by_year"].get(year)
            entry["dps"] = round(dps, 4) if dps is not None else None
            if dps is not None and eps and net_income:
                total = dps * net_income / eps  # 隐含股数 = 归母净利 ÷ 基本 EPS
                entry["dividends_total_basis"] = "每股分红 × 隐含股数（归母净利 ÷ 基本每股收益）"
            if dps is not None and eps and eps > 0:
                entry["payout_ratio_pct"] = _pct(dps, eps)
        else:
            total = row.get("div_paid")
            if total is not None:
                entry["dividends_total_basis"] = "现金流量表已付本公司股东股息（当年实际支付）"
                if net_income and net_income > 0:
                    entry["payout_ratio_pct"] = _pct(total, net_income)
        entry["dividends_total_yi"] = _yi(total)
        entry["paid_status"] = _paid_status(total, a_share, year)
        fcf = _fcf(row)
        if fcf is not None:
            entry["fcf_yi"] = _yi(fcf["value"])
            entry["fcf_basis"] = fcf["basis"]
            if "alternative" in fcf:
                entry["fcf_alternative"] = fcf["alternative"]
            if total is not None:
                if fcf["value"] > 0:
                    entry["dividends_to_fcf_pct"] = _pct(total, fcf["value"])
                elif total > 0:
                    entry["dividends_to_fcf_note"] = "自由现金流非正，股息没有自由现金流覆盖"
        elif market == "美股":
            entry["fcf_note"] = "EDGAR 概念链无资本开支，未计算自由现金流"
        flags = []
        if (entry.get("dividends_to_fcf_pct") or 0) > 100:
            flags.append("股息超过自由现金流")
        if (entry.get("payout_ratio_pct") or 0) > 100:
            flags.append("股息支付率超过 100%")
        if flags:
            entry["flags"] = flags
        years.append(entry)
    out: Dict[str, Any] = {
        "by_year": years,
        "dividend_yield": _dividend_yield(market, rows, daily_basic, graham),
    }
    out["ever_paid"] = _ever_paid(all_annual, a_share)
    if a_share is None:
        unpaid = _recent_unpaid_years(all_annual)
        if unpaid:
            out["recent_unpaid_years"] = unpaid
    if a_share and a_share["conflicts"]:
        out["dividend_record_conflicts"] = a_share["conflicts"]
    return out


def _paid_status(total: Optional[float], a_share: Optional[Dict[str, Any]], year: str) -> str:
    """该财年是否派息，按**原始金额**判定（展示值已四舍五入到 0.01 亿，40 万元会显示成 0.00，
    不能据此判未派息——PR #333 后评审）：paid / none / unknown。A股 有分红记录而该年无现金分派
    = none（Tushare 分红表覆盖上市以来）；港股/美股按已付股息原值。"""
    if a_share is not None:
        if a_share["by_year"].get(year):
            return "paid"
        return "none" if a_share["has_records"] else "unknown"
    if total is None:
        return "unknown"
    return "paid" if total > 0 else "none"


def _ever_paid(all_annual, a_share) -> Optional[bool]:
    """是否曾经派息。True 要有正向支付证据（看**全部**已知年度，不截断）；False 只在有完整历史
    时才下：A股 的 Tushare 分红表覆盖上市以来全部记录，有记录而无一次现金分派 = 从未派息。
    港股/美股的报表只覆盖近若干年，窗口内没付不能证明历史上从未付过（PR #333 评审 P2）——
    返回 None，窗口内的情况另见 recent_unpaid_years。"""
    if a_share is not None:
        if a_share["by_year"]:
            return True
        return False if a_share["has_records"] else None
    if any((row.get("div_paid") or 0) > 0 for row in all_annual):
        return True
    return None


def _recent_unpaid_years(all_annual) -> int:
    """港股/美股：从最新财年起**连续相邻**的财年里已知未派息（已付股息为 0）的年数。遇到未知、
    正数或**缺年**即停——缺年的派息情况未知，不能算进「连续」（PR #333 复审：2025/2023/2021
    三份零分红年报曾被数成连续 3 年）。"""

    def unpaid(row: Dict[str, Any]) -> Optional[bool]:
        paid = row.get("div_paid")
        return None if paid is None else paid <= 0

    return consecutive_run(all_annual, unpaid).count


def _pb_basis(graham: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    for item in (graham or {}).get("criteria") or []:
        if item.get("criterion") == "pb_or_product":
            return item.get("basis") or {}
    return {}


def _dividend_yield(market, rows, daily_basic, graham) -> Dict[str, Any]:
    if market == "A股":
        value = _num(daily_basic, "dv_ttm")
        if value is None:
            return {"value_pct": None, "note": "估值快照无股息率"}
        return {
            "value_pct": round(value, 2),
            "basis": f"Tushare dv_ttm（近 12 个月股息率，{(daily_basic or {}).get('trade_date')}）",
        }
    latest_fy = next((row for row in rows if row["fp"] == "FY"), None)
    basis = _pb_basis(graham)
    paid, shares, price = (
        (latest_fy or {}).get("div_paid"),
        basis.get("implied_shares"),
        basis.get("price"),
    )
    if not (paid and shares and price):
        return {"value_pct": None, "note": "缺已付股息、隐含股数或价格，未估算"}
    if basis.get("share_ratio") not in (None, 1, 1.0):
        return {"value_pct": None, "note": "价格按 ADS 计、股数按普通股计，未估算"}
    fx, fx_note = 1.0, ""
    if latest_fy.get("currency") != basis.get("price_currency"):
        # 与格雷厄姆估值同一汇率：每股净资产（价格币种）× 隐含股数 ÷ 归母权益（报表币种）
        equity, bvps = basis.get("equity"), basis.get("bvps")
        if not (equity and bvps and basis.get("equity_currency") == latest_fy.get("currency")):
            return {"value_pct": None, "note": "报表币种与价格币种不同且无可用汇率，未估算"}
        fx = bvps * shares / equity
        fx_note = f"，股息按估值同一汇率折为 {basis.get('price_currency')}"
    return {
        "value_pct": round(paid * fx / (shares * price) * 100, 2),
        "basis": (
            f"估算：{latest_fy['end_date'][:4]} 年已付股息 ÷（{basis.get('price_date')} 收盘价 × "
            f"隐含股数）{fx_note}，非官方股息率"
        ),
        "estimated": True,
    }


# ---------------------------------------------------------------------------
# 资产负债科目变动与 ROE
# ---------------------------------------------------------------------------


def _balance_value(row: Dict[str, Any], field: str) -> Optional[float]:
    if field == "interest_bearing_debt":
        return (row.get("debt") or {}).get("value")
    return row.get(field)


def balance_changes(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    with_balance = [row for row in rows if row.get("total_assets")]
    if not with_balance:
        return {}
    latest = with_balance[0]
    previous = with_balance[1] if len(with_balance) > 1 else None
    latest_end = _parse_date(latest["end_date"])
    if previous is not None:
        previous_end = _parse_date(previous["end_date"])
        # 「上期末」只认 13 个月内的期末；中间缺期时不拿更早的顶替
        if (
            not _same_currency(latest, previous)
            or latest_end is None
            or previous_end is None
            or (latest_end - previous_end).days > 400
        ):
            previous = None
    year_ago = next(
        (
            row
            for row in with_balance[1:]
            if row["end_date"][4:] == latest["end_date"][4:]
            and int(row["end_date"][:4]) == int(latest["end_date"][:4]) - 1
            and _same_currency(row, latest)
        ),
        None,
    )
    assets = latest["total_assets"]
    items = {}
    for field, label in _BALANCE_ITEMS:
        value = _balance_value(latest, field)
        if value is None:
            continue
        entry: Dict[str, Any] = {"label": label, "value_yi": _yi(value)}
        share = value / assets
        entry["share_of_assets_pct"] = round(share * 100, 2)
        if share < LOW_BASE_SHARE:
            entry["low_base"] = True
        for key, other in (("vs_previous_period", previous), ("vs_year_ago", year_ago)):
            other_value = _balance_value(other, field) if other else None
            if other_value is None:
                continue
            entry[key] = {
                "period": other["end_date"],
                "value_yi": _yi(other_value),
                "direction": _direction(value, other_value),
                "change_pct": _growth(value, other_value),
            }
        items[field] = entry
    out: Dict[str, Any] = {
        "period": latest["end_date"],
        "currency": latest.get("currency"),
        "total_assets_yi": _yi(assets),
        "items": items,
    }
    debt = latest.get("debt")
    if latest.get("money_cap") is not None and debt:
        net_cash = latest["money_cap"] - debt["value"]
        out["net_cash"] = {
            "value_yi": _yi(net_cash),
            "share_of_assets_pct": round(net_cash / assets * 100, 2),
            "debt_basis": debt["basis"],
        }
    return out


def roe_by_year(rows: Sequence[Dict[str, Any]], market: str) -> List[Dict[str, Any]]:
    annual = [row for row in rows if row["fp"] == "FY"]
    out = []
    for index, row in enumerate(annual[:MAX_YEARS]):
        entry: Dict[str, Any] = {"fiscal_year_end": row["end_date"]}
        if market == "A股" and row.get("roe"):
            labels = {
                "roe": "roe(Tushare 净资产收益率)",
                "roe_waa": "roe_waa(加权平均)",
                "roe_dt": "roe_dt(扣非)",
            }
            entry.update({labels[k]: round(float(v), 2) for k, v in row["roe"].items()})
            out.append(entry)
            continue
        net_income, equity = row.get("net_income"), row.get("equity")
        if net_income is None or not equity:
            continue
        prior = _year_ago(annual, row)  # 期初权益 = 恰好上一财年末，缺年不拿更早的顶替
        if prior is not None and prior.get("equity") and _same_currency(row, prior):
            average = (equity + prior["equity"]) / 2
            entry["roe_pct"] = _pct(net_income, average)
            entry["formula"] = "归母净利 ÷ 期初期末归母权益均值"
        else:
            entry["roe_pct"] = _pct(net_income, equity)
            entry["formula"] = "归母净利 ÷ 期末归母权益（缺上一财年末）"
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

SIGNALS_SEMANTICS = (
    "signals=服务端按固定口径预先算好的数（金额单位亿、报告币种见 currency；百分比已乘 100），"
    "正文涉及这些量时**直接引用、不得自行重算**；同比按 (本期−上期)÷|上期| 计，两期都亏损时"
    "正数表示亏损收窄，两期一正一负时不给百分比，但 direction(上升/下降/持平，按原值判断)照给；"
    "所有同比都是恰好上一年的同期，缺年时不比较：period_signals.latest_fy/latest_interim=最新年报/"
    "最新中报(季报)与上年同期(A股 利润为年初至今累计)、second_half=由全年减上半年推算的下半年及"
    "同比、latest_single_quarter=累计值相减推算的单季；shareholder_returns.by_year=逐年股息总额、"
    "每股分红(dps，含中期)、paid_status(paid/none/unknown，按原始金额判定是否派息)、股息支付率、自由现金流(fcf_basis 为口径；分析输入只给主口径)、"
    "股息占自由现金流比例，dividend_yield=股息率(estimated=true 为估算)，ever_paid=true 表示"
    "有派息记录、false 表示确认从未派息(仅 A股 有完整分红史时给出，不得称「分红中断」)、null 表示"
    "无法确认；recent_unpaid_years=港股/美股从最新财年起连续相邻、已知未派息的年数(缺年或未知即止，只说明报表窗口内)；balance_changes=最新一期资产负债科目较上期末/上年同期的"
    "方向与幅度、占总资产比例(low_base=true 表示占比不足 1%，其增速不作信号)，net_cash=货币资金"
    "减有息负债(只含货币资金，不含定期存款、交易性金融资产等现金类资产)；roe=逐年 ROE(口径见"
    "字段名或 formula)"
)


def compute_signals(
    market: str,
    rows: Sequence[Dict[str, Any]],
    *,
    dividend_rows: Sequence[Dict[str, Any]] = (),
    daily_basic: Optional[Dict[str, Any]] = None,
    graham: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """归一后的期间行 → signals 块；没有任何期间行时 {"status": "no_data"}。"""
    if not rows:
        return {"status": "no_data"}
    return {
        "status": "ok",
        "period_signals": period_signals(rows, market),
        "shareholder_returns": shareholder_returns(
            rows, market, dividend_rows=dividend_rows, daily_basic=daily_basic, graham=graham
        ),
        "balance_changes": balance_changes(rows),
        "roe": roe_by_year(rows, market),
    }


def signals_from_inputs(
    market: str, inputs: Optional[Dict[str, Any]], graham: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """load_signal_inputs 的结果 → signals（按市场归一后计算；测试用固件直接调用）。"""
    if not inputs:
        return {"status": "no_data"}
    annual, interim = inputs.get("annual") or {}, inputs.get("interim") or {}
    if market == "A股":
        kinds = ("income", "balancesheet", "cashflow", "fina_indicator")
        rows = normalize_a_share(
            {kind: (annual.get(kind) or []) + (interim.get(kind) or []) for kind in kinds}
        )
    elif market == "港股":
        from .earnings_quality import merge_hk_statement_rows

        rows = normalize_hk(
            merge_hk_statement_rows(
                {
                    "report_statements": (annual.get("report_statements") or [])
                    + (interim.get("report_statements") or []),
                    "yahoo_fundamentals": annual.get("yahoo_fundamentals") or [],
                }
            )
        )
    elif market == "美股":
        rows = normalize_us(
            (annual.get("edgar_companyfacts") or []) + (interim.get("edgar_companyfacts") or [])
        )
    else:
        return {"status": "no_data"}
    return compute_signals(
        market,
        rows,
        dividend_rows=inputs.get("dividend_rows") or [],
        daily_basic=(inputs.get("daily_basic") or [None])[0],
        graham=graham,
    )


# ---------------------------------------------------------------------------
# 标签兜底校验（只丢标签，不拒绝整份）
# ---------------------------------------------------------------------------

_MARGIN_CRITERIA = ("pe", "pb_or_product", "current_ratio", "lt_debt_vs_net_current_assets")


def _direction_of(signals: Dict[str, Any], block_name: str, field: str) -> Optional[str]:
    """按原值判断的同比方向（上升/下降/持平）——盈亏反转时百分比为 None，方向仍确定
    （PR #333 评审 P2：由盈转亏的中报曾因百分比为空被当成「没有证据」）。"""
    block = (signals.get("period_signals") or {}).get(block_name) or {}
    return (block.get(field) or {}).get("direction")


def _growth_supported(signals: Dict[str, Any], block_name: str) -> Optional[bool]:
    """该期是否支持「业绩增长」（营收与归母净利均同比增长）；数据不全返回 None。"""
    revenue = _direction_of(signals, block_name, "revenue")
    net = _direction_of(signals, block_name, "net_income")
    if revenue is None or net is None:
        return None
    return revenue == "上升" and net == "上升"


def _decline_supported(signals: Dict[str, Any], block_name: str) -> Optional[bool]:
    net = _direction_of(signals, block_name, "net_income")
    return None if net is None else net == "下降"


def _contradicted_in_both(judge, signals: Dict[str, Any]) -> bool:
    """最新财年与最新中报（季报）里**有数据的各期都不支持**才算矛盾，只要一期支持就保留——
    2026-09 全量回放里 5 个「业绩下滑」都是财年增长而最新中报下降，模型依据更新的中报是合理的。
    某期缺数据时由另一期决定；两期都缺则不判。"""
    known = [
        verdict
        for verdict in (judge(signals, name) for name in ("latest_fy", "latest_interim"))
        if verdict is not None
    ]
    return bool(known) and not any(known)


_PERIOD_LABELS = {"latest_fy": "最新财年", "latest_interim": "最新中报（季报）"}


def _field_evidence(block: Dict[str, Any], field: str, label: str) -> Optional[str]:
    entry = block.get(field) or {}
    if not entry.get("direction"):
        return None
    change = f"，同比 {entry['yoy_pct']}%" if entry.get("yoy_pct") is not None else ""
    return (
        f"{label}{entry['direction']}（{entry.get('prior_value_yi')} → "
        f"{entry.get('value_yi')} 亿{change}）"
    )


def _period_evidence(signals: Dict[str, Any], *, with_revenue: bool) -> str:
    """理由里写出各期两期原值与方向（盈亏反转时没有百分比，方向与原值照样可核对）。"""
    parts = []
    for name, label in _PERIOD_LABELS.items():
        block = (signals.get("period_signals") or {}).get(name) or {}
        items = [_field_evidence(block, "net_income", "归母净利")]
        if with_revenue:
            items.insert(0, _field_evidence(block, "revenue", "营收"))
        items = [item for item in items if item]
        if items:
            parts.append(f"{label}（{block.get('period', '')}）" + "、".join(items))
    return "；".join(parts)


def _tag_verdict(tag: str, signals: Dict[str, Any], graham: Dict[str, Any]) -> Optional[str]:
    """标签与数据**明确**矛盾时返回原因；数据缺失或口径覆盖不到（不知道）返回 None——不知道 ≠
    不成立。只收录有确定性依据的几条（线上 prompt 的标签定义 + 2026-09 全量回放校正）；
    「净现金充裕」不在此列：signals 的净现金只含货币资金，不含定期存款、交易性金融资产，
    口径比「现金类资产」窄（回放中美的因此被误判），留给模型据数判断。"""
    returns = signals.get("shareholder_returns") or {}
    if tag == "业绩增长" and _contradicted_in_both(_growth_supported, signals):
        return _period_evidence(signals, with_revenue=True) + "；没有一期是营收与归母净利双增长"
    if tag == "业绩下滑" and _contradicted_in_both(_decline_supported, signals):
        return _period_evidence(signals, with_revenue=False) + "；归母净利都未同比下降"
    if tag in ("分红中断", "分红连续") and returns.get("ever_paid") is False:
        return "从未派息"
    if tag == "高股息":
        if returns.get("ever_paid") is False:
            return "从未派息"
        latest = (returns.get("by_year") or [{}])[0]
        if latest.get("paid_status") == "none":  # 原始金额判定，不用展示值
            return f"{latest.get('fiscal_year_end', '')[:4]} 年未派息"
    if tag == "安全边际不足":
        verdicts = {
            item.get("criterion"): item.get("verdict")
            for item in (graham or {}).get("criteria") or []
        }
        if all(verdicts.get(name) == "pass" for name in _MARGIN_CRITERIA):
            return "格雷厄姆估值与财务强度四项全部达标"
    return None


def validate_tags(
    tags: Sequence[str], signals: Optional[Dict[str, Any]], graham: Optional[Dict[str, Any]]
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """按预计算信号去掉与数据矛盾的标签，返回 (标签, 调整记录)。signals 缺失原样返回；
    全部标签都会被丢掉时保留原标签并记 tag_signal_conflict，不让整份分析失败。"""
    if not signals or signals.get("status") != "ok":
        return list(tags), []
    kept, adjustments = [], []
    for tag in tags:
        reason = _tag_verdict(tag, signals, graham or {})
        if reason:
            adjustments.append({"type": "tag_dropped_by_signal", "tag": tag, "reason": reason})
        else:
            kept.append(tag)
    if not kept and tags:
        return list(tags), [
            {
                "type": "tag_signal_conflict",
                "tags": list(tags),
                "reasons": [a["reason"] for a in adjustments],
            }
        ]
    return kept, adjustments
