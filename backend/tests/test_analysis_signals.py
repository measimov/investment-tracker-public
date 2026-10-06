"""预计算信号（#265）：真实固件（生产只读导出、裁剪到计算用到的字段）上的语义断言，
加标签兜底的构造用例。断言的是口径与定义（去重规则、低基数、币种、缺数据不猜），
不是某只标的的具体数字。"""

import json
from pathlib import Path

import pytest

from app.services import analysis_signals as sig

FIXTURES = Path(__file__).parent / "fixtures" / "signals"


def _load(symbol):
    return json.loads((FIXTURES / f"{symbol}.json").read_text(encoding="utf-8"))


def _signals(symbol):
    data = _load(symbol)
    return data, sig.signals_from_inputs(data["market"], data, data["graham"])


def _year(signals, fiscal_year_end):
    return next(
        item
        for item in signals["shareholder_returns"]["by_year"]
        if item["fiscal_year_end"] == fiscal_year_end
    )


# ---------------------------------------------------------------------------
# 真实固件
# ---------------------------------------------------------------------------


def test_a_share_dividends_dedupe_repeated_implementation_records():
    """Tushare 同一次分配常有多条「实施」（不同公告日）：按 (end_date, ex_date) 去重后，每股分红
    = 各次分配之和（含中期）；直接累加全部实施行会重复计。"""
    data, signals = _signals("600941")
    rows = [r for r in data["dividend_rows"] if r.get("div_proc") == "实施"]
    year = signals["period_signals"]["latest_fy"]["period"][:4]
    in_year = [
        r for r in rows if str(r["end_date"]).startswith(year) and (r["cash_div_tax"] or 0) > 0
    ]
    distinct = {(r["end_date"], r["ex_date"]): r["cash_div_tax"] for r in in_year}
    assert len(in_year) > len(distinct), "固件应包含重复的实施记录"
    assert len({r["end_date"] for r in in_year}) >= 2, "固件应包含中期分配"
    entry = _year(signals, f"{year}1231")
    assert entry["dps"] == pytest.approx(sum(distinct.values()), abs=1e-4)
    assert entry["dps"] < sum(r["cash_div_tax"] for r in in_year)
    # 股息总额 = 每股 × 隐含股数，支付率按每股口径
    assert entry["dividends_total_basis"].startswith("每股分红 × 隐含股数")
    assert 0 < entry["payout_ratio_pct"] < 100


def test_a_share_fcf_gives_both_definitions_when_they_disagree():
    """A股 默认 经营现金流 − |购建长期资产支付的现金|；Tushare free_cashflow 口径不同时并列给出，
    不在两者之间挑一个（300759 2025 两个口径一正一负）。"""
    _, signals = _signals("300759")
    entry = _year(signals, "20251231")
    assert entry["fcf_basis"].startswith("经营现金流 − |购建")
    assert entry["fcf_alternative"]["basis"] == "Tushare free_cashflow"
    assert (entry["fcf_yi"] > 0) != (entry["fcf_alternative"]["value_yi"] > 0)


def test_a_share_period_signals_are_cumulative_and_single_quarter_is_derived():
    _, signals = _signals("600941")
    periods = signals["period_signals"]
    assert periods["latest_interim"]["basis"] == "年初至今累计"
    assert periods["latest_interim"]["prior_period"][4:] == periods["latest_interim"]["period"][4:]
    half = periods["second_half"][0]
    fy = periods["latest_fy"]
    # 下半年 = 全年 − 上半年（按财年）
    assert half["fiscal_year_end"] == fy["period"]
    quarter = periods["latest_single_quarter"]
    assert quarter["basis"] == "累计值相减推算的单季"
    # A股 ROE 直接引用 Tushare 三个口径并标注
    assert set(signals["roe"][0]) >= {"roe_waa(加权平均)", "roe_dt(扣非)"}


def test_low_base_items_are_flagged():
    """占总资产不足 1% 的科目标 low_base（00799 存货：大幅增速但基数极低）。"""
    _, signals = _signals("00799")
    inventories = signals["balance_changes"]["items"]["inventories"]
    assert inventories["share_of_assets_pct"] < 1 and inventories["low_base"] is True
    money = signals["balance_changes"]["items"]["money_cap"]
    assert "low_base" not in money


def test_dividends_exceeding_fcf_are_flagged_with_the_fcf_definition():
    """02313 2025：已付股息超过自由现金流（#265 的原始案例）。港股股息取现金流量表已付股息。"""
    _, signals = _signals("02313")
    entry = _year(signals, "20251231")
    assert entry["dividends_to_fcf_pct"] > 100
    assert "股息超过自由现金流" in entry["flags"]
    assert entry["fcf_basis"] == "经营现金流 − |资本开支|"
    assert entry["dividends_total_basis"].startswith("现金流量表已付本公司股东股息")
    # 存贷双高不下结论，只给两个比例
    items = signals["balance_changes"]["items"]
    assert {"money_cap", "interest_bearing_debt"} <= set(items)
    assert "存贷双高" not in json.dumps(signals, ensure_ascii=False)


def test_hk_dividend_yield_uses_the_valuation_fx_rate():
    """港股股息率为估算：同币种直接算（00799 HKD/HKD）；报表人民币、价格港币时按格雷厄姆估值
    同一汇率折算（02313，汇率 = 每股净资产 × 隐含股数 ÷ 归母权益）。"""
    _, same = _signals("00799")
    yield_ = same["shareholder_returns"]["dividend_yield"]
    assert yield_["estimated"] is True and yield_["value_pct"] > 0 and "汇率" not in yield_["basis"]
    data, different = _signals("02313")
    yield_ = different["shareholder_returns"]["dividend_yield"]
    assert yield_["estimated"] is True and "折为 HKD" in yield_["basis"]
    basis = next(
        c["basis"] for c in data["graham"]["criteria"] if c["criterion"] == "pb_or_product"
    )
    fx = basis["bvps"] * basis["implied_shares"] / basis["equity"]
    paid = _year(different, "20251231")["dividends_total_yi"] * 1e8
    expected = paid * fx / (basis["implied_shares"] * basis["price"]) * 100
    assert yield_["value_pct"] == pytest.approx(expected, abs=0.02)
    # 缺可用汇率：不估算
    no_fx = sig._dividend_yield(
        "港股",
        [{"fp": "FY", "end_date": "20251231", "currency": "CNY", "div_paid": 1e9}],
        None,
        {
            "criteria": [
                {
                    "criterion": "pb_or_product",
                    "basis": {"implied_shares": 1e9, "price": 10.0, "price_currency": "HKD"},
                }
            ]
        },
    )
    assert no_fx["value_pct"] is None and "汇率" in no_fx["note"]


def test_loss_making_company_does_not_produce_misleading_ratios():
    """09926 连年亏损：净利同比按 |上期| 计（亏损扩大为负），没有股息时不给支付率、不做股息对照。"""
    _, signals = _signals("09926")
    fy = signals["period_signals"]["latest_fy"]
    assert fy["net_income"]["value_yi"] < 0 and fy["net_income"]["prior_value_yi"] < 0
    assert fy["net_income"]["yoy_pct"] < 0  # 亏损扩大
    for entry in signals["shareholder_returns"]["by_year"]:
        assert "payout_ratio_pct" not in entry and "dividends_to_fcf_pct" not in entry


def test_us_without_capex_does_not_invent_fcf():
    _, signals = _signals("NFLX")
    for entry in signals["shareholder_returns"]["by_year"]:
        assert "fcf_yi" not in entry and "资本开支" in entry["fcf_note"]
    assert signals["period_signals"]["latest_interim"]["basis"] == "单期"
    assert signals["shareholder_returns"]["ever_paid"] is None  # 不知道 ≠ 从未派息


def test_signals_stay_small_and_empty_inputs_are_no_data():
    for symbol in ("600941", "300759", "00799", "09926", "02313", "NFLX"):
        _, signals = _signals(symbol)
        assert signals["status"] == "ok"
        assert len(json.dumps(signals, ensure_ascii=False)) < 8000, symbol
    assert sig.signals_from_inputs("A股", None, None) == {"status": "no_data"}
    assert sig.compute_signals("A股", []) == {"status": "no_data"}
    assert sig.signals_from_inputs("B股", {"annual": {}}, None) == {"status": "no_data"}


def test_currency_change_blocks_comparison():
    rows = sig.normalize_hk(
        [
            {"end_date": "20251231", "fp": "FY", "currency": "HKD", "total_revenue": 200.0},
            {"end_date": "20241231", "fp": "FY", "currency": "USD", "total_revenue": 100.0},
        ]
    )
    block = sig.period_signals(rows, "港股")["latest_fy"]
    assert block["comparison"] is None and "币种" in block["note"]
    assert "yoy_pct" not in block["revenue"]


def test_dividend_conflicts_keep_latest_and_are_reported():
    rows = [
        {
            "div_proc": "实施",
            "end_date": "20241231",
            "ex_date": "20250606",
            "ann_date": "20250321",
            "cash_div_tax": 1.0,
        },
        {
            "div_proc": "实施",
            "end_date": "20241231",
            "ex_date": "20250606",
            "ann_date": "20250523",
            "cash_div_tax": 1.2,
        },
        # null 与 0 视为同值（送转记录），不算冲突
        {
            "div_proc": "实施",
            "end_date": "20231231",
            "ex_date": "20240606",
            "ann_date": "20240321",
            "cash_div_tax": None,
        },
        {
            "div_proc": "实施",
            "end_date": "20231231",
            "ex_date": "20240606",
            "ann_date": "20240521",
            "cash_div_tax": 0.0,
        },
        {"div_proc": "预案", "end_date": "20221231", "ex_date": None, "cash_div_tax": 9.9},
    ]
    result = sig.a_share_dividends_per_share(rows)
    assert result["by_year"] == {"2024": 1.2}
    assert result["conflicts"] == [{"end_date": "20241231", "ex_date": "20250606"}]


# ---------------------------------------------------------------------------
# 标签兜底
# ---------------------------------------------------------------------------


def _signals_for_tags(
    *,
    fy=(5.0, 5.0),
    interim=(5.0, 5.0),
    ever_paid=True,
    latest_dividends=3.0,
):
    """fy/interim = (营收同比, 归母净利同比)，None 表示缺该期。"""

    def direction(pct):
        return None if pct is None else "上升" if pct > 0 else "下降" if pct < 0 else "持平"

    def block(pair):
        if pair is None:
            return {}
        return {
            "revenue": {"yoy_pct": pair[0], "direction": direction(pair[0])},
            "net_income": {"yoy_pct": pair[1], "direction": direction(pair[1])},
        }

    return {
        "status": "ok",
        "period_signals": {"latest_fy": block(fy), "latest_interim": block(interim)},
        "shareholder_returns": {
            "ever_paid": ever_paid,
            "by_year": [
                {
                    "fiscal_year_end": "20251231",
                    "dividends_total_yi": latest_dividends,
                    "paid_status": "unknown"
                    if latest_dividends is None
                    else "paid"
                    if latest_dividends > 0
                    else "none",
                }
            ],
        },
    }


PASSING_GRAHAM = {
    "criteria": [
        {"criterion": name, "verdict": "pass"}
        for name in ("pe", "pb_or_product", "current_ratio", "lt_debt_vs_net_current_assets")
    ]
}


@pytest.mark.parametrize(
    "tag, signals, graham",
    [
        # 财年与最新中报都不支持才丢
        ("业绩增长", _signals_for_tags(fy=(5.0, -3.0), interim=(2.0, -1.0)), {}),
        ("业绩增长", _signals_for_tags(fy=(0.0, 5.0), interim=None), {}),
        ("业绩下滑", _signals_for_tags(fy=(5.0, 2.0), interim=(5.0, 3.0)), {}),
        ("业绩下滑", _signals_for_tags(fy=(5.0, 2.0), interim=None), {}),  # 无中报：财年决定
        ("分红中断", _signals_for_tags(ever_paid=False), {}),
        ("分红连续", _signals_for_tags(ever_paid=False), {}),
        ("高股息", _signals_for_tags(ever_paid=False), {}),
        ("高股息", _signals_for_tags(latest_dividends=0), {}),
        ("安全边际不足", _signals_for_tags(), PASSING_GRAHAM),
    ],
)
def test_contradicting_tags_are_dropped(tag, signals, graham):
    tags, adjustments = sig.validate_tags([tag, "估值偏低"], signals, graham)
    assert tags == ["估值偏低"]
    assert adjustments[0]["type"] == "tag_dropped_by_signal" and adjustments[0]["tag"] == tag


@pytest.mark.parametrize(
    "tag, signals, graham",
    [
        ("业绩增长", _signals_for_tags(), {}),
        # 财年不支持但最新中报支持（生产回放：财年增长而中报下降的「业绩下滑」同理）
        ("业绩增长", _signals_for_tags(fy=(5.0, -3.0), interim=(8.0, 6.0)), {}),
        ("业绩下滑", _signals_for_tags(fy=(5.0, 24.2), interim=(3.0, -17.4)), {}),
        ("业绩增长", _signals_for_tags(fy=None, interim=None), {}),  # 数据缺失：保留
        ("分红中断", _signals_for_tags(ever_paid=None), {}),  # 不知道 ≠ 从未派息
        ("高股息", _signals_for_tags(latest_dividends=None), {}),  # 股息未知：保留
        (
            "安全边际不足",
            _signals_for_tags(),
            {"criteria": [{"criterion": "pe", "verdict": "fail"}]},
        ),
        ("净现金充裕", _signals_for_tags(), {}),  # 口径覆盖不到，不兜底
        ("审计非标", _signals_for_tags(), {}),  # 与信号无关的标签不动
    ],
)
def test_consistent_or_unknown_tags_are_kept(tag, signals, graham):
    assert sig.validate_tags([tag], signals, graham) == ([tag], [])


def test_validation_never_empties_tags_or_runs_without_signals():
    signals = _signals_for_tags(fy=(5.0, -3.0), interim=(1.0, -2.0))
    tags, adjustments = sig.validate_tags(["业绩增长"], signals, {})
    assert tags == ["业绩增长"] and adjustments[0]["type"] == "tag_signal_conflict"
    assert sig.validate_tags(["业绩增长"], {"status": "error"}, {}) == (["业绩增长"], [])
    assert sig.validate_tags(["业绩增长"], None, None) == (["业绩增长"], [])


# ---------------------------------------------------------------------------
# PR #333 评审回归：缺年、截断窗口、盈亏反转——走真实的 period_signals → validate_tags 链路
# ---------------------------------------------------------------------------


def _hk_rows(*rows):
    return sig.normalize_hk([{"currency": "HKD", **row} for row in rows])


def test_missing_year_is_not_labelled_as_year_over_year():
    """缺 2024 年时，2025 对 2023 不得标成同比（此前输出 prior_period=20231231、同比 100%）。"""
    rows = _hk_rows(
        {
            "end_date": "20251231",
            "fp": "FY",
            "total_revenue": 200e8,
            "n_income_attr_p": 20e8,
            "total_hldr_eqy_exc_min_int": 100e8,
        },
        {
            "end_date": "20231231",
            "fp": "FY",
            "total_revenue": 100e8,
            "n_income_attr_p": 10e8,
            "total_hldr_eqy_exc_min_int": 80e8,
        },
        {"end_date": "20250630", "fp": "H1", "total_revenue": 90e8, "n_income_attr_p": 9e8},
        {"end_date": "20230630", "fp": "H1", "total_revenue": 45e8, "n_income_attr_p": 4e8},
    )
    periods = sig.period_signals(rows, "港股")
    latest = periods["latest_fy"]
    assert latest["comparison"] is None and "prior_period" not in latest
    assert "yoy_pct" not in latest["revenue"] and "direction" not in latest["revenue"]
    # 下半年：两年都能推算，但不相邻，不给同比
    assert all("revenue_h2_yoy_pct" not in item for item in periods["second_half"])
    # ROE：期初权益不拿两年前的顶替
    roe = sig.roe_by_year(rows, "港股")[0]
    assert roe["formula"].endswith("（缺上一财年末）") and roe["roe_pct"] == 20.0
    # 缺年的公司，业绩标签没有证据可判，保留
    signals = {"status": "ok", "period_signals": periods}
    assert sig.validate_tags(["业绩下滑"], signals, {}) == (["业绩下滑"], [])


def test_adjacent_year_still_compares():
    rows = _hk_rows(
        {"end_date": "20251231", "fp": "FY", "total_revenue": 200e8, "n_income_attr_p": 20e8},
        {"end_date": "20241231", "fp": "FY", "total_revenue": 100e8, "n_income_attr_p": 10e8},
    )
    latest = sig.period_signals(rows, "港股")["latest_fy"]
    assert latest["prior_period"] == "20241231"
    assert latest["revenue"] == {
        "value_yi": 200.0,
        "prior_value_yi": 100.0,
        "yoy_pct": 100.0,
        "direction": "上升",
    }


def test_ever_paid_uses_full_history_and_never_concludes_never_paid_for_hk():
    """截断窗口（最近 5 年）里没付不能推出从未派息：2020 年付过、2021–2025 为 0 → 曾派息，
    「分红中断」保留；只有最近 3 年已知为 0 → 无法确认，也保留。"""
    six_years = _hk_rows(
        *[
            {"end_date": f"{year}1231", "fp": "FY", "div_paid_owners": 0.0, "n_income_attr_p": 1e8}
            for year in range(2025, 2020, -1)
        ],
        {"end_date": "20201231", "fp": "FY", "div_paid_owners": 3e8, "n_income_attr_p": 1e8},
    )
    returns = sig.shareholder_returns(six_years, "港股")
    assert len(returns["by_year"]) == sig.MAX_YEARS  # 逐年块仍只给 5 年
    assert returns["ever_paid"] is True and returns["recent_unpaid_years"] == 5
    signals = {"status": "ok", "shareholder_returns": returns}
    assert sig.validate_tags(["分红中断"], signals, {}) == (["分红中断"], [])

    three_zero = _hk_rows(
        *[
            {"end_date": f"{year}1231", "fp": "FY", "div_paid_owners": 0.0}
            for year in (2025, 2024, 2023)
        ]
    )
    returns = sig.shareholder_returns(three_zero, "港股")
    assert returns["ever_paid"] is None and returns["recent_unpaid_years"] == 3
    signals = {"status": "ok", "shareholder_returns": returns}
    assert sig.validate_tags(["分红中断"], signals, {}) == (["分红中断"], [])


@pytest.mark.parametrize(
    "fy, interim, tag, kept",
    [
        # 中报由盈转亏（5 亿 → −1 亿，没有百分比）而财年增长 20%：「业绩下滑」有中报依据，保留
        ((10e8, 12e8), (5e8, -1e8), "业绩下滑", True),
        # 中报由亏转盈而财年下降：「业绩下滑」有财年依据，保留；「业绩增长」看营收也上升才成立
        ((12e8, 10e8), (-1e8, 5e8), "业绩下滑", True),
        ((12e8, 10e8), (-1e8, 5e8), "业绩增长", True),
        # 两期都由亏转盈：「业绩下滑」两期都不支持，丢
        ((-2e8, 3e8), (-1e8, 5e8), "业绩下滑", False),
        # 两期都由盈转亏：「业绩增长」两期都不支持，丢
        ((3e8, -2e8), (5e8, -1e8), "业绩增长", False),
    ],
)
def test_sign_flips_are_judged_by_direction(fy, interim, tag, kept):
    rows = _hk_rows(
        {"end_date": "20251231", "fp": "FY", "total_revenue": 110e8, "n_income_attr_p": fy[1]},
        {"end_date": "20241231", "fp": "FY", "total_revenue": 100e8, "n_income_attr_p": fy[0]},
        {"end_date": "20260630", "fp": "H1", "total_revenue": 60e8, "n_income_attr_p": interim[1]},
        {"end_date": "20250630", "fp": "H1", "total_revenue": 50e8, "n_income_attr_p": interim[0]},
    )
    signals = {"status": "ok", "period_signals": sig.period_signals(rows, "港股")}
    tags, adjustments = sig.validate_tags([tag, "估值偏低"], signals, {})
    if kept:
        assert tags == [tag, "估值偏低"] and adjustments == []
    else:
        assert tags == ["估值偏低"]
        assert "→" in adjustments[0]["reason"]  # 理由写出两期原值与方向


def test_previous_period_end_must_be_within_thirteen_months():
    rows = _hk_rows(
        {"end_date": "20251231", "fp": "FY", "total_assets": 100e8, "inventories": 5e8},
        {"end_date": "20231231", "fp": "FY", "total_assets": 90e8, "inventories": 4e8},
    )
    items = sig.balance_changes(rows)["items"]["inventories"]
    assert "vs_previous_period" not in items and "vs_year_ago" not in items


def test_ever_paid_reads_full_a_share_dividend_history_beyond_100_rows():
    """PR #333 复审 P2：A股 分红表以预案/股东大会/实施等过程行计，老公司可超过 100 行。按 100 行
    截断时最早一次现金分派被挤出窗口，ever_paid 误为 False 并删掉正确的「分红中断」。
    这里走 load_signal_inputs 真实取数：101 行里最早一条是现金分派，其后 100 条为零现金。"""
    from app.database import SessionLocal
    from app.models.security_profile import SecurityProfileData
    from app.services.security_profile_service import load_signal_inputs

    symbol = "600DIVHIS"
    db = SessionLocal()
    try:
        db.query(SecurityProfileData).filter(SecurityProfileData.symbol == symbol).delete()

        def add(dataset, period_key, payload):
            db.add(
                SecurityProfileData(
                    symbol=symbol,
                    market="A股",
                    dataset=dataset,
                    period_key=period_key,
                    payload=payload,
                )
            )

        add("income", "20251231", {"end_date": "20251231", "n_income_attr_p": 1e8})
        add(
            "dividend_history",
            "19951231|实施|19960601",
            {
                "end_date": "19951231",
                "div_proc": "实施",
                "ann_date": "19960601",
                "ex_date": "19960615",
                "cash_div_tax": 0.5,
            },
        )
        for index in range(100):  # 之后 100 条零现金记录（送转/预案/不分配）
            year = 1996 + index // 4
            proc = ("预案", "股东大会通过", "实施", "不分配")[index % 4]
            add(
                "dividend_history",
                f"{year}1231|{proc}|{year + 1}0{index % 4 + 1}01",
                {
                    "end_date": f"{year}1231",
                    "div_proc": proc,
                    "ann_date": f"{year + 1}0{index % 4 + 1}01",
                    "ex_date": f"{year + 1}0615" if proc == "实施" else None,
                    "cash_div_tax": 0.0,
                    "stk_div": 0.3,
                },
            )
        db.commit()

        inputs = load_signal_inputs(db, symbol, "A股")
        assert len(inputs["dividend_rows"]) == 101
        signals = sig.signals_from_inputs("A股", inputs, {})
        assert signals["shareholder_returns"]["ever_paid"] is True
        tags, adjustments = sig.validate_tags(["分红中断", "估值偏低"], signals, {})
        assert tags == ["分红中断", "估值偏低"] and adjustments == []
    finally:
        db.query(SecurityProfileData).filter(SecurityProfileData.symbol == symbol).delete()
        db.commit()
        db.close()


@pytest.mark.parametrize(
    "rows, expected",
    [
        # 正常连续三年为零
        ([("20251231", 0.0), ("20241231", 0.0), ("20231231", 0.0)], 3),
        # 缺年：2024/2022 未知，只能算最新一年（PR #333 复审复现）
        ([("20251231", 0.0), ("20231231", 0.0), ("20211231", 0.0)], 1),
        # 中间一年缺支付字段：到此为止
        ([("20251231", 0.0), ("20241231", None), ("20231231", 0.0)], 1),
        # 最新一年付过：0
        ([("20251231", 2e8), ("20241231", 0.0)], 0),
        # 非日历财年（3 月末）连续
        ([("20260331", 0.0), ("20250331", 0.0)], 2),
    ],
)
def test_recent_unpaid_years_stops_at_gaps(rows, expected):
    annual = _hk_rows(
        *[
            {"end_date": end, "fp": "FY", **({"div_paid_owners": paid} if paid is not None else {})}
            for end, paid in rows
        ]
    )
    returns = sig.shareholder_returns(annual, "港股")
    assert returns.get("recent_unpaid_years", 0) == expected


# ---------------------------------------------------------------------------
# PR #333 后评审：分红去重唯一定义、原始值判定
# ---------------------------------------------------------------------------


def test_signals_and_model_input_pick_the_same_dividend_revision():
    """同一次分配两条「实施」：A 的实施公告日较新但公告日较旧（1.2），B 相反（1.0）。signals 与
    送模型的分红记录必须选同一条（此前各用一套规则，选出 1.2 与 1.0 两个金额）。"""
    from app.services import security_analysis_jobs as jobs

    rows = [
        {
            "div_proc": "实施",
            "end_date": "20241231",
            "ex_date": "20250606",
            "imp_ann_date": "20250601",
            "ann_date": "20250301",
            "cash_div_tax": 1.2,
        },
        {
            "div_proc": "实施",
            "end_date": "20241231",
            "ex_date": "20250606",
            "imp_ann_date": "20250520",
            "ann_date": "20250525",
            "cash_div_tax": 1.0,
        },
    ]
    per_share = sig.a_share_dividends_per_share(rows)
    compact = jobs._compact_statement_rows("dividend_history", rows)
    assert per_share["by_year"] == {"2024": 1.2}
    assert [row["cash_div_tax"] for row in compact] == [1.2]
    assert per_share["conflicts"] == [{"end_date": "20241231", "ex_date": "20250606"}]


@pytest.mark.parametrize(
    "div_paid, status, high_dividend_kept",
    [
        (4e5, "paid", True),  # 40 万元：展示值 0.00 亿，但不是未派息
        (0.0, "none", False),  # 真实为零
        (None, "unknown", True),  # 未知
    ],
)
def test_paid_status_uses_raw_amount_not_the_rounded_display(div_paid, status, high_dividend_kept):
    rows = _hk_rows(
        {
            "end_date": "20251231",
            "fp": "FY",
            "n_income_attr_p": 1e8,
            **({"div_paid_owners": div_paid} if div_paid is not None else {}),
        },
    )
    returns = sig.shareholder_returns(rows, "港股")
    latest = returns["by_year"][0]
    assert latest["paid_status"] == status
    if div_paid:
        assert latest["dividends_total_yi"] == 0.0  # 展示值确实舍入成 0
    tags, _ = sig.validate_tags(
        ["高股息", "估值偏低"], {"status": "ok", "shareholder_returns": returns}, {}
    )
    assert ("高股息" in tags) is high_dividend_kept
