"""分析输入去噪音与收缩顺序（#332）：字段白名单、同源去重、分红例证条数、逐级收缩。"""

import ast
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.services import security_analysis_jobs as saj
from app.services.security_analysis_prompts import build_analysis_messages

SERVICES = Path(__file__).resolve().parent.parent / "app" / "services"


def test_model_ratio_display_preserves_original_judgements_and_non_ratio_precision():
    payload = {
        "earnings_quality": {
            "per_year": {
                "2025": {
                    "accruals_ratio": -0.0517,
                    "recurring_profit_share": 1.0034,
                    "cfo_ni_ratio": 0.4483,
                    "gross_margin": 2.5955,
                }
            },
            "cfo_ni_ratio_5y": 1.2345,
            "beneish_m_score": {
                "2025": {"score": -1.7801, "flag": False, "factors": {"DSRI": 1.2345}}
            },
        },
        "signals": {"yield": {"value_pct": 2.5955}, "dps": 0.0034, "revenue_yi": 12.3456},
        "graham_screen": {
            "criteria": [
                {
                    "criterion": "current_ratio",
                    "value": 1.9999,
                    "verdict": "fail",
                    "basis": {"eps": 0.003456, "fx_rate": 7.123456},
                }
            ],
            "fragility": {"debt_to_assets": 0.4567, "interest_coverage": 1.2345},
        },
        "profile": {"income": [{"basic_eps": 0.003456, "revenue": 123456.789012}]},
    }
    original = deepcopy(payload)
    message = build_analysis_messages(payload)[1]["content"]
    sent = json.loads(message.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert payload == original
    assert sent["earnings_quality"]["per_year"]["2025"] == {
        "accruals_ratio": -0.05,
        "recurring_profit_share": 1.0,
        "cfo_ni_ratio": 0.45,
        "gross_margin": 2.6,
    }
    assert sent["earnings_quality"]["cfo_ni_ratio_5y"] == 1.23
    assert sent["earnings_quality"]["beneish_m_score"]["2025"] == {
        "score": -1.78,
        "flag": False,
        "factors": {"DSRI": 1.23},
    }
    assert sent["signals"] == {"yield": {"value_pct": 2.6}, "dps": 0.0034, "revenue_yi": 12.3456}
    assert sent["graham_screen"]["criteria"][0]["value"] == 2.0
    assert sent["graham_screen"]["criteria"][0]["verdict"] == "fail"
    assert (
        sent["graham_screen"]["criteria"][0]["basis"]
        == original["graham_screen"]["criteria"][0]["basis"]
    )
    assert sent["graham_screen"]["fragility"] == {"debt_to_assets": 0.46, "interest_coverage": 1.23}
    assert sent["profile"] == original["profile"]


def test_tiny_signed_ratios_are_not_presented_as_zero_and_unknown_stays_unknown():
    payload = {
        "signals": {
            "yield": {"value_pct": 0.000123},
            "latest_fy": {"revenue": {"yoy_pct": -0.000123}},
            "dividend": [{"payout_ratio_pct": 0.0, "dividends_to_fcf_pct": None}],
        }
    }
    sent = json.loads(
        build_analysis_messages(payload)[1]["content"].split("```json\n", 1)[1].split("\n```", 1)[0]
    )
    assert sent["signals"] == {
        "yield": {"value_pct": "大于 0 且小于 0.01"},
        "latest_fy": {"revenue": {"yoy_pct": "大于 -0.01 且小于 0"}},
        "dividend": [{"payout_ratio_pct": 0.0, "dividends_to_fcf_pct": None}],
    }


FINA_FIELDS = set(
    json.loads(
        (Path(__file__).parent / "fixtures" / "tushare" / "fina_indicator_fields.json").read_text(
            encoding="utf-8"
        )
    )["fields"]
)
# 引用模块里出现、但**不是**对 fina_indicator 字段的引用（同名的输出键等），逐条写明理由
NOT_FINA_REFERENCES = {
    "gross_margin": "earnings_quality 输出的 per_year 键（值取自 grossprofit_margin）；fina 的同名字段是毛利额",
    "ann_date": "公告/分红等其他数据集的日期字段",
    "ts_code": "Tushare 代码，取数时用，不是分析数据",
    "end_date": "所有数据集共用的期末日",
    "eps": "report_statements / EDGAR 行上的每股盈利同名键",
    "ebitda": "港股报表的 EBITDA 科目",
}
# 分析用到 fina_indicator 字段的模块：信号、利润质量、格雷厄姆、提示词
REFERENCING_MODULES = (
    "analysis_signals.py",
    "earnings_quality.py",
    "graham_screen.py",
    "security_analysis_prompts.py",
)


def _string_constants(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_every_fina_field_the_analysis_code_reads_is_whitelisted():
    """新增对 fina_indicator 某字段的引用而忘了加白名单 → 模型拿不到这个数，这里直接报红。"""
    whitelist = set(saj.STATEMENT_LLM_FIELDS["fina_indicator"])
    referenced = set()
    for name in REFERENCING_MODULES:
        referenced |= _string_constants(SERVICES / name) & FINA_FIELDS
    missing = sorted(referenced - whitelist - set(NOT_FINA_REFERENCES))
    assert not missing, f"这些 fina_indicator 字段被分析代码引用但不在白名单里：{missing}"
    # 白名单里的每个字段都确实是 fina_indicator 的字段（防拼错）
    assert whitelist <= FINA_FIELDS
    assert "gross_margin" not in whitelist  # 毛利额，与毛利率字段长得像（#289）


def test_compact_profile_whitelists_dedupes_and_limits_examples():
    fina_row = {field: 1.0 for field in FINA_FIELDS} | {"end_date": "20251231"}
    xueqiu = [
        {"end_date": end, "total_revenue": 1.1, "total_revenue_yoy": 0.1, "ctime": 1}
        for end in ("20251231", "20241231", "20231231", "20221231")
    ]
    income = [
        {"end_date": end, "total_revenue": 1.0} for end in ("20260630", "20251231", "20241231")
    ]
    dividends = [
        {
            "end_date": f"{year}1231",
            "ann_date": f"{year + 1}0401",
            "div_proc": "实施",
            "cash_div_tax": 1.0,
            "ex_date": f"{year + 1}0601",
        }
        for year in range(2015, 2026)
    ]
    compact = saj._compact_profile(
        {
            "fina_indicator": [fina_row],
            "xueqiu_income": xueqiu,
            "income": income,
            "dividend_history": dividends,
        }
    )
    assert set(compact["fina_indicator"][0]) == set(saj.STATEMENT_LLM_FIELDS["fina_indicator"])
    # 两源值不一致时仍按 Tushare 优先；雪球只补缺失期间，且不带 _yoy / ctime
    assert [row["end_date"] for row in compact["xueqiu_income"]] == ["20231231", "20221231"]
    assert set(compact["xueqiu_income"][0]) == {"end_date", "total_revenue"}
    # 分红原始记录只留最近 4 条作例证
    assert [row["end_date"] for row in compact["dividend_history"]] == [
        "20251231",
        "20241231",
        "20231231",
        "20221231",
    ]


def test_xueqiu_rows_are_kept_when_tushare_income_is_missing():
    compact = saj._compact_profile({"xueqiu_income": [{"end_date": "20251231", "op": 2.0}]})
    assert compact["xueqiu_income"] == [{"end_date": "20251231", "op": 2.0}]


# ---------------------------------------------------------------------------
# 逐级收缩
# ---------------------------------------------------------------------------


def _payload(events=40, peers=20, announcements=30, digest_chars=0):
    big = "x" * 200
    return {
        "meta": {},
        "profile": {"income": [{"f": "y"}]},
        "events": [{"e": big} for _ in range(events)],
        "announcements": [{"t": big} for _ in range(announcements)],
        "peers": {"list": [{"n": big} for _ in range(peers)]},
        "report_digests": [{"d": "z" * digest_chars}] if digest_chars else [],
    }


@pytest.fixture
def profile_loader(monkeypatch):
    calls = []

    def fake_load(db, symbol, market, caps=None, **kw):
        calls.append(caps)
        # 核心数据集按封顶返回行数，非核心数据集同样：体积随封顶变化
        return {
            "datasets": {
                "income": [{"f": "c" * 300} for _ in range(caps["income"])],
                "stk_holdertrade": [{"f": "n" * 300} for _ in range(caps["stk_holdertrade"])],
            }
        }

    monkeypatch.setattr(saj, "load_symbol_profile", fake_load)
    monkeypatch.setattr(saj, "_compact_profile", lambda datasets: datasets)
    from app.services import report_digest_service

    monkeypatch.setattr(report_digest_service, "load_report_digests", lambda *a, **k: [])
    monkeypatch.setattr(
        report_digest_service, "serialize_digest_for_analysis", lambda *a, **k: [{"d": "short"}]
    )
    return calls


def test_within_budget_nothing_is_cut(monkeypatch, profile_loader):
    monkeypatch.setattr(saj, "CHAR_BUDGET", 10**7)
    payload = saj.shrink_analysis_payload(None, "T", "A股", _payload())
    assert payload["meta"]["input_shrink"] == []
    assert "profile_data_gaps" not in payload and len(payload["events"]) == 40
    assert profile_loader == []


def test_auxiliary_context_goes_first_and_evidence_is_untouched(monkeypatch, profile_loader):
    payload = _payload()
    size_after_aux = saj._payload_chars(
        {
            **payload,
            "events": payload["events"][:8],
            "announcements": payload["announcements"][:10],
            "peers": {"list": payload["peers"]["list"][:8]},
        }
    )
    monkeypatch.setattr(saj, "CHAR_BUDGET", size_after_aux + 200)
    result = saj.shrink_analysis_payload(None, "T", "A股", payload)
    assert result["meta"]["input_shrink"] == ["aux"]
    assert len(result["events"]) == saj.EVENTS_SHRUNK_CAP
    assert result["profile"] == {"income": [{"f": "y"}]}  # 报表没动
    assert profile_loader == []
    assert result["profile_data_gaps"] == [saj.SHRINK_GAP_NOTES["aux"]]


def test_noncore_datasets_shrink_before_digests_and_statements(monkeypatch, profile_loader):
    monkeypatch.setattr(saj, "CHAR_BUDGET", 1)  # 永远超：逐级走到底
    result = saj.shrink_analysis_payload(None, "T", "A股", _payload(digest_chars=5000))
    assert result["meta"]["input_shrink"] == ["aux", "profile_noncore", "digests", "statements"]
    noncore_caps, all_caps = profile_loader
    # 第二级只减半非核心数据集，报表封顶保持原样；最后一级才减报表
    assert noncore_caps["income"] == saj.ANALYSIS_CAPS["income"]
    assert noncore_caps["stk_holdertrade"] == saj.SHRUNK_CAPS["stk_holdertrade"]
    assert all_caps["income"] == saj.SHRUNK_CAPS["income"]
    assert result["report_digests"] == [{"d": "short"}]
    assert result["profile_data_gaps"] == [
        saj.SHRINK_GAP_NOTES[level] for level in result["meta"]["input_shrink"]
    ]


def test_core_dataset_list_covers_every_statement_like_dataset():
    for dataset in (
        "income",
        "balancesheet",
        "cashflow",
        "fina_indicator",
        "report_statements",
        "yahoo_fundamentals",
        "edgar_companyfacts",
        "xueqiu_income",
    ):
        assert dataset in saj.CORE_PROFILE_DATASETS
        assert saj.NONCORE_SHRUNK_CAPS[dataset] == saj.ANALYSIS_CAPS[dataset]
