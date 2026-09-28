"""披露易现金股息公告（EF001）解析：真实表格金样 + 取代关系 + 复权因子纯函数。

金样 `tests/fixtures/hkex_dividend/`：2026-09-28 从生产容器实测下载的表格（pdfplumber
逐页 extract_text 以换行拼接）。覆盖：港元末期（00700）、宣派人民币派港元的更新公告
（00728/03900/06049，含 H 股代扣税表）、中期 + 红筹 10% 企业所得税说明（00883）、
美元宣派美元派发（09618）、末期特別股息（00148）、「其他/特別股息」股息類型折行
（00799）、中期（01579）、2022 年「更新/撤回理由」标签在值之后的更新公告（01579）、
2022 年金额与除淨日「有待公佈」的待定首份公告（00728）。纯函数，不连库。

v2 金样（同日生产缓存里 18 份未解析表格的原文，挑 12 份覆盖全部变体，见 V2_GOLDEN）：
EF002 可選擇貨幣（00270 新/更新、02688）、EF003 可選擇以股份代替（02156 更新、00288
实物分派）、報告期末「不適用」退到財政年末（06049 2022 末期两份更新、02688、02669 特別）、
報告期末与財政年末都「不適用」的特別股息（00288/00878/09898，含简体「特别」）、
「撤回股息公告」+「已撤回之股息信息」（00878）。
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.services import hkex_dividend_source as src
from app.services.hk_adjustment_factors import (
    compute_backward_adj_factors,
    cross_rate_fn,
    dividend_amount_in,
)
from app.services.portfolio.fx import ExchangeRateLookup

FIXTURES = Path(__file__).parent / "fixtures" / "hkex_dividend"


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# (文件, 代码, 状态, 类型, 性质, 报告期末, 宣派, 派发, 汇率, 除净日, 记录日, 派息日,
#  暂停过户, 代扣适用, 税率集合, 非居民企业税率, 港股通个人税率)
GOLDEN = [
    ("00700_final_2025.txt", "00700", "new", "末期", "普通股息", date(2025, 12, 31),
     ("5.3", "HKD"), ("5.3", "HKD"), ("HKD", "HKD", "1"), date(2026, 5, 15),
     date(2026, 5, 20), date(2026, 6, 1), (date(2026, 5, 19), date(2026, 5, 20)),
     False, [], None, None),
    ("00728_final_2025_update.txt", "00728", "update", "末期", "普通股息", date(2025, 12, 31),
     ("0.0908", "CNY"), ("0.10391", "HKD"), ("CNY", "HKD", "1.144387"), date(2026, 6, 2),
     date(2026, 6, 9), date(2026, 7, 8), (date(2026, 6, 4), date(2026, 6, 9)),
     True, ["10", "20"], "10", "20"),
    ("00883_interim_2026.txt", "00883", "new", "中期（半年期）", "普通股息", date(2026, 6, 30),
     ("0.94", "HKD"), ("0.94", "HKD"), ("HKD", "HKD", "1"), date(2026, 9, 10),
     date(2026, 9, 18), date(2026, 10, 16), (date(2026, 9, 14), date(2026, 9, 18)),
     True, ["10"], "10", None),
    ("03900_final_2024_update.txt", "03900", "update", "末期", "普通股息", date(2024, 12, 31),
     ("0.3", "CNY"), ("0.328", "HKD"), ("CNY", "HKD", "1.0941"), date(2025, 6, 25),
     date(2025, 7, 2), date(2025, 7, 31), (date(2025, 6, 27), date(2025, 7, 2)),
     False, [], None, None),
    ("06049_final_2025_update.txt", "06049", "update", "末期", "普通股息", date(2025, 12, 31),
     ("1.401", "CNY"), ("1.60706", "HKD"), ("CNY", "HKD", "1.14708"), date(2026, 6, 2),
     date(2026, 6, 5), date(2026, 7, 15), (date(2026, 6, 4), date(2026, 6, 5)),
     True, ["10", "20"], "10", "20"),
    ("09618_final_2025_usd.txt", "09618", "new", "末期", "普通股息", date(2025, 12, 31),
     ("0.5", "USD"), ("0.5", "USD"), ("USD", "USD", "1"), date(2026, 4, 8),
     date(2026, 4, 9), date(2026, 4, 23), None,
     False, [], None, None),
    ("00148_special_final_2025.txt", "00148", "new", "末期", "特別股息", date(2025, 12, 31),
     ("0.4", "HKD"), ("0.4", "HKD"), ("HKD", "HKD", "1"), date(2026, 6, 11),
     date(2026, 6, 18), date(2026, 7, 8), (date(2026, 6, 15), date(2026, 6, 18)),
     False, [], None, None),
    ("00799_special_2025.txt", "00799", "new", "其他 特別股息", "特別股息", date(2025, 12, 31),
     ("0.477", "HKD"), ("0.477", "HKD"), ("HKD", "HKD", "1"), date(2026, 4, 10),
     date(2026, 4, 16), date(2026, 4, 27), (date(2026, 4, 14), date(2026, 4, 16)),
     False, [], None, None),
    ("01579_final_2021_update.txt", "01579", "update", "末期", "普通股息", date(2021, 12, 31),
     ("0.219563", "CNY"), ("0.27006249", "HKD"), ("CNY", "HKD", "1.23"), date(2022, 5, 24),
     date(2022, 5, 30), date(2022, 6, 16), (date(2022, 5, 26), date(2022, 5, 30)),
     False, [], None, None),
    ("01579_interim_2026.txt", "01579", "new", "中期（半年期）", "普通股息", date(2026, 6, 30),
     ("0.4188", "HKD"), ("0.4188", "HKD"), ("HKD", "HKD", "1"), date(2026, 9, 8),
     date(2026, 9, 15), date(2026, 9, 24), (date(2026, 9, 10), date(2026, 9, 15)),
     False, [], None, None),
]


PENDING_FIXTURES = ["00728_final_2021_pending.txt"]

# v2 变体金样：(文件, 代码, 模板, 状态, 类型, 性质, 锚点来源, 报告期末, 財政年末, 公告日期,
#  派发, 除净日, 待定, 以股代息, 可选货币)
V2_GOLDEN = [
    ("00270_interim_2026_ef002.txt", "00270", "EF002", "new", "中期（半年期）", "普通股息",
     "period_end", date(2026, 6, 30), date(2026, 12, 31), date(2026, 8, 28),
     ("0.2919", "HKD"), date(2026, 9, 11), False, False, True),
    ("00270_interim_2026_ef002_update.txt", "00270", "EF002", "update", "中期（半年期）",
     "普通股息", "period_end", date(2026, 6, 30), date(2026, 12, 31), date(2026, 9, 21),
     ("0.2919", "HKD"), date(2026, 9, 11), False, False, True),
    ("02688_final_2023_ef002.txt", "02688", "EF002", "new", "末期", "普通股息",
     "financial_year_end", None, date(2023, 12, 31), date(2024, 3, 22),
     ("2.31", "HKD"), date(2024, 6, 5), False, False, True),
    ("02156_final_2022_ef003_update.txt", "02156", "EF003", "update", "末期", "普通股息",
     "period_end", date(2022, 12, 31), date(2022, 12, 31), date(2023, 6, 8),
     ("0.1", "HKD"), date(2023, 6, 1), False, True, False),
    ("00288_special_2025_ef003.txt", "00288", "EF003", "new",
     "其他 史密斯菲爾德食品有限公司之股票", "特別股息", "financial_year_end", None,
     date(2025, 12, 31), date(2025, 2, 6), ("0.01673", "HKD"), date(2025, 2, 18),
     False, True, False),
    ("00288_special_2025_no_period.txt", "00288", "EF001", "new", "其他 特別股息", "特別股息",
     "none", None, None, date(2025, 2, 28), ("0.18", "HKD"), date(2025, 3, 13),
     False, False, False),
    # 更新公告沿用原公告日期（2023-03-29），清单时间 2023-04-25/05-17
    ("06049_final_2022_update_pending.txt", "06049", "EF001", "update", "末期", "普通股息",
     "financial_year_end", None, date(2022, 12, 31), date(2023, 3, 29),
     None, date(2023, 6, 8), True, False, False),
    ("06049_final_2022_update.txt", "06049", "EF001", "update", "末期", "普通股息",
     "financial_year_end", None, date(2022, 12, 31), date(2023, 3, 29),
     ("0.56795", "HKD"), date(2023, 6, 8), False, False, False),
    ("02669_special_2025.txt", "02669", "EF001", "new", "其他 特別股息", "特別股息",
     "financial_year_end", None, date(2025, 12, 31), date(2025, 8, 25),
     ("0.01", "HKD"), date(2025, 9, 19), False, False, False),
    ("00878_special_2025_no_period.txt", "00878", "EF001", "new", "其他 特別", "特別股息",
     "none", None, None, date(2025, 4, 30), ("1", "HKD"), date(2025, 5, 28),
     False, False, False),
    ("00878_special_2025_withdrawal.txt", "00878", "EF001", "withdrawal", "其他 特別",
     "特別股息", "none", None, None, date(2025, 5, 26), None, None, False, False, False),
    ("09898_special_2024_no_period.txt", "09898", "EF001", "new", "其他 微博股份有限公司",
     "特別股息", "none", None, None, date(2024, 3, 14), ("0.82", "USD"), date(2024, 4, 11),
     False, False, False),
]


def test_every_fixture_has_a_golden_row():
    assert sorted(p.name for p in FIXTURES.glob("*.txt")) == sorted(
        [g[0] for g in GOLDEN] + PENDING_FIXTURES + [g[0] for g in V2_GOLDEN]
    )


@pytest.mark.parametrize("golden", GOLDEN, ids=[g[0] for g in GOLDEN])
def test_parse_real_forms(golden):
    (name, code, kind, dtype, nature, period_end, declared, payment, rate, ex_date,
     record_date, pay_date, book_close, wh_applicable, rates, nre, southbound) = golden
    form, reason = src.parse_dividend_form_with_reason(_text(name))
    assert reason is None
    assert form["stock_code"] == code
    assert form["status_kind"] == kind
    assert form["dividend_type"] == dtype
    assert form["dividend_nature"] == nature
    assert form["period_end"] == period_end
    assert form["declared"] == {"amount": Decimal(declared[0]), "currency": declared[1]}
    assert form["payment"] == {"amount": Decimal(payment[0]), "currency": payment[1]}
    assert form["exchange_rate"] == {"from": rate[0], "to": rate[1], "rate": Decimal(rate[2])}
    assert (form["ex_date"], form["record_date"], form["pay_date"]) == (
        ex_date, record_date, pay_date
    )
    assert form["book_close"] == (
        {"start": book_close[0], "end": book_close[1]} if book_close else None
    )
    withholding = form["withholding"]
    assert withholding["applicable"] is wh_applicable
    assert withholding["rates_percent"] == [Decimal(r) for r in rates]
    assert withholding["non_resident_enterprise_percent"] == (Decimal(nre) if nre else None)
    assert withholding["southbound_individual_percent"] == (
        Decimal(southbound) if southbound else None
    )
    assert form["scrip_option"] is False
    assert form["currency_election"] is False
    assert form["pending"] is False
    # JSON 往返（缓存 payload）无损
    assert src.form_from_json(src.form_to_json(form)) == form


def test_pending_first_announcement_is_valid_but_pending():
    """首份公告只定了宣派金额：派发金额/汇率/除淨日「有待公佈」→ 待定，不拿宣派 RMB 顶替。"""
    form, reason = src.parse_dividend_form_with_reason(_text("00728_final_2021_pending.txt"))
    assert reason is None
    assert form["pending"] is True
    assert form["status_kind"] == "new"
    assert (form["dividend_type"], form["period_end"]) == ("末期", date(2021, 12, 31))
    assert form["declared"] == {"amount": Decimal("0.17"), "currency": "CNY"}
    assert form["payment"] is None
    assert form["ex_date"] is None and form["record_date"] is None
    assert form["pay_date"] == date(2022, 7, 18)
    assert src.form_from_json(src.form_to_json(form)) == form


def test_label_after_value_layout():
    """2022 年版式：「更新/撤回理由」标签在值的下一行。"""
    form = src.parse_dividend_form(_text("01579_final_2021_update.txt"))
    assert form["update_reason"] == "修訂匯率及公司預設派發貨幣"


def test_unrecognized_payment_text_is_not_replaced_by_declared():
    text = _text("00728_final_2025_update.txt").replace("每 股 0.10391HKD", "港元，另行公佈")
    form, reason = src.parse_dividend_form_with_reason(text)
    assert form is None and "無法識別派息金額" in reason


def test_update_forms_carry_reason_and_announcement_date():
    form = src.parse_dividend_form(_text("00728_final_2025_update.txt"))
    assert form["status"] == "更新公告"
    assert form["update_reason"] == "更新派付末期股息的匯率及末期股息港元金額"
    assert form["announcement_date"] == date(2026, 5, 19)
    # 06049 的更新公告标题里没有「更新」：以公告狀態为准
    assert src.parse_dividend_form(_text("06049_final_2025_update.txt"))["status_kind"] == "update"


@pytest.mark.parametrize("name", ["00700_final_2025", "00883_interim_2026"])
def test_pdf_text_extraction_matches_snapshot(name):
    data = (FIXTURES / f"{name}.pdf").read_bytes()
    assert src.extract_pdf_text(data) == _text(f"{name}.txt")


# ---------------------------------------------------------------------------
# 认不出就返回 None（不猜）
# ---------------------------------------------------------------------------


def test_not_a_dividend_form():
    form, reason = src.parse_dividend_form_with_reason("董事會會議召開日期\n股份代號 00700")
    assert form is None and "不是" in reason


@pytest.mark.parametrize("mutate, expected", [
    (lambda t: t.replace("除淨日 2026年5月15日", "除淨日 待定"), "缺少除淨日"),
    (lambda t: t.replace("公告狀態 新公告", "公告狀態 其他"), "無法識別的公告狀態"),
    # 「不適用」退到財政年末（test_period_not_applicable_falls_back_to_financial_year_end），
    # 缺失或写了认不出的内容才是缺项
    (lambda t: t.replace("宣派股息的報告期末 2025年12月31日", "宣派股息的報告期末 待定"),
     "缺少宣派股息的報告期末"),
    (lambda t: t.replace("宣派股息的報告期末 2025年12月31日", "宣派股息的報告期末 不適用")
     .replace("財政年末 2025年12月31日", "財政年末 待定"), "缺少宣派股息的報告期末"),
    (lambda t: t.replace(" 每 股 5.3HKD", ""), "缺少每股派息金額"),
    (lambda t: t.replace("每 股 5.3HKD", "待定"), "無法識別派息金額"),
    (lambda t: t.replace("公告日期 2026年3月18日", "公告日期"), "缺少公告日期"),
])
def test_missing_required_fields_return_reason(mutate, expected):
    form, reason = src.parse_dividend_form_with_reason(mutate(_text("00700_final_2025.txt")))
    assert form is None
    assert expected in reason


def test_payment_falls_back_to_declared_only_when_same_currency():
    text = _text("00700_final_2025.txt").replace(
        "派息金額及公司預設派發貨幣 每 股 5.3HKD", "派息金額及公司預設派發貨幣"
    )
    form = src.parse_dividend_form(text)
    assert form["payment"] == {"amount": Decimal("5.3"), "currency": "HKD"}

    rmb = _text("00728_final_2025_update.txt").replace(
        "派息金額及公司預設派發貨幣 每 股 0.10391HKD", "派息金額及公司預設派發貨幣"
    )
    form, reason = src.parse_dividend_form_with_reason(rmb)
    assert form is None and "宣派與派發幣種不同" in reason


def test_amount_variants():
    assert src.parse_per_share_amount("每 10 股 3HKD") == {
        "amount": Decimal("0.3"), "currency": "HKD"
    }
    assert src.parse_per_share_amount("每股 RMB 0.25") == {
        "amount": Decimal("0.25"), "currency": "CNY"
    }
    assert src.parse_per_share_amount("每 股 1,234.5USD") == {
        "amount": Decimal("1234.5"), "currency": "USD"
    }
    assert src.parse_per_share_amount("每 股 HKD 0.3 RMB") is None  # 两个币种不猜
    assert src.parse_per_share_amount("不適用") is None
    assert src.parse_exchange_rate("1 RMB : 1.0869HKD") == {
        "from": "CNY", "to": "HKD", "rate": Decimal("1.0869")
    }


def test_withdrawal_form_parses_without_ex_date():
    text = (
        _text("00700_final_2025.txt")
        .replace("公告狀態 新公告", "公告狀態 撤回公告\n更新/撤回理由 董事會撤回派息建議")
        .replace("除淨日 2026年5月15日", "除淨日 不適用")
    )
    form = src.parse_dividend_form(text)
    assert form["status_kind"] == "withdrawal"
    assert form["update_reason"] == "董事會撤回派息建議"


def test_scrip_and_currency_election_flags():
    text = _text("00700_final_2025.txt").replace(
        "其他信息 不適用",
        "其他信息 股東可選擇以股代息，或選擇以人民幣貨幣收取現金股息。",
    )
    form = src.parse_dividend_form(text)
    assert form["scrip_option"] is True
    assert form["currency_election"] is True


# ---------------------------------------------------------------------------
# v2：EF002/EF003、報告期末「不適用」、撤回股息公告
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("golden", V2_GOLDEN, ids=[g[0] for g in V2_GOLDEN])
def test_parse_v2_variants(golden):
    (name, code, template, kind, dtype, nature, basis, period_end, fy, announced, payment,
     ex_date, pending, scrip, currency_election) = golden
    form, reason = src.parse_dividend_form_with_reason(_text(name))
    assert reason is None
    assert (form["stock_code"], form["template"], form["status_kind"]) == (code, template, kind)
    assert (form["dividend_type"], form["dividend_nature"]) == (dtype, nature)
    assert (form["period_basis"], form["period_end"], form["financial_year_end"]) == (
        basis, period_end, fy
    )
    assert form["announcement_date"] == announced
    assert form["payment"] == (
        {"amount": Decimal(payment[0]), "currency": payment[1]} if payment else None
    )
    assert form["ex_date"] == ex_date
    assert form["pending"] is pending
    assert (form["scrip_option"], form["currency_election"]) == (scrip, currency_election)
    assert src.form_from_json(src.form_to_json(form)) == form


def test_ef002_currency_options():
    """EF002：每股金额取预设派发货币（港元），人民币选项只作展示；待定的选项不让整份待定。"""
    first = src.parse_dividend_form(_text("00270_interim_2026_ef002.txt"))
    assert first["pending"] is False
    assert first["currency_options"] == {
        "options": [{"currency": "CNY", "amount": None, "exchange_rate": None, "pending": True}],
        "partial_election": False,
        "election_deadline": "2026-10-07 16:30",
    }
    update = src.parse_dividend_form(_text("00270_interim_2026_ef002_update.txt"))
    assert update["update_reason"] == "更新匯率"
    assert update["currency_options"]["options"] == [{
        "currency": "CNY", "amount": "0.2516301",
        "exchange_rate": {"from": "HKD", "to": "CNY", "rate": "0.862042"}, "pending": False,
    }]
    form = src.parse_dividend_form(_text("02688_final_2023_ef002.txt"))
    assert form["currency_options"]["partial_election"] is True
    assert form["currency_options"]["options"][0]["amount"] == "2.096052"
    assert form["withholding"]["non_resident_enterprise_percent"] == Decimal("10")
    assert form["scrip"] is None


def test_ef003_scrip_fields():
    """EF003：预设选项 + 代息股份信息；「可以部分現金及部分新股方式收取股 / 是 / 息」折行。"""
    form = src.parse_dividend_form(_text("02156_final_2022_ef003_update.txt"))
    assert form["update_reason"] == "確定現金股息轉換為代息股份的價格"
    assert form["scrip"] == {
        "default_option": "現金",
        "default_cash": True,
        "price": {"amount": "3.48", "currency": "HKD"},
        "price_pending": False,
        "certificate_date": "2023-07-07",
        "first_trading_date": "2023-07-10",
        "partial_election": True,
        "fraction_handling": "下調至最接近整數單位",
        "election_deadline": "2023-06-27 16:30",
    }
    assert form["currency_options"] is None
    specie = src.parse_dividend_form(_text("00288_special_2025_ef003.txt"))
    assert specie["scrip"]["price"] == {"amount": "155.6", "currency": "HKD"}
    assert specie["scrip"]["partial_election"] is False
    # 预设选项写的是股份：不作选择的股东收到新股，标记出来（金额口径不变）
    shares_default = src.parse_dividend_form(
        _text("02156_final_2022_ef003_update.txt").replace("預設選項 現金", "預設選項 代息股份")
    )
    assert shares_default["scrip"]["default_cash"] is False
    assert shares_default["payment"] == {"amount": Decimal("0.1"), "currency": "HKD"}


def test_type_on_own_line_and_simplified_char_are_normalized():
    """「其他 / 股息類型 / 特别」与「特別」同一写法；新公告与撤回公告身份一致。"""
    new = src.parse_dividend_form(_text("00878_special_2025_no_period.txt"))
    withdrawal = src.parse_dividend_form(_text("00878_special_2025_withdrawal.txt"))
    assert new["dividend_type"] == withdrawal["dividend_type"] == "其他 特別"
    assert src.normalize_type_text(" 其他  特别股息 ") == "其他 特別股息"
    assert withdrawal["status"] == "撤回股息公告"
    assert withdrawal["update_reason"].startswith("由於該計劃於2025年5月23日")
    assert withdrawal["ex_date"] is None and withdrawal["payment"] is None


def test_period_not_applicable_falls_back_to_financial_year_end():
    text = _text("00700_final_2025.txt").replace(
        "宣派股息的報告期末 2025年12月31日", "宣派股息的報告期末 不適用"
    )
    form = src.parse_dividend_form(text)
    assert (form["period_basis"], form["period_end"], form["financial_year_end"]) == (
        "financial_year_end", None, date(2025, 12, 31)
    )
    assert src.dividend_identity(form) == (date(2025, 12, 31), "末期", "普通股息")
    # 報告期末写明的末期股息：同一个锚点值，但来源不同（取代关系据此区分描述方式）
    explicit = src.parse_dividend_form(_text("00700_final_2025.txt"))
    assert src.dividend_identity(explicit) == src.dividend_identity(form)
    assert src.period_basis(explicit) == "period_end"


def test_no_period_identity_only_for_new_forms():
    new = src.parse_dividend_form(_text("00288_special_2025_no_period.txt"))
    assert src.dividend_identity(new) == (
        ("no-period", date(2025, 2, 28)), "其他 特別股息", "特別股息"
    )
    withdrawal = src.parse_dividend_form(_text("00878_special_2025_withdrawal.txt"))
    assert src.dividend_identity(withdrawal)[0] is None
    update = src.parse_dividend_form(
        _text("00288_special_2025_no_period.txt").replace("公告狀態 新公告", "公告狀態 更新公告")
    )
    assert update is not None and src.dividend_identity(update)[0] is None
    # 解析失败的无期间表格：只有状态认得出是新公告才有身份
    garbled_new = _text("00288_special_2025_no_period.txt").replace(
        "除淨日 2025年3月13日", "除淨日 另行公佈"
    )
    assert src.parse_dividend_form(garbled_new) is None
    assert src.partial_identity(garbled_new) == (
        ("no-period", date(2025, 2, 28)), "其他 特別股息", "特別股息"
    )
    assert src.partial_identity(
        garbled_new.replace("公告狀態 新公告", "公告狀態 更新公告")
    ) is None


# ---------------------------------------------------------------------------
# 取代关系
# ---------------------------------------------------------------------------


def _entry(form, sort_key):
    return {"form": form, "sort_key": sort_key, "url": None, "doc_id": sort_key[1]}


def test_update_supersedes_and_withdrawal_cancels():
    update = src.parse_dividend_form(_text("00728_final_2025_update.txt"))
    original = {
        **update, "status": "新公告", "status_kind": "new",
        "payment": {"amount": Decimal("0.1"), "currency": "HKD"},
        "ex_date": date(2026, 6, 1),
    }
    interim = {**update, "period_end": date(2025, 6, 30), "ex_date": date(2025, 9, 1)}
    current = src.resolve_current_dividends([
        _entry(update, ("2026-05-19T21:44:00", "2026051901240")),
        _entry(original, ("2026-03-24T17:08:00", "2026032400515")),
        _entry(interim, ("2025-08-14T17:19:00", "2025081400750")),
    ])
    assert [(e["form"]["ex_date"], e["form"]["payment"]["amount"]) for e in current] == [
        (date(2025, 9, 1), Decimal("0.10391")),
        (date(2026, 6, 2), Decimal("0.10391")),
    ]

    # 最新一份是待定公告：进待定列表，不拿更早公告的旧值顶替
    pending = {**update, "pending": True, "ex_date": None, "payment": None}
    current, pending_list = src.resolve_dividend_states([
        _entry(original, ("2026-03-24T17:08:00", "2026032400515")),
        _entry(pending, ("2026-05-19T21:44:00", "2026051901240")),
    ])
    assert current == [] and [e["form"] for e in pending_list] == [pending]

    withdrawal = {**update, "status": "撤回公告", "status_kind": "withdrawal"}
    current = src.resolve_current_dividends([
        _entry(update, ("2026-05-19T21:44:00", "2026051901240")),
        _entry(withdrawal, ("2026-05-20T09:00:00", "2026052000001")),
    ])
    assert current == []


def _cached(doc_id, listed_at, text):
    return {
        "doc_id": doc_id, "url": f"https://x/{doc_id}.pdf", "listed_at": listed_at,
        **src._build_payload({"doc_id": doc_id, "url": f"https://x/{doc_id}.pdf",
                              "listed_at": listed_at}, text),
    }


def test_unparsable_newer_update_blocks_instead_of_reviving_old_form():
    """PR #249 评审 P2 的离线复现：旧的正常公告 + 更新的、派息金额认不出的更新公告。"""
    old = _cached("2026031800477", "2026-03-18T17:09:00", _text("00700_final_2025.txt"))
    newer = _cached(
        "2026040100001", "2026-04-01T09:00:00",
        _text("00700_final_2025.txt")
        .replace("公告狀態 新公告", "公告狀態 更新公告")
        .replace("派息金額及公司預設派發貨幣 每 股 5.3HKD", "派息金額及公司預設派發貨幣 見附件"),
    )
    assert newer["status"] == "unparsed"
    entries = src.cached_entries([old, newer])
    assert src.resolve_current_dividends(entries) == []
    resolution = src.resolve_dividend_resolution(entries)
    (blocked,) = resolution.blocked
    assert blocked["entry"]["doc_id"] == "2026040100001"
    assert resolution.protected_ex_dates == {date(2026, 5, 15)}
    assert resolution.unscoped == []

    # 认不出的公告比现行公告更早（例如旧版式）：不影响更新的现行值
    older_unparsed = _cached(
        "2026010100001", "2026-01-01T09:00:00",
        _text("00700_final_2025.txt").replace("派息金額及公司預設派發貨幣 每 股 5.3HKD",
                                          "派息金額及公司預設派發貨幣 見附件"),
    )
    entries = src.cached_entries([old, older_unparsed])
    assert [e["doc_id"] for e in src.resolve_current_dividends(entries)] == ["2026031800477"]


def _without_nature(text):
    return text.replace("股息性質 普通股息\n", "")


def test_unparsable_update_missing_nature_is_unscoped_not_a_new_identity():
    """PR #249 第二轮评审 P2 的离线复现：更新公告金额认不出且缺「股息性質」。

    只认前两项会得到 (期末, 类型, None) 这个与现行股息 (…, 普通股息) 对不上的新身份：
    旧公告照样现行、protected 也不含它的除净日。必须按身份不完整整标的挂起。
    """
    old = _cached("2026031800477", "2026-03-18T17:09:00", _text("00700_final_2025.txt"))
    newer = _cached(
        "2026040100001", "2026-04-01T09:00:00",
        _without_nature(
            _text("00700_final_2025.txt")
            .replace("公告狀態 新公告", "公告狀態 更新公告")
            .replace("派息金額及公司預設派發貨幣 每 股 5.3HKD", "派息金額及公司預設派發貨幣 見附件")
        ),
    )
    assert "股息性質" not in newer["text"] and newer["status"] == "unparsed"
    resolution = src.resolve_dividend_resolution(src.cached_entries([old, newer]))
    assert [e["doc_id"] for e in resolution.unscoped] == ["2026040100001"]
    assert resolution.blocked == []


def test_nature_is_required_even_when_everything_else_parses():
    form, reason = src.parse_dividend_form_with_reason(_without_nature(_text("00700_final_2025.txt")))
    assert form is None and reason == "缺少股息性質"


def test_withdrawal_missing_nature_is_unscoped():
    withdrawal = _without_nature(
        _text("00700_final_2025.txt")
        .replace("公告狀態 新公告", "公告狀態 撤回公告")
        .replace("除淨日 2026年5月15日", "除淨日 不適用")
    )
    form = src.parse_dividend_form(withdrawal)
    assert form is not None and form["status_kind"] == "withdrawal"
    old = _cached("2026031800477", "2026-03-18T17:09:00", _text("00700_final_2025.txt"))
    cancel = _cached("2026040100002", "2026-04-01T10:00:00", withdrawal)
    resolution = src.resolve_dividend_resolution(src.cached_entries([old, cancel]))
    assert [e["doc_id"] for e in resolution.unscoped] == ["2026040100002"]


def test_unparsed_form_without_identity_is_unscoped():
    garbled = _cached("2026040100003", "2026-04-01T11:00:00",
                      _text("00700_final_2025.txt").replace("宣派股息的報告期末 2025年12月31日", "")
                      .replace("除淨日 2026年5月15日", ""))
    assert garbled["status"] == "unparsed"
    resolution = src.resolve_dividend_resolution(src.cached_entries([garbled]))
    assert [e["doc_id"] for e in resolution.unscoped] == ["2026040100003"]


# --- v2 取代关系：財政年末锚点 / 无期间 ---


def _06049_2022_original():
    """06049 2022 末期的原公告（生产缓存里没有）：按更新公告还原成新公告，報告期末同为不適用。"""
    return (
        _text("06049_final_2022_update_pending.txt")
        .replace("公告狀態 更新公告", "公告狀態 新公告")
        .replace("更新/撤回理由 補充代扣所得稅及香港過戶登記處相關信息\n", "")
    )


def test_fy_fallback_update_supersedes_original_with_same_identity():
    original = _cached("2023032900001", "2023-03-29T17:00:00", _06049_2022_original())
    pending = _cached("2023042502546", "2023-04-25T20:36:00",
                      _text("06049_final_2022_update_pending.txt"))
    final = _cached("2023051700956", "2023-05-17T22:50:00", _text("06049_final_2022_update.txt"))
    assert {p["status"] for p in (original, pending, final)} == {"ok"}
    forms = [src.form_from_json(p["form"]) for p in (original, pending, final)]
    assert {src.dividend_identity(f) for f in forms} == {(date(2022, 12, 31), "末期", "普通股息")}

    resolution = src.resolve_dividend_resolution(src.cached_entries([original, pending, final]))
    assert resolution.unscoped == [] and resolution.blocked == []
    ((entry),) = resolution.current
    assert entry["doc_id"] == "2023051700956"
    assert entry["form"]["payment"] == {"amount": Decimal("0.56795"), "currency": "HKD"}
    # 只到待定那一份时：待定，不拿原公告的宣派人民币顶替
    resolution = src.resolve_dividend_resolution(src.cached_entries([original, pending]))
    assert resolution.current == [] and [e["doc_id"] for e in resolution.pending] == [
        "2023042502546"
    ]


@pytest.mark.parametrize("first_explicit", [True, False])
def test_fy_fallback_does_not_tie_forms_written_differently(first_explicit):
    """原公告写明報告期末、更新公告写「不適用」（或反之）：锚点值相同也不认作同一笔——
    描述方式变了无法确认，整标的挂起，旧公告不得继续现行（PR #249 的保证）。"""
    explicit = _text("00700_final_2025.txt")
    loose = explicit.replace("宣派股息的報告期末 2025年12月31日", "宣派股息的報告期末 不適用")
    first, second = (explicit, loose) if first_explicit else (loose, explicit)
    update = second.replace("公告狀態 新公告", "公告狀態 更新公告").replace(
        "派息金額及公司預設派發貨幣 每 股 5.3HKD", "派息金額及公司預設派發貨幣 每 股 6.1HKD"
    )
    old = _cached("2026031800477", "2026-03-18T17:09:00", first)
    newer = _cached("2026040100001", "2026-04-01T09:00:00", update)
    resolution = src.resolve_dividend_resolution(src.cached_entries([old, newer]))
    assert [e["doc_id"] for e in resolution.unscoped] == ["2026040100001"]
    assert "寫法不同" in resolution.unscoped[0]["reason"]


def test_fy_fallback_update_cannot_claim_an_interim_special_of_the_same_year():
    """中期特別（報告期末 6-30）与一份「不適用」退到財政年末的特別股息更新公告：可能是同一笔。"""
    interim_special = (
        _text("00700_final_2025.txt")
        .replace("宣派股息的報告期末 2025年12月31日", "宣派股息的報告期末 2025年6月30日")
        .replace("股息類型 末期", "股息類型 其他 特別股息")
        .replace("股息性質 普通股息", "股息性質 特別股息")
    )
    update = (
        interim_special.replace("公告狀態 新公告", "公告狀態 更新公告")
        .replace("宣派股息的報告期末 2025年6月30日", "宣派股息的報告期末 不適用")
    )
    entries = src.cached_entries([
        _cached("2025082000001", "2025-08-20T17:00:00", interim_special),
        _cached("2025090100001", "2025-09-01T17:00:00", update),
    ])
    assert [e["doc_id"] for e in src.resolve_dividend_resolution(entries).unscoped] == [
        "2025090100001"
    ]


def test_fy_fallback_of_other_year_or_type_does_not_interfere():
    """06049 2022 末期（不適用→財政年末）与 2023 末期（写明報告期末）是两个族，互不影响。"""
    entries = src.cached_entries([
        _cached("2023042502546", "2023-04-25T20:36:00",
                _text("06049_final_2022_update_pending.txt")),
        _cached("2023051700956", "2023-05-17T22:50:00", _text("06049_final_2022_update.txt")),
        _cached("2026052901240", "2026-05-29T18:04:00", _text("06049_final_2025_update.txt")),
    ])
    resolution = src.resolve_dividend_resolution(entries)
    assert resolution.unscoped == []
    assert [e["doc_id"] for e in resolution.current] == ["2023051700956", "2026052901240"]


def test_no_period_new_specials_are_standalone():
    entries = src.cached_entries([
        _cached("2025020601554", "2025-02-06T18:49:00", _text("00288_special_2025_ef003.txt")),
        _cached("2025022800888", "2025-02-28T17:26:00",
                _text("00288_special_2025_no_period.txt")),
        # 同一标的另一份无期间特別股息（不同公告日期）：各自一笔
        _cached("2025092201467", "2025-09-22T19:37:00",
                _text("00288_special_2025_no_period.txt")
                .replace("公告日期 2025年2月28日", "公告日期 2025年9月22日")
                .replace("除淨日 2025年3月13日", "除淨日 2025年10月6日")),
    ])
    resolution = src.resolve_dividend_resolution(entries)
    assert resolution.unscoped == [] and resolution.blocked == []
    assert [(e["form"]["ex_date"], e["form"]["payment"]["amount"]) for e in resolution.current] == [
        (date(2025, 2, 18), Decimal("0.01673")),
        (date(2025, 3, 13), Decimal("0.18")),
        (date(2025, 10, 6), Decimal("0.18")),
    ]


@pytest.mark.parametrize("kind", ["update", "withdrawal"])
def test_no_period_update_or_withdrawal_is_unscoped(kind):
    new = _cached("2025022800888", "2025-02-28T17:26:00", _text("00288_special_2025_no_period.txt"))
    later = _text("00288_special_2025_no_period.txt")
    if kind == "update":
        later = later.replace("公告狀態 新公告", "公告狀態 更新公告").replace(
            "派息金額及公司預設派發貨幣 每 股 0.18HKD", "派息金額及公司預設派發貨幣 每 股 0.2HKD"
        )
    else:
        later = later.replace("公告狀態 新公告", "公告狀態 撤回股息公告")
    newer = _cached("2025030500001", "2025-03-05T17:00:00", later)
    assert newer["status"] == "ok"
    resolution = src.resolve_dividend_resolution(src.cached_entries([new, newer]))
    assert [e["doc_id"] for e in resolution.unscoped] == ["2025030500001"]
    assert "均不適用" in resolution.unscoped[0]["reason"]


def test_00878_no_period_special_then_withdrawal_blocks_symbol():
    """00878：私有化附带的无期间特別股息（新公告）→ 计划未获批准「撤回股息公告」。
    撤回公告认不出对应哪一笔（公告日期不稳定、无期间），整标的挂起——不能让特別股息
    继续当现行值被写成建议。"""
    entries = src.cached_entries([
        _cached("2025043000208", "2025-04-30T06:33:00",
                _text("00878_special_2025_no_period.txt")),
        _cached("2025052600440", "2025-05-26T16:40:00",
                _text("00878_special_2025_withdrawal.txt")),
    ])
    resolution = src.resolve_dividend_resolution(entries)
    assert [e["doc_id"] for e in resolution.unscoped] == ["2025052600440"]
    assert resolution.unscoped[0]["form"]["status_kind"] == "withdrawal"


def test_two_new_forms_colliding_on_a_loose_identity_are_unscoped():
    """两份「不適用」退到同一財政年末的新公告、除净日不同：可能是两笔，不让后者静默取代前者。"""
    first = _text("02669_special_2025.txt")
    second = first.replace("除淨日 2025年9月19日", "除淨日 2026年6月23日")
    entries = src.cached_entries([
        _cached("2025082500603", "2025-08-25T16:46:00", first),
        _cached("2026032600001", "2026-03-26T16:35:00", second),
    ])
    assert [e["doc_id"] for e in src.resolve_dividend_resolution(entries).unscoped] == [
        "2026032600001"
    ]
    # 同一除净日的重复新公告：按最新一份去重，不挂起
    entries = src.cached_entries([
        _cached("2025082500603", "2025-08-25T16:46:00", first),
        _cached("2025082500999", "2025-08-25T18:00:00", first),
    ])
    resolution = src.resolve_dividend_resolution(entries)
    assert resolution.unscoped == []
    assert [e["doc_id"] for e in resolution.current] == ["2025082500999"]


def test_stale_cached_payload_is_reparsed_in_memory():
    """只读缓存路径（复权因子重算）：v1 解析失败的缓存行按原文在内存里重解析。"""
    v1 = {
        "parser_version": 1, "doc_id": "2024032200356", "url": "https://x/2024032200356.pdf",
        "listed_at": "2024-03-22T16:32:00", "status": "unparsed",
        "reason": "不是「股票發行人現金股息公告」表格", "form": None,
        "text": _text("02688_final_2023_ef002.txt"),
    }
    (entry,) = src.cached_entries([v1])
    assert not entry.get("unresolved")
    assert entry["form"]["template"] == "EF002"
    assert entry["sort_key"] == ("2024-03-22T16:32:00", "2024032200356")


def test_document_id():
    assert src.document_id(
        "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0318/2026031800477_c.pdf"
    ) == "2026031800477"


# ---------------------------------------------------------------------------
# 复权因子（纯函数）
# ---------------------------------------------------------------------------


def _hkd(value):
    return {"amount": Decimal(value), "currency": "HKD"}


def _no_rate(*_args):
    return None


def test_backward_factor_hkd_dividend():
    prices = [
        (date(2026, 5, 13), Decimal("500"), "HKD"),
        (date(2026, 5, 14), Decimal("530"), "HKD"),
        (date(2026, 5, 15), Decimal("525"), "HKD"),  # 除净日
        (date(2026, 5, 18), Decimal("520"), "HKD"),
    ]
    out = compute_backward_adj_factors(
        prices, [{"ex_date": date(2026, 5, 15), "payment": _hkd("5.3"), "declared": _hkd("5.3")}],
        _no_rate,
    )
    ratio = (Decimal("530") - Decimal("5.3")) / Decimal("530")
    assert out["factors"][date(2026, 5, 13)] == Decimal("1")
    assert out["factors"][date(2026, 5, 14)] == Decimal("1")
    assert out["factors"][date(2026, 5, 15)] == 1 / ratio
    assert out["factors"][date(2026, 5, 18)] == 1 / ratio
    # 比例法：除净日的复权收益率 = 除净价 / (前收 − 股息)，与 Tushare 复权同一口径
    adj = {d: c * out["factors"][d] for d, c, _ in prices}
    assert (adj[date(2026, 5, 15)] / adj[date(2026, 5, 14)]).quantize(Decimal("1e-12")) == (
        Decimal("525") / (Decimal("530") - Decimal("5.3"))
    ).quantize(Decimal("1e-12"))
    assert out["events"][0]["status"] == "applied"


def test_same_ex_date_components_are_summed_and_factors_compound():
    prices = [
        (date(2026, 1, 2), Decimal("10"), "HKD"),
        (date(2026, 3, 2), Decimal("10"), "HKD"),
        (date(2026, 6, 11), Decimal("9"), "HKD"),
        (date(2026, 9, 1), Decimal("9"), "HKD"),
        (date(2026, 9, 10), Decimal("9"), "HKD"),
    ]
    dividends = [
        {"ex_date": date(2026, 3, 2), "payment": _hkd("0.5"), "declared": None},
        {"ex_date": date(2026, 6, 11), "payment": _hkd("0.6"), "declared": None},  # 末期
        {"ex_date": date(2026, 6, 11), "payment": _hkd("0.4"), "declared": None},  # 特別
    ]
    out = compute_backward_adj_factors(prices, dividends, _no_rate)
    r1 = Decimal("9.5") / Decimal("10")
    r2 = Decimal("9") / Decimal("10")  # 前收 10、合计 1.0
    assert out["factors"][date(2026, 3, 2)] == 1 / r1
    assert out["factors"][date(2026, 6, 11)] == (1 / r1) / r2
    assert [e["components"] for e in out["events"]] == [1, 2]


def test_rmb_dividend_is_converted_to_hkd_via_cny_cross():
    """宣派与派发都是人民币（无港元金额）→ 除净日（含）之前最近汇率经 CNY 交叉折港元。"""

    class Rate:
        def __init__(self, f, t, r, d):
            self.from_currency, self.to_currency = f, t
            self.rate, self.effective_date = Decimal(r), d

    lookup = ExchangeRateLookup([
        Rate("HKD", "CNY", "0.90", date(2026, 5, 1)),
        Rate("HKD", "CNY", "0.95", date(2026, 7, 1)),  # 未来汇率不得使用
        Rate("USD", "CNY", "7.20", date(2026, 5, 1)),
    ])
    rate_fn = cross_rate_fn(lookup)
    rmb = {"amount": Decimal("0.9"), "currency": "CNY"}
    component = {"ex_date": date(2026, 6, 2), "payment": rmb, "declared": rmb}
    q = Decimal("1e-12")
    assert dividend_amount_in(component, "HKD", date(2026, 6, 2), rate_fn).quantize(q) == 1
    usd = {"amount": Decimal("0.5"), "currency": "USD"}
    assert dividend_amount_in(
        {"payment": usd, "declared": usd}, "HKD", date(2026, 6, 2), rate_fn
    ) == Decimal("0.5") * Decimal("7.20") / Decimal("0.90")
    # 宣派 RMB、派发 HKD → 直接用派发金额，不折汇
    mixed = {"payment": _hkd("0.10391"), "declared": {"amount": Decimal("0.0908"),
                                                      "currency": "CNY"}}
    assert dividend_amount_in(mixed, "HKD", date(2026, 6, 2), _no_rate) == Decimal("0.10391")

    prices = [(date(2026, 6, 1), Decimal("10"), "HKD"), (date(2026, 6, 2), Decimal("9"), "HKD")]
    out = compute_backward_adj_factors(prices, [component], rate_fn)
    assert out["factors"][date(2026, 6, 2)].quantize(q) == (
        1 / (Decimal("9") / Decimal("10"))
    ).quantize(q)


def test_missing_rate_or_oversized_dividend_leaves_later_factors_unknown():
    prices = [
        (date(2026, 1, 2), Decimal("10"), "HKD"),
        (date(2026, 2, 2), Decimal("10"), "HKD"),
        (date(2026, 3, 2), Decimal("10"), "HKD"),
    ]
    usd = {"amount": Decimal("0.1"), "currency": "USD"}
    out = compute_backward_adj_factors(
        prices, [{"ex_date": date(2026, 2, 2), "payment": usd, "declared": usd}], _no_rate
    )
    assert out["factors"] == {
        date(2026, 1, 2): Decimal("1"), date(2026, 2, 2): None, date(2026, 3, 2): None,
    }
    assert out["events"][0]["reason"] == "missing_fx_rate"

    out = compute_backward_adj_factors(
        prices, [{"ex_date": date(2026, 2, 2), "payment": _hkd("10"), "declared": None}],
        _no_rate,
    )
    assert out["factors"][date(2026, 2, 2)] is None
    assert out["events"][0]["reason"] == "dividend_not_below_prev_close"


def test_events_outside_price_series():
    prices = [(date(2026, 3, 2), Decimal("10"), "HKD"), (date(2026, 3, 3), Decimal("10"), "HKD")]
    out = compute_backward_adj_factors(prices, [
        {"ex_date": date(2025, 6, 1), "payment": _hkd("1"), "declared": None},
        {"ex_date": date(2026, 9, 1), "payment": _hkd("1"), "declared": None},
    ], _no_rate)
    assert [e["status"] for e in out["events"]] == ["before_series", "pending"]
    assert set(out["factors"].values()) == {Decimal("1")}


# ---------------------------------------------------------------------------
# 清单（HTTP 层 mock）
# ---------------------------------------------------------------------------


def test_list_dividend_forms_queries_form_category(monkeypatch):
    """披露易检索：类别参数 + result 是再包一层的 JSON 字符串（2026-09-28 实测形状）。"""
    import json

    from app.services import report_fetchers

    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": json.dumps([
                {"DATE_TIME": "18/03/2026 17:09",
                 "TITLE": "截至二零二五年十二月三十一日止年度末期股息",
                 "FILE_LINK": "/listedco/listconews/sehk/2026/0318/2026031800477_c.pdf"},
                {"DATE_TIME": "18/03/2026 17:09", "TITLE": "附件", "FILE_LINK": "/x/y.htm"},
            ], ensure_ascii=False)}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, params, headers))
        return Response()

    monkeypatch.setattr(report_fetchers, "hkex_stock_id", lambda code: "7609")
    monkeypatch.setattr(report_fetchers, "_throttle", lambda *a: None)
    monkeypatch.setattr(report_fetchers.requests, "get", fake_get)

    forms = src.list_dividend_forms("00700", date(2025, 1, 1), date(2026, 9, 29))
    assert forms == [{
        "doc_id": "2026031800477",
        "title": "截至二零二五年十二月三十一日止年度末期股息",
        "listed_at": "2026-03-18T17:09:00",
        "url": "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0318/2026031800477_c.pdf",
    }]
    ((url, params, headers),) = calls
    assert url.endswith("/search/titleSearchServlet.do")
    assert (params["stockId"], params["t1code"], params["t2Gcode"], params["t2code"]) == (
        "7609", 10000, 3, 13251
    )
    assert (params["fromDate"], params["toDate"]) == ("20250101", "20260929")
    assert "Referer" in headers
