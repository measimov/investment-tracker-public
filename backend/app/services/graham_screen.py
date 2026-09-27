"""格雷厄姆防御型准则 × 塔勒布脆弱性信号（纯函数，无 DB/网络）。

LLM 自行心算估值/财务比率不可靠：与 earnings_quality 同模式，从已入库的
报表行/估值快照预计算，结构化结果连同语义词典喂给分析 prompt，模型只做
解读不做算术。

七项准则出自《聪明的投资者》防御型投资者标准，阈值集中在
GRAHAM_DEFENSIVE_THRESHOLDS（原著口径注释在旁，个别按市场现实调低）。
每项输出 verdict=pass/fail/indeterminate + reason：**数据不足绝不冒充判定**
——港股年度科目由披露易年报 PDF 抽取可达十年（雅虎只补近 3-5 年），覆盖不足十年的标的
"十年盈利稳定"这类准则只能给 indeterminate 并注明
覆盖年限；反过来有亏损年即可确定 fail，与覆盖年限无关。

塔勒布侧输出脆弱性信号（fragility）：杠杆、净债务、利息覆盖——识别
"下行无界"的结构（高杠杆 + 利息覆盖薄 = 对稳定环境的隐性依赖）；
净现金为负债的镜像，是下行保护的第一层。凸性/期权性属定性判断，
留给 LLM 从商业画像/财报摘要评估，不在本层伪造量化。

估值口径（跨市场）：**判定一律用 TTM**。A股 = Tushare daily_basic 的 pe_ttm/pb 快照（不变）；
港股/美股 = 行情库最新收盘价（`valuation.price`，陈价照算但在依据里标注）÷ 报表推算的 TTM 每股
盈利（港股：最新年报 + 更新的中报 − 上年同期中报；美股：最新年报 + 年报后的单季 − 上年同期单季，
20-F 发行人无季报 → 最新年报），报表币种按价格日汇率折成价格币种；PB 用最近一期资产负债表
（中报新于年报即用中报）的归母权益 ÷ 隐含股数（同一报告的归母净利 / 基本每股盈利，估算）。
年报静态 PE 与格雷厄姆原著「三年平均盈利」PE 只作 `supplement` 展示，**不参与判定**。
"""

from datetime import date
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .earnings_quality import EDGAR_MISSING_OTHER_CURRENCY, edgar_missing_other_currency

# 原著防御型标准；dividend_years_min 原著为 20 年不间断，A 股市场史与
# 注册制前的分红文化撑不起该口径，按"连续 5 年"起判、数值如实展示。
GRAHAM_DEFENSIVE_THRESHOLDS: Dict[str, float] = {
    "current_ratio_min": 2.0,
    "earnings_stability_years": 10,
    "dividend_years_min": 5,
    "earnings_growth_min_pct": 33.0,  # 原著：十年每股盈利累计增长至少 1/3
    "pe_max": 15.0,
    "pb_max": 1.5,
    "pe_pb_product_max": 22.5,
}

# 分红实施年度与最新报告年度的容许差：年报分红在次年实施
DIVIDEND_LAG_YEARS = 1

CRITERIA_SEMANTICS = {
    "current_ratio": "流动比率=流动资产/流动负债；防御型标准 ≥ 2",
    "lt_debt_vs_net_current_assets": "长期债务 ≤ 净流动资产（流动资产−流动负债）",
    "earnings_stability": "以最新报告年度为锚、逐年无缺口的连续十年归母净利润为正；"
    "任一年亏损即 fail，有缺年/覆盖不足为 indeterminate（缺数不冒充稳定）",
    "dividend_record": "锚定最新报告年度的连续现金分红年数（允许 1 年披露滞后；"
    "最近一次分红更早即视为中断 fail）。A股=分红实施记录；港股/美股=现金流量表"
    "「已付本公司股东股息」逐年大于 0（现金流量表在而未列该项按 0 计）",
    "earnings_growth": "锚定十年窗口 [最新报告年度-9, 最新报告年度] 首尾每股盈利"
    "（缺 EPS 端点用净利润）累计增幅 ≥ 1/3；任一端点缺失为 indeterminate",
    "pe": "市盈率 ≤ 15，按 TTM 判定（A股 daily_basic pe_ttm；港股/美股 = 最新收盘价 ÷ "
    "报表推算的 TTM 每股盈利，basis 注明构成、价格日期与汇率）；supplement 中的年报静态 PE "
    "与原著三年平均盈利 PE 仅供参考、不参与判定",
    "pb_or_product": "市净率 ≤ 1.5，或 PE×PB ≤ 22.5（放宽条款：盈利便宜可容忍 PB 略高）；"
    "港股/美股 PB = 收盘价 ÷ 最近一期（MRQ）每股归母净资产（隐含股数估算）",
}

# 准则与判定的中文名：页面准则卡（前端 views/security-detail/analysisGlossary.ts 同名常量）
# 与 AI 分析正文共用同一套叫法。分析输入把它们挂在每条准则上（name_zh / verdict_zh），
# prompt 要求正文只用中文名——否则模型会把 current_ratio、pass 这类字段名原样抄进正文。
GRAHAM_CRITERIA_NAMES_ZH: Dict[str, str] = {
    "current_ratio": "流动比率",
    "lt_debt_vs_net_current_assets": "长期债务",
    "earnings_stability": "盈利稳定性",
    "dividend_record": "分红记录",
    "earnings_growth": "盈利增长",
    "pe": "市盈率",
    "pb_or_product": "市净率",
}
GRAHAM_VERDICT_LABELS_ZH: Dict[str, str] = {
    "pass": "达标",
    "fail": "不达标",
    "indeterminate": "不可判定",
}

# 隐含股数 = 归母净利 / 基本每股盈利：EPS 只披露到 2 位小数，绝对值太小时股数误差可达两位数百分比
SMALL_EPS_WARN = 0.05
# 同一公司各期隐含股数的容许倍数（与 report_statement_service.EPS_SHARES_RATIO 同量级）：
# 一年内股本变动很少超过 2 倍，超出即视为该期 EPS 取错（附注号/仙未折元）
EPS_SHARES_TOLERANCE = 2.0
# 美股相邻单季期末的间隔（52/53 周财年：13 周 = 91 天，14 周 = 98 天；日历季 90–92 天）
QUARTER_GAP_DAYS = (84, 98)

FRAGILITY_SEMANTICS = {
    "debt_to_assets": "总负债/总资产；杠杆越高对融资环境的隐性依赖越强",
    "net_debt_to_assets": "(有息负债−货币资金)/总资产；负值=净现金，"
    "是下行保护的第一层（塔勒布：先保证不死）",
    "interest_coverage": "息税前利润/利息支出；薄覆盖=利率或盈利小幅波动即可致损的脆弱结构",
    "net_cash_to_market_cap": "净现金/总市值（仅 A股有市值快照）；比例高=市价接近清算保护",
}


def _num(row: Optional[Dict[str, Any]], *fields: str) -> Optional[float]:
    if not row:
        return None
    for field in fields:
        value = row.get(field)
        if isinstance(value, (int, float)) and value == value:
            return float(value)
    return None


def _year_of(row: Dict[str, Any]) -> Optional[str]:
    end_date = str(row.get("end_date") or "")
    if not end_date:
        return None
    if end_date.endswith("1231") or str(row.get("fp") or "") == "FY":
        return end_date[:4]
    return None


def _annual_by_year(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    by_year: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        year = _year_of(row)
        if year and year not in by_year:
            by_year[year] = row
    return by_year


def _criterion(
    key: str,
    verdict: str,
    reason: str,
    value: Optional[float] = None,
    **extra: Any,
) -> Dict[str, Any]:
    item = {
        "criterion": key,
        "value": round(value, 4) if isinstance(value, float) else value,
        "verdict": verdict,
        "reason": reason,
    }
    # basis（估值构成/价格日期/汇率）与 supplement（参考值，不参与判定）只在有内容时出现
    item.update({name: payload for name, payload in extra.items() if payload is not None})
    return item


# 美股 EDGAR 行的长期债务：本期未报任何长期债务概念、但往年报过（同一概念链）→ 视为 0
# 的前提是该行由当前概念链抓取（旧链抓的行缺新概念不代表公司没有债务）
EDGAR_ZERO_DEBT_MIN_VERSION = 2  # v3（按报告币种取数）同样满足


def _lt_debt(
    balance_row: Optional[Dict[str, Any]],
    market: str,
    balance_by_year: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """准则 2 的「长期债务」取值与口径；取不到返回 None。

    - 港股：PDF 映射的非流动借款 `lt_borr`（非流动借款 + 非流动债券/票据/可转债）优先；没有时
      退回含短债的 `total_debt`，由调用方在超出净流动资产时判 indeterminate（无法归因）；
    - 美股：EDGAR 长期债务概念链；本期缺失而往年有值、且该行由当前概念链抓取 → 0（已清偿或
      转入流动负债——拼多多 2024 年可转债全部列为一年内到期、2025 年起不再报告）；
    - A股：沿用有息负债合计口径（_total_debt）。
    """
    if balance_row is None:
        return None
    if market == "港股":
        lt_borr = _num(balance_row, "lt_borr")
        if lt_borr is not None:
            return {"value": lt_borr, "basis": "非流动借款", "long_term_only": True}
        return _total_debt(balance_row, market)
    if market == "美股":
        info = _total_debt(balance_row, market)
        if info is not None:
            return info
        if edgar_missing_other_currency(balance_row, "lt_debt"):
            return None  # 只以非报告币种披露：不可知，不能按「缺概念 → 0」
        version = int(balance_row.get("edgar_chain_version") or 0)
        reported_before = any(
            _num(row, "lt_debt") is not None for row in balance_by_year.values()
        )
        if version >= EDGAR_ZERO_DEBT_MIN_VERSION and reported_before:
            return {
                "value": 0.0,
                "basis": "EDGAR 本期未报长期债务概念（往年报过，视为已清偿/转入流动负债，按 0 计）",
                "long_term_only": True,
            }
        return None
    return _total_debt(balance_row, market)


def _total_debt(balance_row: Optional[Dict[str, Any]], market: str) -> Optional[Dict[str, Any]]:
    """有息负债合计与口径说明；无法可靠合计时返回 None（宁缺毋错）。

    - 港股：total_debt 合计（PDF 映射或 Yahoo）；没有合计时用 PDF 的流动+非流动借款；
    - 美股：EDGAR 概念链只有长期债务（lt_debt），短债概念在各公司间过于分裂，
      合计只含长期口径并注明（净现金因此偏乐观，方向性标注给 LLM）；
    - A股：短借+长借+应付债券+一年内到期非流动负债，可得项求和、缺项注明。
    """
    if balance_row is None:
        return None
    if market == "港股":
        value = _num(balance_row, "total_debt")
        if value is not None:
            return {"value": value, "basis": "total_debt(含短债)"}
        lt_borr, st_borr = _num(balance_row, "lt_borr"), _num(balance_row, "st_borr")
        if lt_borr is not None and st_borr is not None:
            return {"value": lt_borr + st_borr, "basis": "流动+非流动借款"}
        return None
    if market == "美股":
        value = _num(balance_row, "lt_debt")
        return {"value": value, "basis": "仅长期债务(EDGAR 短债概念不统一)"} if value is not None else None
    parts = {
        "st_borr": _num(balance_row, "st_borr"),
        "lt_borr": _num(balance_row, "lt_borr"),
        "bond_payable": _num(balance_row, "bond_payable"),
        "non_cur_liab_due_1y": _num(balance_row, "non_cur_liab_due_1y"),
    }
    present = {name: value for name, value in parts.items() if value is not None}
    if not present:
        return None
    missing = sorted(set(parts) - set(present))
    basis = "短借+长借+应付债券+一年内到期非流动负债"
    if missing:
        basis += f"（缺 {'/'.join(missing)}，按 0 计）"
    return {"value": sum(present.values()), "basis": basis}


def _latest_daily_basic(daily_basic_rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    dated = [row for row in daily_basic_rows if row.get("trade_date")]
    if not dated:
        return None
    return max(dated, key=lambda row: str(row["trade_date"]))


def _dividend_years(dividend_rows: List[Dict[str, Any]]) -> List[int]:
    """有实施现金分红的年度集合（A股 dividend 数据集口径）。"""
    years = set()
    for row in dividend_rows:
        if str(row.get("div_proc") or "") != "实施":
            continue
        cash_div = _num(row, "cash_div_tax", "cash_div")
        end_date = str(row.get("end_date") or "")
        if cash_div and cash_div > 0 and len(end_date) >= 4:
            years.add(int(end_date[:4]))
    return sorted(years, reverse=True)


# ---------------------------------------------------------------------------- 估值（港股/美股）


def _end(row: Optional[Dict[str, Any]]) -> Optional[date]:
    raw = str((row or {}).get("end_date") or "")
    if len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
    except ValueError:
        return None


def _one_year_before(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:  # 2 月 29 日
        return day.replace(year=day.year - 1, day=28)


def resolve_fx_rates(
    currencies: Iterable[Optional[str]], target: str, on_date: date, lookup: Any
) -> Dict[str, float]:
    """报表币种 → 价格币种的汇率（纯函数，`lookup` 为 portfolio.fx.ExchangeRateLookup 鸭子类型）。

    汇率表只存 X→CNY：直接/反向查不到时经 CNY 交叉（USD→HKD = USD→CNY × CNY→HKD）。
    查不到的币种不出现在结果里，由调用方判 indeterminate。"""
    rates: Dict[str, float] = {}
    for currency in {c for c in currencies if c}:
        if currency == target:
            rates[currency] = 1.0
            continue
        rate = lookup.get_rate_on_or_before(currency, target, on_date)
        if rate is None and "CNY" not in (currency, target):
            to_cny = lookup.get_rate_on_or_before(currency, "CNY", on_date)
            from_cny = lookup.get_rate_on_or_before("CNY", target, on_date)
            if to_cny is not None and from_cny is not None:
                rate = to_cny * from_cny
        if rate is not None:
            rates[currency] = float(rate)
    return rates


class _Valuation:
    """估值输入的只读视图：价格、汇率、每股口径换算（美股 ADS）。"""

    def __init__(self, market: str, valuation: Dict[str, Any]):
        self.market = market
        self.price = valuation.get("price") or None
        self.fx_rates: Dict[str, float] = valuation.get("fx_rates") or {}
        self.interim_rows: List[Dict[str, Any]] = list(valuation.get("interim_rows") or [])
        self.share_ratio = valuation.get("share_ratio")
        self.share_ratio_note = valuation.get("share_ratio_note")
        self.share_ratio_source = valuation.get("share_ratio_source")
        self.share_ratio_missing = bool(valuation.get("share_ratio_missing"))
        self.annual_form = valuation.get("annual_form")

    @property
    def currency(self) -> str:
        return str((self.price or {}).get("currency") or "")

    @property
    def ratio(self) -> float:
        return float(self.share_ratio or 1.0)

    def convert(self, value: float, currency: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
        if not currency:
            return None, "报表币种未知"
        if currency == self.currency:
            return value, None
        rate = self.fx_rates.get(currency)
        if rate is None:
            return None, f"缺 {currency}→{self.currency} 汇率"
        return value * rate, None

    def price_label(self) -> str:
        price = self.price or {}
        label = f"价格 {float(price['close']):g} {self.currency}（{price.get('date')}"
        if price.get("stale"):
            label += f"，已陈旧 {price.get('age_days')} 天"
        return label + "）"

    def price_basis(self) -> Dict[str, Any]:
        price = self.price or {}
        basis = {
            "valuation_method": "estimated",
            "price": float(price["close"]),
            "price_currency": self.currency,
            "price_date": price.get("date"),
            "price_stale": bool(price.get("stale")),
            "price_age_days": price.get("age_days"),
            "price_source": price.get("source"),
        }
        if self.share_ratio:
            basis["share_ratio"] = self.share_ratio
            basis["share_ratio_note"] = self.share_ratio_note
            if self.share_ratio_source:
                basis["share_ratio_source"] = self.share_ratio_source
        return basis


def _shares_mismatch(row: Dict[str, Any], reference_shares: Optional[float]) -> Optional[str]:
    """该行 EPS 与净利润是否自洽：隐含股数（净利/EPS）偏离参照（最新年报的隐含股数）2 倍以上
    即判异常——生产实测 00728 2025 中报 EPS 被映射成 2.0（实为 0.25），滚动后 TTM 成了负数；
    EPS 仍是「仙」未折元的行同样会被这里拦下。"""
    shares = _implied_shares(row)
    if shares is None or not reference_shares:
        return None
    ratio = shares / reference_shares
    if 1 / EPS_SHARES_TOLERANCE <= ratio <= EPS_SHARES_TOLERANCE:
        return None
    return f"每股盈利与净利润不自洽（隐含股数为年报的 {ratio:.2g} 倍）"


def _reference_shares(annual_income: List[Dict[str, Any]]) -> Optional[float]:
    """参照股数 = 最新一份可算年报的隐含股数（TTM 的锚本来就是它；经审计、口径最稳）。"""
    return next((s for s in (_implied_shares(row) for row in annual_income) if s), None)


def _eps_component(
    row: Dict[str, Any], sign: str, view: _Valuation,
    reference_shares: Optional[float] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    eps = _num(row, "basic_eps")
    if eps is None:
        return None, "缺基本每股盈利"
    mismatch = _shares_mismatch(row, reference_shares)
    if mismatch:
        return None, mismatch
    converted, error = view.convert(eps, row.get("currency"))
    if error:
        return None, error
    return {
        "period": f"{row.get('end_date')}|{row.get('fp') or 'FY'}",
        "sign": sign,
        "eps": eps,
        "currency": row.get("currency"),
        "eps_in_price_currency": round(converted, 6),
    }, None


def _find_prior(rows: List[Dict[str, Any]], current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """上年同期行：期末日在「本期末 − 1 年」±20 天内（52/53 周财年与月末差异）。"""
    target = _one_year_before(_end(current))
    candidates = [
        row for row in rows
        if _end(row) is not None and abs((_end(row) - target).days) <= 20
        and _num(row, "basic_eps") is not None
    ]
    return min(candidates, key=lambda row: abs((_end(row) - target).days)) if candidates else None


def _ttm_eps(
    market: str, annual_income: List[Dict[str, Any]], view: _Valuation
) -> Dict[str, Any]:
    """TTM 每股盈利（价格币种、报表每股口径，未乘 ADS 比例）。

    返回 {eps, method, label, components[, note]} 或 {error}。"""
    # 最新一份有利润表数据的年报：它缺 EPS 就判不可算，不拿更早年份顶替（那不是 TTM）
    fy = next(
        (row for row in annual_income
         if any(_num(row, f) is not None for f in ("basic_eps", "n_income_attr_p", "total_revenue"))),
        None,
    )
    if fy is None:
        return {"error": "缺年报利润表数据"}
    reference = _implied_shares(fy) or _reference_shares(annual_income)
    fy_component, error = _eps_component(fy, "+", view)
    if error:
        return {"error": f"{fy.get('end_date')} 年报{error}"}
    fy_end = _end(fy)
    fy_label = f"{fy.get('end_date')} 年报"
    kind = "中报" if market == "港股" else "季报"

    def annual_only(label: str, note: Optional[str] = None) -> Dict[str, Any]:
        result = {
            "eps": fy_component["eps_in_price_currency"], "method": "annual",
            "label": label, "components": [fy_component],
        }
        if note:
            result["note"] = note
        return result

    # 只看带利润表数据的期间行（EDGAR 季度键下还有只含时点科目的比较列行）；同一期末只留一行
    interim = [
        row for row in view.interim_rows
        if _end(row) is not None
        and any(_num(row, f) is not None for f in ("basic_eps", "n_income_attr_p"))
    ]
    by_end: Dict[date, Dict[str, Any]] = {}
    for row in interim:
        by_end.setdefault(_end(row), row)
    newer = [
        by_end[end] for end in sorted(by_end)
        if fy_end and fy_end < end <= _one_year_after(fy_end)
    ]
    if market == "港股":
        newer = newer[-1:]  # 只取最新一期中报（H1 为 6 个月累计）
        if newer and not 120 <= (_end(newer[0]) - fy_end).days <= 240:
            return annual_only(
                f"{fy_label}（无衔接中报）",
                f"最新中报 {newer[0].get('end_date')} 与最新年报 {fy.get('end_date')} 不衔接，未滚动",
            )
    if not newer:
        if market == "美股" and str(view.annual_form or "").startswith("20-F"):
            return annual_only(f"{fy_label}（20-F 发行人不披露季报）")
        return annual_only(f"{fy_label}（无更新{kind}）")
    if market == "美股":
        # 单季必须从年报期末起**逐季连续**：库里只存在的季度不代表中间没缺季（缺 Q1 只剩 Q2 时
        # 「年报 + Q2 − 上年 Q2」只覆盖 9 个月却当 TTM 用）。52/53 周财年的季度长 84–98 天
        previous = fy_end
        for row in newer:
            gap = (_end(row) - previous).days
            if not QUARTER_GAP_DAYS[0] <= gap <= QUARTER_GAP_DAYS[1]:
                return annual_only(
                    f"{fy_label}（年报后单季不连续，未滚动）",
                    f"{previous.strftime('%Y%m%d')} 至 {row.get('end_date')} 间隔 {gap} 天，"
                    "中间缺单季，未滚动",
                )
            previous = _end(row)
        if len(newer) > 3:
            return annual_only(
                f"{fy_label}（年报后单季超过 3 个，未滚动）",
                "年报后已有 4 个单季，应有更新的年报未入库，未滚动",
            )
    components = [fy_component]
    total = fy_component["eps_in_price_currency"]
    for row in newer:
        prior = _find_prior(interim, row)
        if prior is None:
            return annual_only(
                f"{fy_label}（缺上年同期{kind}，未滚动）",
                f"{row.get('end_date')} {kind}缺上年同期每股盈利",
            )
        for item, sign in ((row, "+"), (prior, "-")):
            component, error = _eps_component(item, sign, view, reference)
            if error:
                return annual_only(
                    f"{fy_label}（{kind}数据不可用，未滚动）",
                    f"{item.get('end_date')} {kind}{error}",
                )
            components.append(component)
            total += component["eps_in_price_currency"] * (1 if sign == "+" else -1)
    if market == "港股":
        label = f"TTM = {fy_label} + {newer[0].get('end_date')} 中报 − 上年同期中报"
    else:
        label = f"TTM = {fy_label} + 年报后 {len(newer)} 个单季 − 上年同期单季"
    return {"eps": total, "method": "ttm", "label": label, "components": components}


def _one_year_after(day: date) -> date:
    try:
        return day.replace(year=day.year + 1)
    except ValueError:
        return day.replace(year=day.year + 1, day=28)


def _implied_shares(row: Optional[Dict[str, Any]]) -> Optional[float]:
    """隐含股数 = 归母净利 / 基本每股盈利（同一报告同一期；估算）。"""
    profit, eps = _num(row, "n_income_attr_p"), _num(row, "basic_eps")
    if profit is None or not eps or not profit:
        return None
    return abs(profit / eps)


def _mrq_bvps(
    annual_income: List[Dict[str, Any]],
    annual_balance: List[Dict[str, Any]],
    view: _Valuation,
) -> Dict[str, Any]:
    """最近一期（MRQ）每股归母净资产（价格币种、按价格口径乘 ADS 比例）；{error} 表示不可算。"""
    income_by_end = {row.get("end_date"): row for row in annual_income}
    candidates: List[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]] = [
        (row, income_by_end.get(row.get("end_date"))) for row in annual_balance
    ] + [(row, row) for row in view.interim_rows]
    candidates = [
        (balance, income) for balance, income in candidates
        if _end(balance) is not None and _num(balance, "total_hldr_eqy_exc_min_int") is not None
    ]
    if not candidates:
        return {"error": "缺归母权益科目"}
    balance, income = max(candidates, key=lambda pair: _end(pair[0]))
    equity = _num(balance, "total_hldr_eqy_exc_min_int")
    period = f"{balance.get('end_date')}|{balance.get('fp') or 'FY'}"
    notes: List[str] = []
    reference = _reference_shares(annual_income)
    shares, shares_from, shares_row = _implied_shares(income), period, income
    mismatch = _shares_mismatch(income, reference) if income is not None else None
    if shares is None or mismatch:
        if mismatch:
            notes.append(f"{period} {mismatch}，改用最新年报的隐含股数")
        shares_row = next((row for row in annual_income if _implied_shares(row)), None)
        shares = reference
        shares_from = f"{shares_row.get('end_date')}|FY" if shares_row else None
    if shares is None:
        return {"error": f"{period} 无法由归母净利/每股盈利推算股数"}
    converted, error = view.convert(equity, balance.get("currency"))
    if error:
        return {"error": f"{period} 资产负债表{error}"}
    bvps = converted / shares * view.ratio
    if abs(_num(shares_row, "basic_eps") or 0) < SMALL_EPS_WARN:
        notes.append("每股盈利数值小，隐含股数误差较大")
    return {
        "bvps": bvps,
        "period": period,
        "equity": equity,
        "equity_currency": balance.get("currency"),
        "implied_shares": round(shares),
        "shares_from": shares_from,
        "notes": notes,
    }


def _eps_supplement(
    annual_income: List[Dict[str, Any]],
    price: float,
    convert: Callable[[float, Optional[str]], Tuple[Optional[float], Optional[str]]],
    ratio: float = 1.0,
) -> Optional[Dict[str, Any]]:
    """年报静态 PE 与格雷厄姆原著「近三年平均每股盈利」PE（参考值，不参与判定）。"""
    series: List[Tuple[int, float]] = []
    reference = _reference_shares(annual_income)
    for row in annual_income:
        year = _year_of(row)
        eps = _num(row, "basic_eps")
        if year is None or eps is None or _shares_mismatch(row, reference):
            continue
        converted, error = convert(eps, row.get("currency"))
        if error or converted is None:
            continue
        series.append((int(year), converted * ratio))
    if not series:
        return None
    series.sort(reverse=True)
    latest_year, latest_eps = series[0]
    supplement: Dict[str, Any] = {
        "static_pe": round(price / latest_eps, 2) if latest_eps > 0 else None,
        "static_basis": f"FY{latest_year} 每股盈利 {latest_eps:.4g}"
        + ("（亏损，PE 无意义）" if latest_eps <= 0 else ""),
        "graham_avg3_pe": None,
        "basis": "年报口径，仅供参考，不参与判定",
    }
    by_year = dict(series)
    window = [latest_year - offset for offset in range(3)]
    if all(year in by_year for year in window):
        average = sum(by_year[year] for year in window) / 3
        supplement["avg3_basis"] = f"FY{window[-1]}–FY{latest_year} 平均每股盈利 {average:.4g}"
        if average > 0:
            supplement["graham_avg3_pe"] = round(price / average, 2)
        else:
            supplement["avg3_basis"] += "（为负，PE 无意义）"
    else:
        supplement["avg3_basis"] = "近三个财年每股盈利不全"
    return supplement


def _daily_basic_valuation(
    market: str, basic: Optional[Dict[str, Any]], annual_income: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """A股：Tushare daily_basic 快照的 pe_ttm/pb 判定（文案与判定口径保持原样），
    补充年报静态 PE 与原著三年平均 PE（快照收盘价 ÷ 年报 EPS，人民币同币种）。"""
    thresholds = GRAHAM_DEFENSIVE_THRESHOLDS
    criteria: List[Dict[str, Any]] = []
    pe = _num(basic, "pe_ttm", "pe")
    pb = _num(basic, "pb")
    trade_date = str((basic or {}).get("trade_date") or "")
    close = _num(basic, "close")
    supplement = (
        _eps_supplement(annual_income, close, lambda value, _currency: (value, None))
        if close else None
    )
    basis = (
        {"valuation_method": "snapshot", "label": "Tushare daily_basic 快照（pe_ttm / pb）",
         "price_date": trade_date,
         "price": close, "price_currency": "CNY"}
        if basic else None
    )
    if pe is not None:
        verdict = "pass" if 0 < pe <= thresholds["pe_max"] else "fail"
        reason = f"PE {pe:.2f}（阈值 ≤ {thresholds['pe_max']:.0f}，快照 {trade_date}）"
        if pe <= 0:
            verdict, reason = "fail", f"PE 为负（亏损），快照 {trade_date}"
        criteria.append(_criterion(
            "pe", verdict, reason, pe, basis=basis, supplement=supplement,
        ))
    else:
        criteria.append(_criterion(
            "pe", "indeterminate",
            "无估值快照" + ("" if market == "A股" else "（本市场无估值数据源）"),
        ))
    if pb is not None:
        if pb <= 0:
            # 负 PB = 净资产为负，不是"价格低于账面价值"的安全边际（评审 P1；
            # 与上面负 PE 判 fail 同口径）
            criteria.append(_criterion(
                "pb_or_product", "fail",
                f"PB {pb:.2f} 为负/零（净资产非正），不构成账面价值安全边际，快照 {trade_date}",
                pb, basis=basis,
            ))
        elif pb <= thresholds["pb_max"]:
            criteria.append(_criterion(
                "pb_or_product", "pass",
                f"PB {pb:.2f} ≤ {thresholds['pb_max']}（快照 {trade_date}）", pb, basis=basis,
            ))
        elif pe is not None and pe > 0 and pe * pb <= thresholds["pe_pb_product_max"]:
            criteria.append(_criterion(
                "pb_or_product", "pass",
                f"PB {pb:.2f} 超限但 PE×PB {pe * pb:.1f} ≤ "
                f"{thresholds['pe_pb_product_max']}（放宽条款）", pb, basis=basis,
            ))
        else:
            criteria.append(_criterion(
                "pb_or_product", "fail",
                f"PB {pb:.2f} > {thresholds['pb_max']}"
                + (f" 且 PE×PB {pe * pb:.1f} 超限" if pe is not None and pe > 0 else ""),
                pb, basis=basis,
            ))
    else:
        criteria.append(_criterion(
            "pb_or_product", "indeterminate",
            "无估值快照" + ("" if market == "A股" else "（本市场无估值数据源）"),
        ))
    return criteria


def _statement_valuation(
    market: str,
    annual_income: List[Dict[str, Any]],
    annual_balance: List[Dict[str, Any]],
    valuation: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """港股/美股的 pe 与 pb_or_product 两项准则（TTM 判定 + 年报口径补充）。"""
    thresholds = GRAHAM_DEFENSIVE_THRESHOLDS
    view = _Valuation(market, valuation or {})
    if not view.price or view.price.get("close") in (None, 0):
        reason = "无行情价格（行情库无该标的收盘价）"
        return (
            _criterion("pe", "indeterminate", reason),
            _criterion("pb_or_product", "indeterminate", reason),
        )
    if view.share_ratio_missing:
        reason = (
            "20-F 发行人以 ADS 交易，价格与每股盈利口径不一致：20-F 封面未解析出 ADS 换算比，"
            "可在特例规则中手动填写"
        )
        return (
            _criterion("pe", "indeterminate", reason),
            _criterion("pb_or_product", "indeterminate", reason),
        )
    close = float(view.price["close"])
    price_label = view.price_label()
    base_basis = view.price_basis()
    supplement = _eps_supplement(annual_income, close, view.convert, view.ratio)

    ttm = _ttm_eps(market, annual_income, view)
    pe: Optional[float] = None
    if "error" in ttm:
        pe_item = _criterion(
            "pe", "indeterminate", f"无法计算 TTM 每股盈利：{ttm['error']}",
            basis=base_basis, supplement=supplement,
        )
    else:
        eps = ttm["eps"] * view.ratio
        basis = {
            **base_basis, "eps_ttm": round(eps, 6), "method": ttm["method"],
            "label": ttm["label"], "components": ttm["components"],
        }
        if ttm.get("note"):
            basis["note"] = ttm["note"]
        detail = f"每股盈利 {eps:.4g} {view.currency}，{ttm['label']}"
        if eps <= 0:
            pe_item = _criterion(
                "pe", "fail", f"TTM 每股盈利为负/零（亏损）；{detail}；{price_label}",
                close / eps if eps else None, basis=basis, supplement=supplement,
            )
        else:
            pe = close / eps
            verdict = "pass" if pe <= thresholds["pe_max"] else "fail"
            pe_item = _criterion(
                "pe", verdict,
                f"PE(TTM) {pe:.2f}（阈值 ≤ {thresholds['pe_max']:.0f}；{detail}；{price_label}）",
                pe, basis=basis, supplement=supplement,
            )

    book = _mrq_bvps(annual_income, annual_balance, view)
    if "error" in book:
        return pe_item, _criterion(
            "pb_or_product", "indeterminate", f"无法估算每股净资产：{book['error']}",
            basis=base_basis,
        )
    pb = close / book["bvps"] if book["bvps"] else None
    pb_basis = {
        **base_basis, "bvps": round(book["bvps"], 6), "bvps_period": book["period"],
        "equity": book["equity"], "equity_currency": book["equity_currency"],
        "implied_shares": book["implied_shares"], "shares_from": book["shares_from"],
        "label": f"MRQ {book['period']} 归母权益 ÷ 隐含股数（估算）",
    }
    if book["notes"]:
        pb_basis["note"] = "；".join(book["notes"])
    book_label = f"每股净资产 {book['bvps']:.4g} {view.currency}（{book['period']}，隐含股数估算）"
    if pb is None or pb <= 0:
        pb_item = _criterion(
            "pb_or_product", "fail",
            f"PB 为负/零（净资产非正），不构成账面价值安全边际；{book_label}；{price_label}",
            pb, basis=pb_basis,
        )
    elif pb <= thresholds["pb_max"]:
        pb_item = _criterion(
            "pb_or_product", "pass",
            f"PB {pb:.2f} ≤ {thresholds['pb_max']}（{book_label}；{price_label}）",
            pb, basis=pb_basis,
        )
    elif pe is not None and pe * pb <= thresholds["pe_pb_product_max"]:
        pb_item = _criterion(
            "pb_or_product", "pass",
            f"PB {pb:.2f} 超限但 PE(TTM)×PB {pe * pb:.1f} ≤ "
            f"{thresholds['pe_pb_product_max']}（放宽条款；{book_label}）",
            pb, basis=pb_basis,
        )
    else:
        pb_item = _criterion(
            "pb_or_product", "fail",
            f"PB {pb:.2f} > {thresholds['pb_max']}"
            + (f" 且 PE(TTM)×PB {pe * pb:.1f} 超限" if pe is not None else "")
            + f"（{book_label}；{price_label}）",
            pb, basis=pb_basis,
        )
    return pe_item, pb_item


# ---------------------------------------------------------------------------- 分红（港股/美股）


def _statement_dividend(
    market: str,
    cashflow_by_year: Dict[str, Dict[str, Any]],
    anchor_year: int,
    earliest_year: int,
    history_confirmed: bool,
) -> Dict[str, Any]:
    """现金流量表「已付本公司股东股息」逐年 > 0 的连续年数（与 A股 同一锚定/滞后规则）。

    `div_paid_status`（透视层给出）：reported=有该科目；not_listed=现金流量表在而未列该项
    （按 0 计）；other_currency_only=EDGAR 该年已付股息只以非报告币种披露（不混币取数）→
    不可知；缺失=该年数据源不带这一科目（旧版本抽取行、雅虎未返回）→ 不可知。"""
    thresholds = GRAHAM_DEFENSIVE_THRESHOLDS
    minimum = int(thresholds["dividend_years_min"])
    known: Dict[int, float] = {}
    not_listed: List[int] = []
    other_currency: List[int] = []  # 未知年份里「只以非报告币种披露」的那些（说明原因用）
    for year, row in cashflow_by_year.items():
        status = row.get("div_paid_status")
        if status == "reported":
            known[int(year)] = abs(_num(row, "div_paid_owners") or 0.0)
        elif status == "not_listed":
            known[int(year)] = 0.0
            not_listed.append(int(year))
        elif status == EDGAR_MISSING_OTHER_CURRENCY:
            other_currency.append(int(year))

    def currency_note(years) -> str:
        hit = sorted(set(years) & set(other_currency))
        if not hit:
            return ""
        return f"（{'、'.join(str(y) for y in hit)} 年已付股息只以非报告币种披露，不混币取数）"

    if not known:
        if other_currency:
            pending = "（EDGAR 已付股息只以非报告币种披露，不混币取数）"
        elif market == "港股":
            pending = "（待报表重抽补齐「已付股东股息」科目）"
        else:
            pending = "（待重新同步 EDGAR 档案）"
        return _criterion(
            "dividend_record", "indeterminate", "无现金流量表已付股息数据" + pending,
        )
    listed_note = (
        f"；{'、'.join(str(y) for y in sorted(not_listed))} 年现金流量表未列已付股息，按 0 计"
        if not_listed else ""
    )
    paid = sorted((year for year, value in known.items() if value > 0), reverse=True)
    latest_paid = paid[0] if paid else None
    # 「已中断 / 从未支付」要有证据：锚定窗口 [anchor-滞后, anchor] 内每一年都必须**已知为 0**
    # （reported=0 或 not_listed）。已取得数据里最近的正值年份只说明那年付过，不说明之后没付
    # ——2022-2025 只有利润表没有现金流量表时，不能把 2021 判成最后一次分红（PR #232 评审）
    if latest_paid is None or latest_paid < anchor_year - DIVIDEND_LAG_YEARS:
        window = range(anchor_year - DIVIDEND_LAG_YEARS, anchor_year + 1)
        unknown = [year for year in window if year not in known]
        if unknown:
            last = f"已知最近一次支付股东股息为 {latest_paid} 年，" if latest_paid else ""
            return _criterion(
                "dividend_record", "indeterminate",
                f"{last}{'、'.join(str(y) for y in unknown)} 年无现金流量表已付股息数据"
                f"{currency_note(unknown)}，"
                f"无法判断分红是否持续（允许 {DIVIDEND_LAG_YEARS} 年披露滞后）",
            )
        if latest_paid is None:
            detail = (
                "现金流量表均未列已付股东股息（按 0 计）" if len(not_listed) == len(known)
                else f"现金流量表均无已付本公司股东股息{listed_note}"
            )
            return _criterion(
                "dividend_record", "fail", f"{min(known)}–{max(known)} 年{detail}", 0.0,
            )
        return _criterion(
            "dividend_record", "fail",
            f"最近一次支付股东股息为 {latest_paid} 年，距最新报告年度 {anchor_year} "
            f"已中断（允许 {DIVIDEND_LAG_YEARS} 年披露滞后）{listed_note}",
            0.0,
        )
    consecutive, year = 1, latest_paid
    while known.get(year - 1, 0.0) > 0:
        consecutive += 1
        year -= 1
    stop_year = year - 1
    tail = f"（现金流量表口径；阈值 ≥ {minimum} 年；原著为 20 年）"
    if consecutive >= minimum:
        return _criterion(
            "dividend_record", "pass",
            f"截至 {latest_paid} 年连续 {consecutive} 年支付股东股息{tail}",
            float(consecutive),
        )
    if stop_year not in known:
        if history_confirmed and stop_year < earliest_year:
            return _criterion(
                "dividend_record", "fail",
                f"截至 {latest_paid} 年连续 {consecutive} 年支付股东股息，披露历史始于 "
                f"{earliest_year} 年，不足 {minimum} 年{tail}",
                float(consecutive),
            )
        return _criterion(
            "dividend_record", "indeterminate",
            f"截至 {latest_paid} 年连续 {consecutive} 年支付股东股息，{stop_year} 年无现金流量表"
            f"数据{currency_note([stop_year])}，无法确认是否达到 {minimum} 年{tail}",
            float(consecutive),
        )
    stop_note = "现金流量表未列已付股息" if stop_year in not_listed else "未支付股东股息"
    return _criterion(
        "dividend_record", "fail",
        f"截至 {latest_paid} 年连续 {consecutive} 年支付股东股息（{stop_year} 年{stop_note}）{tail}",
        float(consecutive),
    )


def compute_graham_screen(
    market: str,
    statements: Dict[str, List[Dict[str, Any]]],
    daily_basic_rows: Optional[List[Dict[str, Any]]] = None,
    dividend_rows: Optional[List[Dict[str, Any]]] = None,
    *,
    max_years: int = 12,
    valuation: Optional[Dict[str, Any]] = None,
    history_confirmed: bool = True,
) -> Dict[str, Any]:
    """七准则 + 脆弱性信号。statements 为 earnings_quality.market_statements 的输出。

    valuation（港股/美股，调用方从行情库/汇率表/中报季报行装配）：
    {price: {close, currency, date, stale, age_days, source} | None,
     fx_rates: {报表币种: 折价格币种的汇率}, interim_rows: [港股 H1 / 美股单季行],
     share_ratio / share_ratio_note / share_ratio_source('rule'|'20-F') / share_ratio_missing
     （美股 ADS 口径，见 ads_ratio_service）, annual_form}。
    history_confirmed：可得年度数据的起点即公司披露历史的起点（港股由披露易清单确认；
    否则只说「已取得年度数据仅 N 年」，不断言公司历史短）。"""
    thresholds = GRAHAM_DEFENSIVE_THRESHOLDS
    income = _annual_by_year(statements.get("income", []))
    balance = _annual_by_year(statements.get("balancesheet", []))
    cashflow = _annual_by_year(statements.get("cashflow", []))

    years = sorted(set(income) | set(balance), reverse=True)[:max_years]
    if not years:
        return {
            "status": "no_data",
            "criteria_semantics": CRITERIA_SEMANTICS,
            "fragility_semantics": FRAGILITY_SEMANTICS,
            "thresholds": thresholds,
        }
    latest = years[0]
    latest_balance = balance.get(latest)
    latest_income = income.get(latest)

    criteria: List[Dict[str, Any]] = []

    # 1. 流动比率
    cur_assets = _num(latest_balance, "total_cur_assets")
    cur_liab = _num(latest_balance, "total_cur_liab")
    if cur_assets is not None and cur_liab not in (None, 0):
        ratio = cur_assets / cur_liab
        criteria.append(_criterion(
            "current_ratio",
            "pass" if ratio >= thresholds["current_ratio_min"] else "fail",
            f"{latest} 年流动比率 {ratio:.2f}（阈值 ≥ {thresholds['current_ratio_min']}）",
            ratio,
        ))
    else:
        criteria.append(_criterion(
            "current_ratio", "indeterminate", "缺流动资产或流动负债科目",
        ))

    # 2. 长期债务 ≤ 净流动资产
    debt_info = _total_debt(latest_balance, market)  # 脆弱性信号的有息负债口径
    lt_info = _lt_debt(latest_balance, market, balance)  # 准则 2 的长期债务口径
    if cur_assets is not None and cur_liab is not None and lt_info is not None:
        net_current_assets = cur_assets - cur_liab
        debt = lt_info["value"]
        if (
            market == "港股" and not lt_info.get("long_term_only")
            and debt > net_current_assets and net_current_assets < 0
        ):
            # 净流动资产为负：长期债务（≥ 0）无论多少都超过它，无需归因
            criteria.append(_criterion(
                "lt_debt_vs_net_current_assets", "fail",
                f"{latest} 年净流动资产为负（{net_current_assets:,.0f}），"
                "任何非负的长期债务都超过它（数据源只有含短债的合计，不影响结论）",
                debt,
            ))
        elif market == "港股" and not lt_info.get("long_term_only") and debt > net_current_assets:
            # total_debt 含短债（短债已在流动负债里扣过一次），超出时无法归因于长期债务
            criteria.append(_criterion(
                "lt_debt_vs_net_current_assets", "indeterminate",
                f"总有息负债 {debt:,.0f} > 净流动资产 {net_current_assets:,.0f}，"
                "但数据源只有含短债的合计，无法单独判定长期债务",
                debt,
            ))
        else:
            verdict = "pass" if debt <= net_current_assets else "fail"
            label = "长期债务" if lt_info.get("long_term_only") else "有息负债"
            criteria.append(_criterion(
                "lt_debt_vs_net_current_assets", verdict,
                f"{latest} 年{label}（{lt_info['basis']}）{debt:,.0f} "
                f"{'≤' if verdict == 'pass' else '>'} 净流动资产 {net_current_assets:,.0f}",
                debt,
            ))
    elif market == "美股" and edgar_missing_other_currency(latest_balance, "lt_debt"):
        criteria.append(_criterion(
            "lt_debt_vs_net_current_assets", "indeterminate",
            f"{latest} 年长期债务只以非报告币种披露（不混币取数），无法判定",
        ))
    else:
        criteria.append(_criterion(
            "lt_debt_vs_net_current_assets", "indeterminate", "缺债务或流动项科目",
        ))

    # 3. 盈利稳定（亏损年一票 fail；年限不足且全为正 → indeterminate）
    profit_series = [
        (year, _num(income.get(year), "n_income_attr_p", "n_income"))
        for year in years
    ]
    known = [(year, value) for year, value in profit_series if value is not None]
    required_years = int(thresholds["earnings_stability_years"])
    anchor_year = int(latest)
    # 连续覆盖检查（评审 P1）：只数"有 10 条为正的记录"会把缺年冒充成稳定
    # 记录。pass 必须是以最新报告年度为锚、逐年无缺口的连续 N 年；有缺口或
    # 末年缺失一律 indeterminate 并点名缺年。
    # 亏损年触发 fail 也只看锚定窗口 [anchor-9, anchor]（评审 P1 二轮）：
    # years 最多取 12 年，窗口外的旧亏损（如 2014 亏、2016-2025 十年全正）
    # 不该推翻"以最新年度为锚的连续十年为正"这一已声明语义。
    window_floor = anchor_year - required_years + 1
    loss_years = [
        year for year, value in known
        if value <= 0 and window_floor <= int(year) <= anchor_year
    ]
    known_years = {year for year, _ in known}
    contiguous_span = 0
    for offset in range(required_years):
        if str(anchor_year - offset) in known_years:
            contiguous_span += 1
        else:
            break
    missing_years = sorted(
        str(anchor_year - offset)
        for offset in range(required_years)
        if str(anchor_year - offset) not in known_years
    )
    earliest_year = min((int(year) for year in known_years), default=anchor_year)
    # 可得年度数据整段连续、只是起点晚于十年窗口：如实说"历史短"，而不是"缺某某年数据"
    short_history = (
        bool(known) and earliest_year > window_floor
        and contiguous_span == anchor_year - earliest_year + 1
    )
    history_span = anchor_year - earliest_year + 1
    short_history_reason = (
        f"披露历史仅 {history_span} 年（最早 {earliest_year}），不足原著十年"
        if history_confirmed
        else f"已取得的年度数据仅 {history_span} 年（最早 {earliest_year}），不足原著十年"
        "（更早年度未取得）"
    )
    if not known:
        criteria.append(_criterion("earnings_stability", "indeterminate", "无净利润数据"))
    elif loss_years:
        criteria.append(_criterion(
            "earnings_stability", "fail",
            f"{window_floor}→{anchor_year} 窗口内存在亏损/零利润年度："
            f"{'、'.join(sorted(loss_years))}",
        ))
    elif contiguous_span >= required_years:
        criteria.append(_criterion(
            "earnings_stability", "pass",
            f"{anchor_year - required_years + 1}→{anchor_year} 连续 {required_years} 年"
            "净利润均为正",
        ))
    elif short_history:
        criteria.append(_criterion(
            "earnings_stability", "indeterminate",
            f"{short_history_reason}；{earliest_year}→{anchor_year} 净利润均为正",
        ))
    else:
        criteria.append(_criterion(
            "earnings_stability", "indeterminate",
            f"以 {anchor_year} 为锚仅连续覆盖 {contiguous_span} 年（缺 "
            f"{'、'.join(missing_years[:4])}{'…' if len(missing_years) > 4 else ''}），"
            f"不足原著 {required_years} 年连续口径",
        ))

    # 4. 股息记录（连续年数，仅 A股 有 dividend 数据集）
    # 锚定最新报告年度（评审 P1）：只从"最近一次分红"往回数，2015-2019 分过
    # 五年、之后停分的公司也会判 pass。年报分红在次年实施，允许 1 年披露
    # 滞后；最新实施年度更早即视为分红已中断 → fail（有记录但不连续到当下）。
    # 港股/美股（无分红实施数据集）：现金流量表「已付本公司股东股息」逐年判断
    div_years = _dividend_years(dividend_rows or [])
    if market in ("港股", "美股") and not div_years:
        criteria.append(_statement_dividend(
            market, cashflow, int(latest), earliest_year, history_confirmed,
        ))
    elif div_years:
        anchor_year = int(latest)
        latest_div_year = div_years[0]
        if latest_div_year < anchor_year - DIVIDEND_LAG_YEARS:
            criteria.append(_criterion(
                "dividend_record", "fail",
                f"最近一次现金分红为 {latest_div_year} 年，距最新报告年度 {anchor_year} "
                f"已中断（允许 {DIVIDEND_LAG_YEARS} 年披露滞后）",
                0.0,
            ))
        else:
            consecutive = 1
            for idx in range(1, len(div_years)):
                if div_years[idx] == div_years[idx - 1] - 1:
                    consecutive += 1
                else:
                    break
            verdict = "pass" if consecutive >= thresholds["dividend_years_min"] else "fail"
            criteria.append(_criterion(
                "dividend_record", verdict,
                f"截至 {latest_div_year} 年连续现金分红 {consecutive} 年"
                f"（阈值 ≥ {int(thresholds['dividend_years_min'])} 年；原著为 20 年）",
                float(consecutive),
            ))
    else:
        criteria.append(_criterion(
            "dividend_record", "indeterminate",
            "无分红实施记录数据" + ("" if market == "A股" else "（本市场无分红数据源）"),
        ))

    # 5. 盈利增长：**锚定十年窗口 [anchor-9, anchor]**（评审 P1 三轮）：years 最多
    # 保留 12 年，反转取首尾会用 11 年区间套十年阈值——2014 EPS=1、2015-2025
    # EPS=2 会算出 2014→2025 +100% pass，而十年窗口 2016→2025 增长为 0 应 fail。
    # 端点要求：窗口首年（anchor-9）与末年（anchor）都必须有值——首年缺则
    # indeterminate（不用更早/更晚的年份顶替，那会改变判定窗口）。
    # 优先 EPS（净利润增长可能只是增发摊薄的幻象）；EPS 端点不齐才回退净利润。
    growth_first_year = anchor_year - required_years + 1

    def _growth_endpoints(*fields: str):
        first = _num(income.get(str(growth_first_year)), *fields)
        last = _num(income.get(str(anchor_year)), *fields)
        return first, last

    first, last = _growth_endpoints("basic_eps", "diluted_eps")
    growth_basis = "每股盈利"
    if first is None or last is None:
        first, last = _growth_endpoints("n_income_attr_p", "n_income")
        growth_basis = "净利润（缺 EPS 端点，未剔除股本变动）"
    if (first is None or last is None) and short_history:
        criteria.append(_criterion(
            "earnings_growth", "indeterminate",
            f"{short_history_reason}，无法按十年窗口（{growth_first_year}→{anchor_year}）计算增幅",
        ))
    elif first is None or last is None:
        criteria.append(_criterion(
            "earnings_growth", "indeterminate",
            f"十年窗口端点 {growth_first_year}/{anchor_year} 缺{growth_basis}数据，"
            "无法按十年口径计算增幅",
        ))
    elif first <= 0:
        criteria.append(_criterion(
            "earnings_growth", "indeterminate",
            f"期初（{growth_first_year}）{growth_basis}非正，增幅无意义",
        ))
    else:
        growth_pct = (last / first - 1) * 100
        verdict = "pass" if growth_pct >= thresholds["earnings_growth_min_pct"] else "fail"
        criteria.append(_criterion(
            "earnings_growth", verdict,
            f"{growth_first_year}→{anchor_year} {growth_basis}累计增幅 {growth_pct:.1f}%"
            f"（阈值 ≥ {thresholds['earnings_growth_min_pct']:.0f}%，十年窗口）",
            growth_pct,
        ))

    # 6/7. 估值：A股 = daily_basic 快照（pe_ttm/pb，判定口径不变）；港股/美股 = 行情价 ÷ 报表 TTM
    basic = _latest_daily_basic(daily_basic_rows or [])
    annual_income = [income[year] for year in sorted(income, reverse=True)]
    annual_balance = [balance[year] for year in sorted(balance, reverse=True)]
    if market in ("港股", "美股"):
        criteria.extend(_statement_valuation(market, annual_income, annual_balance, valuation))
    else:
        criteria.extend(_daily_basic_valuation(market, basic, annual_income))

    # 塔勒布脆弱性信号
    fragility: Dict[str, Any] = {}
    total_assets = _num(latest_balance, "total_assets")
    total_liab = _num(latest_balance, "total_liab")
    money_cap = _num(latest_balance, "money_cap")
    if total_assets and total_liab is not None:
        fragility["debt_to_assets"] = round(total_liab / total_assets, 4)
    if debt_info is not None and money_cap is not None and total_assets:
        fragility["net_debt_to_assets"] = round(
            (debt_info["value"] - money_cap) / total_assets, 4
        )
        fragility["net_debt_basis"] = debt_info["basis"]
    ebit = _num(latest_income, "operating_income", "operate_profit", "ebit")
    interest = _num(latest_income, "int_exp", "fin_exp")
    if ebit is not None and interest is not None:
        if interest > 0:
            fragility["interest_coverage"] = round(ebit / interest, 2)
        else:
            fragility["interest_coverage_note"] = "利息支出为零或净收益，覆盖倍数无意义"
    total_mv = _num(basic, "total_mv")
    if (
        market == "A股"
        and total_mv
        and debt_info is not None
        and money_cap is not None
    ):
        # daily_basic 总市值单位为万元，报表科目单位为元
        fragility["net_cash_to_market_cap"] = round(
            (money_cap - debt_info["value"]) / (total_mv * 10000), 4
        )

    passed = sum(1 for c in criteria if c["verdict"] == "pass")
    failed = sum(1 for c in criteria if c["verdict"] == "fail")
    return {
        "status": "ok",
        "as_of_year": latest,
        "criteria": criteria,
        "passed": passed,
        "failed": failed,
        "indeterminate": len(criteria) - passed - failed,
        "fragility": fragility,
        "thresholds": thresholds,
        "criteria_semantics": CRITERIA_SEMANTICS,
        "fragility_semantics": FRAGILITY_SEMANTICS,
    }
