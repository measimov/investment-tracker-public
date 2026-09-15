"""港股三张报表抽取管线（DB）：目标规划、缓存/版本/指纹、本期权威 vs 比较期只填空、
prompt bump 免重下载、确定性失败封顶、fatal 传递、与 Yahoo 合并口径。网络与 LLM 全部打桩。"""

import gzip
import json
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models.security_profile import SecurityProfileData
from app.services import report_statement_prompts as prompts
from app.services import report_statement_service as svc
from app.services import security_profile_service as profile_svc
from app.services.earnings_quality import market_statements, merge_hk_statement_rows
from app.services.report_statements import STATEMENT_EXTRACTOR_VERSION
from app.services.llm_client import LLMClientError, LLMNotConfiguredError
from app.services.report_statements import locate_statements

from .helpers import reset_tables

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "reports"
SYMBOL, MARKET = "00700", "港股"


def _pages(name: str):
    with gzip.open(FIXTURE_DIR / f"{name}.pages.txt.gz", "rt", encoding="utf-8") as handle:
        return handle.read().split("\x0c")


ANNUAL_PAGES = _pages("hk_00700_20251231")
INTERIM_PAGES = _pages("hk_00700_20260630_interim")


def _shift_years_back(pages):
    """2026 中报固件 → 合成 2025 中报（年份整体前移一年，先占位再替换避免连锁）。"""
    out = []
    for page in pages:
        text = (
            page.replace("二零二五年", "\x00PREV\x00").replace("二零二六年", "二零二五年")
            .replace("\x00PREV\x00", "二零二四年").replace("2025", "\x00P\x00")
            .replace("2026", "2025").replace("\x00P\x00", "2024")
        )
        out.append(text)
    return out


INTERIM_2025_PAGES = _shift_years_back(INTERIM_PAGES)
INTERIM_2025_TARGET = {
    "title": "2025 中期報告", "ann_date": "20/08/2025 17:00",
    "url": "https://www1.hkexnews.hk/a/2025h1.pdf",
}
ANNUAL_TARGET = {
    "title": "2025 年報", "ann_date": "09/04/2026 16:30",
    "url": "https://www1.hkexnews.hk/a/2025.pdf",
}
INTERIM_TARGET = {
    "title": "2026 中期報告", "ann_date": "20/08/2026 17:00",
    "url": "https://www1.hkexnews.hk/a/2026h1.pdf",
}


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        reset_tables(session, [SecurityProfileData])
        yield session
        session.rollback()
        reset_tables(session, [SecurityProfileData])
    finally:
        session.close()


def _mapping_for(pages, report_type):
    """用真实定位结果按标签生成映射 JSON（模拟一个只会照抄行 id 的模型）。"""
    located = {k: v for k, v in locate_statements(pages, report_type=report_type).items() if v}

    def ids(kind, *labels, note=None):
        out = []
        for row in located[kind].rows:
            if (row.label in labels) or (note is not None and row.label == "" and row.note == note):
                out.append(row.row_id)
        return out[:1] if len(labels) <= 1 and note is None else out

    return json.dumps({
        "income": {
            "total_revenue": ids("income", note="6"),
            "cost_of_revenue": ids("income", "收入成本"),
            "gross_profit": ids("income", "毛利"),
            "operating_income": ids("income", "經營盈利"),
            "n_income_attr_p": ids("income", "本公司權益持有人")[:1],
            "total_profit": ids("income", "除稅前盈利"),
            "sga_exp": ids("income", "銷售及市場推廣開支", "一般及行政開支"),
            "basic_eps": ids("income", "－基本")[:1],
        },
        "balance": {
            "total_assets": ids("balance", "資產總額"),
            "money_cap": ids("balance", "現金及現金等價物"),
            "fix_assets": ids("balance", "物業、設備及器材"),
        },
        "cashflow": {
            "n_cashflow_act": ids("cashflow", "經營活動所得現金流量淨額"),
            "capex": ids("cashflow", "購買物業、設備及器材、在建工程與投資物業的付款╱預付款項"),
        },
    })


def _patch_pipeline(monkeypatch, *, reports, pages_by_url, llm=None):
    """reports: {"annual": [...], "interim": [...]}；pages_by_url: url → 逐页文本。"""
    calls = {"llm": 0, "download": 0}

    def fake_reports(symbol, *, report_type="annual", limit=12):
        return list(reports.get(report_type, []))

    def fake_download(url, *, source="cninfo"):
        calls["download"] += 1
        assert source == "hkexnews"
        return url.encode()

    def fake_pages(pdf_bytes):
        return pages_by_url[pdf_bytes.decode()]

    def fake_llm(messages, **kwargs):
        calls["llm"] += 1
        if llm is not None:
            return llm(messages)
        payload = json.loads(messages[1]["content"].split("```json\n")[1].rsplit("\n```", 1)[0])
        report_type = payload["report"]["type"]
        if report_type == "annual":
            pages = ANNUAL_PAGES
        elif payload["report"]["period_end"] == "20250630":
            pages = INTERIM_2025_PAGES
        else:
            pages = INTERIM_PAGES
        return {"content": _mapping_for(pages, report_type), "model": "fake", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    monkeypatch.setattr(svc, "hkex_reports", fake_reports)
    monkeypatch.setattr(svc, "download_report_pdf", fake_download)
    monkeypatch.setattr(svc, "_extract_pages", fake_pages)
    monkeypatch.setattr(svc, "chat_completion", fake_llm)
    return calls


def _rows(db, dataset):
    rows = (
        db.query(SecurityProfileData)
        .filter_by(symbol=SYMBOL, market=MARKET, dataset=dataset)
        .order_by(SecurityProfileData.period_key.desc())
        .all()
    )
    return {row.period_key: row.payload for row in rows}


def test_plan_targets_annual_and_interim_with_dedupe_and_noise(monkeypatch):
    def fake_reports(symbol, *, report_type="annual", limit=12):
        if report_type == "annual":
            return [
                {"title": "2025 年報", "ann_date": "09/04/2026 16:30", "url": "u/2025"},
                {"title": "2025 年報（更正）", "ann_date": "10/04/2026 16:30", "url": "u/2025x"},
                {"title": "2025 年報", "ann_date": "30/03/2026 16:30", "url": "u/2025old"},  # 重刊：取公告日最新
                {"title": "2024 年報摘要", "ann_date": "01/04/2025 16:30", "url": "u/2024s"},
                {"title": "2024 年報", "ann_date": "01/04/2025 16:30", "url": "u/2024"},
            ]
        return [
            {"title": "2026 中期報告", "ann_date": "20/08/2026 17:00", "url": "u/2026h"},
            {"title": "2025 中期報告", "ann_date": "22/08/2025 17:00", "url": "u/2025h"},
            {"title": "2025 第一季度業績", "ann_date": "15/05/2025 17:00", "url": "u/q1"},
        ]

    monkeypatch.setattr(svc, "hkex_reports", fake_reports)
    planned = svc.plan_statement_targets(SYMBOL, MARKET)
    assert planned["complete"] is True and planned["failed_kinds"] == []
    assert [(t["period_key"], t["url"]) for t in planned["targets"]] == [
        ("20260630|interim", "u/2026h"),
        ("20251231|annual", "u/2025"),
        ("20250630|interim", "u/2025h"),
        ("20241231|annual", "u/2024"),
    ]
    assert svc.plan_statement_targets(SYMBOL, "A股") == {"targets": [], "complete": True, "failed_kinds": []}


def test_plan_targets_marks_incomplete_when_one_listing_fails(monkeypatch):
    def fake_reports(symbol, *, report_type="annual", limit=12):
        if report_type == "interim":
            raise RuntimeError("503")
        return [{"title": "2025 年報", "ann_date": "09/04/2026 16:30", "url": "u/2025"}]

    monkeypatch.setattr(svc, "hkex_reports", fake_reports)
    planned = svc.plan_statement_targets(SYMBOL, MARKET)
    assert planned["complete"] is False and planned["failed_kinds"] == ["interim"]
    assert [t["period_key"] for t in planned["targets"]] == ["20251231|annual"]


def test_infer_hk_interim_end():
    assert svc._infer_hk_interim_end("2026 中期報告", "20/08/2026 17:00") == "20260630"
    assert svc._infer_hk_interim_end("二零二五年中期報告", "28/11/2025 17:00") == "20250930"  # 3 月财年
    assert svc._infer_hk_interim_end("2025 中期報告", "bad") == "20250630"
    assert svc._infer_hk_interim_end("中期報告", "20/08/2026 17:00") is None


def test_ensure_extracts_annual_and_interim_and_keeps_primary_rows_authoritative(db, monkeypatch):
    calls = _patch_pipeline(
        monkeypatch,
        reports={"annual": [ANNUAL_TARGET], "interim": [INTERIM_TARGET]},
        pages_by_url={ANNUAL_TARGET["url"]: ANNUAL_PAGES, INTERIM_TARGET["url"]: INTERIM_PAGES},
    )
    result = svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=4)
    assert result["fatal"] is None and result["failed"] == 0
    assert (result["total"], result["generated"], result["completed"]) == (2, 2, 2)
    assert calls == {"llm": 2, "download": 2}

    rows = _rows(db, svc.STATEMENT_DATASET)
    assert set(rows) == {"20260630|H1", "20251231|FY", "20250630|H1", "20241231|FY"}
    fy2025 = rows["20251231|FY"]
    # 年报本期行是权威：中报资产负债表的比较列（上财年末）不得覆盖它
    assert fy2025["is_comparative"] is False and fy2025["source_period_key"] == "20251231|annual"
    assert fy2025["total_revenue"] == 751_766_000_000.0
    assert fy2025["cost_of_revenue"] == 329_173_000_000.0  # 费用类取绝对值（与 Yahoo 同符号）
    assert fy2025["sga_exp"] == 177_854_000_000.0
    assert fy2025["basic_eps"] == 24.749  # 每股指标不放大
    assert fy2025["capex"] == -87_482_000_000.0 and fy2025["free_cashflow"] == 215_570_000_000.0
    assert fy2025["currency"] == "CNY" and fy2025["fp"] == "FY"
    assert fy2025["source_pages"] == {"income": [130, 131], "balance": [132, 134], "cashflow": [139, 140]}
    # 年报比较期行
    assert rows["20241231|FY"]["is_comparative"] is True
    assert rows["20241231|FY"]["total_revenue"] == 660_257_000_000.0
    # 中报：六个月列
    h1 = rows["20260630|H1"]
    assert h1["is_comparative"] is False and h1["total_revenue"] == 401_243_000_000.0
    assert h1["total_assets"] > 0 and h1["n_cashflow_act"] == 154_061_000_000.0
    assert rows["20250630|H1"]["total_revenue"] == 364_526_000_000.0

    extracts = _rows(db, svc.EXTRACT_DATASET)
    assert set(extracts) == {"20251231|annual", "20260630|interim"}
    annual = extracts["20251231|annual"]
    assert annual["status"] == "ok" and annual["attempts"] == 1
    assert annual["extractor_version"] == svc.STATEMENT_EXTRACTOR_VERSION
    assert annual["prompt_version"] == svc.STATEMENT_PROMPT_VERSION
    assert set(annual["statements"]) == {"income", "balance", "cashflow"}
    assert sorted(annual["periods_written"]) == ["20241231|FY", "20251231|FY"]
    # 中报先处理（更新）：其资产负债表比较列先以比较期身份写下 20251231|FY，随后年报本期行接管
    assert sorted(extracts["20260630|interim"]["periods_written"]) == [
        "20250630|H1", "20251231|FY", "20260630|H1",
    ]

    # 第二轮：全部命中缓存，零下载零 LLM
    again = svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=4)
    assert (again["completed"], again["generated"], again["attempted"]) == (2, 0, 0)
    assert calls == {"llm": 2, "download": 2}

    progress = svc.statement_progress(db, SYMBOL, MARKET)
    assert progress["reports_ok"] == 2 and progress["reports_failed"] == 0
    assert progress["annual_periods"] == ["20251231", "20241231"]
    assert progress["interim_periods"] == ["20260630", "20250630"]


def test_budget_caps_reports_per_run_and_reports_pending(db, monkeypatch):
    _patch_pipeline(
        monkeypatch,
        reports={"annual": [ANNUAL_TARGET], "interim": [INTERIM_TARGET]},
        pages_by_url={ANNUAL_TARGET["url"]: ANNUAL_PAGES, INTERIM_TARGET["url"]: INTERIM_PAGES},
    )
    result = svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=1)
    assert result["generated"] == 1 and result["remaining"] == 1
    assert result["pending_periods"] == ["20251231"]  # 中报 20260630 更新，先处理
    assert result["gaps"][0].startswith("以下报告期的报表尚未抽取")


def test_prompt_bump_remaps_without_downloading(db, monkeypatch):
    calls = _patch_pipeline(
        monkeypatch, reports={"annual": [ANNUAL_TARGET]}, pages_by_url={ANNUAL_TARGET["url"]: ANNUAL_PAGES},
    )
    svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=4)
    assert calls == {"llm": 1, "download": 1}

    monkeypatch.setattr(svc, "STATEMENT_PROMPT_VERSION", svc.STATEMENT_PROMPT_VERSION + 1)
    monkeypatch.setattr(svc, "download_report_pdf", lambda *a, **k: pytest.fail("prompt bump 不得重下载"))
    result = svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=4)
    assert result["generated"] == 1 and calls["llm"] == 2
    extract = _rows(db, svc.EXTRACT_DATASET)["20251231|annual"]
    assert extract["prompt_version"] == svc.STATEMENT_PROMPT_VERSION and extract["status"] == "ok"


def test_extractor_bump_or_new_fingerprint_re_downloads(db, monkeypatch):
    calls = _patch_pipeline(
        monkeypatch, reports={"annual": [ANNUAL_TARGET]}, pages_by_url={ANNUAL_TARGET["url"]: ANNUAL_PAGES},
    )
    svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=4)
    monkeypatch.setattr(svc, "STATEMENT_EXTRACTOR_VERSION", svc.STATEMENT_EXTRACTOR_VERSION + 1)
    svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=4)
    assert calls == {"llm": 2, "download": 2}
    # 报告出了修订版（新 URL）：指纹变化 → 重抽
    revised = {**ANNUAL_TARGET, "url": "https://www1.hkexnews.hk/a/2025-rev.pdf", "ann_date": "20/04/2026 16:30"}
    _patch_pipeline(
        monkeypatch, reports={"annual": [revised]}, pages_by_url={revised["url"]: ANNUAL_PAGES},
    )
    monkeypatch.setattr(svc, "STATEMENT_EXTRACTOR_VERSION", svc.STATEMENT_EXTRACTOR_VERSION)
    result = svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=4)
    assert result["generated"] == 1
    assert _rows(db, svc.STATEMENT_DATASET)["20251231|FY"]["source_url"] == revised["url"]


def test_locate_failure_is_deterministic_and_caps_after_max_attempts(db, monkeypatch):
    calls = _patch_pipeline(
        monkeypatch, reports={"annual": [ANNUAL_TARGET]},
        pages_by_url={ANNUAL_TARGET["url"]: ["董事會報告\n沒有報表", "附註"]},
    )
    first = svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=4)
    assert first["failed"] == 1 and calls["llm"] == 0
    assert any("报表抽取失败" in gap for gap in first["gaps"])
    extract = _rows(db, svc.EXTRACT_DATASET)["20251231|annual"]
    assert extract["status"] == "failed" and extract["attempts"] == 1
    assert "未能定位报表" in extract["error"]

    svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=4)
    assert _rows(db, svc.EXTRACT_DATASET)["20251231|annual"]["attempts"] == 2

    third = svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=4)
    assert third["permanently_failed"] == 1 and third["attempted"] == 0
    assert any("已封顶" in gap for gap in third["gaps"])
    assert calls["download"] == 2


def test_transient_download_error_does_not_burn_attempts(db, monkeypatch):
    import requests

    _patch_pipeline(monkeypatch, reports={"annual": [ANNUAL_TARGET]}, pages_by_url={})
    monkeypatch.setattr(svc, "download_report_pdf", lambda *a, **k: (_ for _ in ()).throw(requests.Timeout("slow")))
    result = svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=4)
    assert result["failed"] == 1
    assert _rows(db, svc.EXTRACT_DATASET)["20251231|annual"]["attempts"] == 0


def test_llm_failures_map_to_fatal_kinds_and_keep_statements_for_retry(db, monkeypatch):
    def auth_failure(messages):
        raise LLMClientError("HTTP 401", status_code=401)

    calls = _patch_pipeline(
        monkeypatch, reports={"annual": [ANNUAL_TARGET], "interim": [INTERIM_TARGET]},
        pages_by_url={ANNUAL_TARGET["url"]: ANNUAL_PAGES, INTERIM_TARGET["url"]: INTERIM_PAGES},
        llm=auth_failure,
    )
    result = svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=4)
    assert result["fatal"]["kind"] == "llm_auth" and result["failed"] == 1
    assert calls["llm"] == 1  # fatal 后立即中止，不再逐份消耗

    def not_configured(messages):
        raise LLMNotConfiguredError("no key")

    _patch_pipeline(
        monkeypatch, reports={"annual": [ANNUAL_TARGET]}, pages_by_url={ANNUAL_TARGET["url"]: ANNUAL_PAGES},
        llm=not_configured,
    )
    result = svc.ensure_report_statements(db, "00005", MARKET, max_new=4)
    assert result["fatal"]["kind"] == "llm_not_configured"

    def bad_output(messages):
        return {"content": '{"income": {}}', "model": "fake", "usage": {}}

    _patch_pipeline(
        monkeypatch, reports={"annual": [ANNUAL_TARGET]}, pages_by_url={ANNUAL_TARGET["url"]: ANNUAL_PAGES},
        llm=bad_output,
    )
    result = svc.ensure_report_statements(db, "00941", MARKET, max_new=4)
    assert result["failed"] == 1 and result["fatal"] is None
    extract = _rows(db, svc.EXTRACT_DATASET)  # 00700 的行
    row = (
        db.query(SecurityProfileData)
        .filter_by(symbol="00941", market=MARKET, dataset=svc.EXTRACT_DATASET)
        .one()
    )
    assert row.payload["attempts"] == 1 and "缺少必需科目" in row.payload["error"]
    assert extract  # 之前的 00700 数据未受影响


def test_comparative_rows_fill_gaps_but_never_overwrite_primary(db, monkeypatch):
    """先处理 2025 年报（写 2025 本期 + 2024 比较期）；再处理 2024 年报，其本期行接管 2024；
    之后 2025 年报重映射（prompt bump）时，它的 2024 比较期行不得覆盖 2024 年报的本期行。"""
    calls = _patch_pipeline(
        monkeypatch, reports={"annual": [ANNUAL_TARGET]}, pages_by_url={ANNUAL_TARGET["url"]: ANNUAL_PAGES},
    )
    svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=4)
    fy2024 = _rows(db, svc.STATEMENT_DATASET)["20241231|FY"]
    assert fy2024["is_comparative"] is True and fy2024["source_period_key"] == "20251231|annual"

    # 2024 年报（固件复用 2025 页面：表头年份含 2024，本期列按年份定位到 2024 列）
    older = {"title": "2024 年報", "ann_date": "01/04/2025 16:30", "url": "https://www1.hkexnews.hk/a/2024.pdf"}
    calls = _patch_pipeline(
        monkeypatch, reports={"annual": [ANNUAL_TARGET, older]},
        pages_by_url={ANNUAL_TARGET["url"]: ANNUAL_PAGES, older["url"]: ANNUAL_PAGES},
    )
    result = svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=4)
    assert (result["generated"], result["failed"]) == (1, 0)
    fy2024 = _rows(db, svc.STATEMENT_DATASET)["20241231|FY"]
    assert fy2024["is_comparative"] is False and fy2024["source_period_key"] == "20241231|annual"
    assert fy2024["total_revenue"] == 660_257_000_000.0
    # 2025 本期行不受影响
    assert _rows(db, svc.STATEMENT_DATASET)["20251231|FY"]["source_period_key"] == "20251231|annual"

    # 2025 年报重映射：其 2024 比较期不得覆盖 2024 年报的本期行
    monkeypatch.setattr(svc, "STATEMENT_PROMPT_VERSION", svc.STATEMENT_PROMPT_VERSION + 1)
    result = svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=4)
    assert result["generated"] == 2
    fy2024 = _rows(db, svc.STATEMENT_DATASET)["20241231|FY"]
    assert fy2024["is_comparative"] is False and fy2024["source_period_key"] == "20241231|annual"
    extract_2025 = _rows(db, svc.EXTRACT_DATASET)["20251231|annual"]
    assert extract_2025["periods_written"] == ["20251231|FY"]  # 比较期被跳过
    assert calls["llm"] == 3  # 2024 年报 1 次 + prompt bump 重映射 2 份


def test_market_statements_prefers_pdf_rows_and_backfills_with_yahoo():
    datasets = {
        "report_statements": [
            {"end_date": "20251231", "fp": "FY", "currency": "CNY", "total_revenue": 751.0, "n_income_attr_p": 224.0,
             "total_assets": 2038.0, "n_cashflow_act": 303.0, "cost_of_revenue": 329.0},
            {"end_date": "20260630", "fp": "H1", "currency": "CNY", "total_revenue": 401.0},
        ],
        "yahoo_fundamentals": [
            {"end_date": "20251231", "fp": "FY", "currency": "CNY", "total_revenue": 999.0},  # 同期：PDF 优先
            {"end_date": "20241231", "fp": "FY", "currency": "CNY", "total_revenue": 660.0, "total_assets": 1780.0},
        ],
    }
    merged = merge_hk_statement_rows(datasets)
    assert [(r["end_date"], r["fp"], r["total_revenue"]) for r in merged] == [
        ("20260630", "H1", 401.0), ("20251231", "FY", 751.0), ("20241231", "FY", 660.0),
    ]
    statements = market_statements("港股", datasets)
    assert [r["end_date"] for r in statements["income"]] == ["20251231", "20241231"]  # 透视只取 FY
    assert statements["income"][0]["total_revenue"] == 751.0
    assert statements["fina_indicator"][0]["grossprofit_margin"] == pytest.approx((751 - 329) / 751 * 100)
    assert market_statements("港股", {}) == {"income": [], "balancesheet": [], "cashflow": [], "fina_indicator": []}


def test_profile_loads_job_dataset_and_graham_inputs_include_pdf_rows(db, monkeypatch):
    _patch_pipeline(
        monkeypatch, reports={"annual": [ANNUAL_TARGET]}, pages_by_url={ANNUAL_TARGET["url"]: ANNUAL_PAGES},
    )
    svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=4)
    profile = profile_svc.load_symbol_profile(db, SYMBOL, MARKET)
    assert [row["end_date"] for row in profile["datasets"]["report_statements"]] == ["20251231", "20241231"]
    assert profile["latest_periods"]["report_statements"] == "20251231|FY"
    inputs = profile_svc.load_graham_inputs(db, SYMBOL, MARKET)
    assert [r["end_date"] for r in inputs["statement_datasets"]["report_statements"]] == ["20251231", "20241231"]
    assert inputs["statement_datasets"]["yahoo_fundamentals"] == []


# ---------------------------------------------------------------------------
# PR #201 评审：比较期按表合并 / 科目级 Yahoo 补缺 / 读取过版本谓词
# ---------------------------------------------------------------------------


def test_older_interim_comparative_does_not_wipe_richer_comparative_from_newer_annual(db, monkeypatch):
    """2025 年报先为 20241231|FY 写下三张表的比较期；随后 2025 中报的资产负债表比较列也指向
    20241231|FY——不得整行替换成只有资产负债表的行（2024 年报缺失时营收/现金流永远回不来）。"""
    _patch_pipeline(
        monkeypatch, reports={"annual": [ANNUAL_TARGET], "interim": [INTERIM_2025_TARGET]},
        pages_by_url={ANNUAL_TARGET["url"]: ANNUAL_PAGES, INTERIM_2025_TARGET["url"]: INTERIM_2025_PAGES},
    )
    result = svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=4)
    assert (result["generated"], result["failed"]) == (2, 0)
    rows = _rows(db, svc.STATEMENT_DATASET)
    fy2024 = rows["20241231|FY"]
    assert fy2024["is_comparative"] is True
    # 年报（20251231）比中报（20250630）新：三张表全部保留年报的比较期数据
    assert fy2024["total_revenue"] == 660_257_000_000.0
    assert fy2024["n_cashflow_act"] == 258_521_000_000.0
    assert fy2024["total_assets"] == 1_780_995_000_000.0
    assert {kind: src["period_key"] for kind, src in fy2024["source_by_kind"].items()} == {
        "income": "20251231|annual", "balance": "20251231|annual", "cashflow": "20251231|annual",
    }
    assert fy2024["source_period_key"] == "20251231|annual"
    # 中报自己的本期行照常写入
    assert rows["20250630|H1"]["is_comparative"] is False
    assert rows["20250630|H1"]["total_revenue"] == 401_243_000_000.0
    assert sorted(_rows(db, svc.EXTRACT_DATASET)["20250630|interim"]["periods_written"]) == [
        "20240630|H1", "20241231|FY", "20250630|H1",
    ]


def test_comparative_merge_is_per_statement_and_prefers_newer_source():
    older_balance_only = {
        "end_date": "20241231", "fp": "FY", "currency": "CNY", "is_comparative": True,
        "source_period_key": "20250630|interim", "source_report_type": "interim",
        "source_end_date": "20250630", "source_url": "u/h1", "source_fingerprint": "f1",
        "source_pages": {"balance": [27, 29]},
        "source_by_kind": {"balance": {"period_key": "20250630|interim", "end_date": "20250630", "report_type": "interim"}},
        "total_assets": 1.0, "money_cap": 2.0, "inventories": 9.0,
        "extractor_version": STATEMENT_EXTRACTOR_VERSION, "prompt_version": prompts.STATEMENT_PROMPT_VERSION,
    }
    newer_annual = {
        "end_date": "20241231", "fp": "FY", "currency": "CNY", "is_comparative": True,
        "source_period_key": "20251231|annual", "source_report_type": "annual",
        "source_end_date": "20251231", "source_url": "u/ar", "source_fingerprint": "f2",
        "source_pages": {"income": [130, 131], "balance": [132, 134]},
        "source_by_kind": {
            "income": {"period_key": "20251231|annual", "end_date": "20251231", "report_type": "annual"},
            "balance": {"period_key": "20251231|annual", "end_date": "20251231", "report_type": "annual"},
        },
        "total_revenue": 100.0, "total_assets": 5.0, "money_cap": None,
        "extractor_version": STATEMENT_EXTRACTOR_VERSION, "prompt_version": prompts.STATEMENT_PROMPT_VERSION,
    }
    # 旧（中报、只有资产负债表）在库，新（年报）来：年报覆盖资产负债表科目，年报没给的科目保留
    merged = svc.merge_comparative_row(older_balance_only, newer_annual)
    assert merged["total_assets"] == 5.0 and merged["total_revenue"] == 100.0
    assert merged["money_cap"] == 2.0 and merged["inventories"] == 9.0  # 新来源缺的科目由旧的补
    assert merged["source_by_kind"]["balance"]["period_key"] == "20251231|annual"
    assert merged["source_period_key"] == "20251231|annual" and merged["is_comparative"] is True
    # 反向：新（年报）在库，旧（中报）来：只填年报没有的科目，其余不动
    merged = svc.merge_comparative_row(newer_annual, older_balance_only)
    assert merged["total_assets"] == 5.0 and merged["total_revenue"] == 100.0
    assert merged["money_cap"] == 2.0 and merged["inventories"] == 9.0
    assert merged["source_by_kind"]["balance"]["period_key"] == "20251231|annual"
    # 本期权威行永不被比较期覆盖；空表直接写入
    primary = {**newer_annual, "is_comparative": False}
    assert svc.merge_comparative_row(primary, older_balance_only) is None
    assert svc.merge_comparative_row(None, older_balance_only) is older_balance_only


def test_comparative_merge_requires_matching_known_currency():
    """评审 P1：币种不同或任一侧未知时不得逐科目补数——20 USD 会被贴成 20 CNY 且元数据指向
    另一份报告。只能整行取单一来源：来源更新者胜，否则原样保留。"""
    def row(currency, kinds, *, end_date, report_type, **fields):
        return {
            "end_date": "20241231", "fp": "FY", "currency": currency, "is_comparative": True,
            "source_period_key": f"{end_date}|{report_type}", "source_report_type": report_type,
            "source_end_date": end_date, "source_url": "u", "source_fingerprint": "f",
            "source_pages": {k: [1, 2] for k in kinds},
            "source_by_kind": {k: {"period_key": f"{end_date}|{report_type}", "end_date": end_date,
                                   "report_type": report_type} for k in kinds},
            "extractor_version": STATEMENT_EXTRACTOR_VERSION,
            "prompt_version": prompts.STATEMENT_PROMPT_VERSION,
            **fields,
        }

    newer_cny = row("CNY", ["income", "balance"], end_date="20251231", report_type="annual",
                    total_assets=1000.0, total_revenue=500.0)
    older_usd = row("USD", ["balance"], end_date="20250630", report_type="interim", money_cap=20.0)
    # 已知不同币种：旧来源不得补数（保留新来源原样 → 不写）
    assert svc.merge_comparative_row(newer_cny, older_usd) is None
    # 已知不同币种、来源更新者是 incoming：整行换成 incoming，绝不混合
    merged = svc.merge_comparative_row(older_usd, newer_cny)
    assert merged is newer_cny and "money_cap" not in merged
    # 一侧币种未知：同样不补数，只按来源新旧取单一来源
    unknown = row(None, ["balance"], end_date="20250630", report_type="interim", money_cap=20.0)
    assert svc.merge_comparative_row(newer_cny, unknown) is None
    assert svc.merge_comparative_row(unknown, newer_cny) is newer_cny
    # 同币种才逐科目合并
    older_cny = row("CNY", ["balance"], end_date="20250630", report_type="interim", money_cap=20.0)
    merged = svc.merge_comparative_row(newer_cny, older_cny)
    assert merged["money_cap"] == 20.0 and merged["total_assets"] == 1000.0 and merged["currency"] == "CNY"


def test_merge_hk_statement_rows_fills_missing_fields_from_yahoo_per_period():
    """PDF 只有资产负债表比较列时，Yahoo 同期的营收/利润/现金流必须保留（评审 P2）。"""
    datasets = {
        "report_statements": [
            {"end_date": "20251231", "fp": "FY", "currency": "CNY", "total_assets": 2038.0,
             "is_comparative": True, "source_period_key": "20260630|interim"},
        ],
        "yahoo_fundamentals": [
            {"end_date": "20251231", "fp": "FY", "currency": "CNY", "total_revenue": 751.0,
             "n_income_attr_p": 224.0, "n_cashflow_act": 303.0, "total_assets": 9999.0},
            {"end_date": "20241231", "fp": "FY", "currency": "CNY", "total_revenue": 660.0},
        ],
    }
    merged = {row["end_date"]: row for row in merge_hk_statement_rows(datasets)}
    assert merged["20251231"]["total_assets"] == 2038.0  # PDF 有值的科目优先
    assert merged["20251231"]["total_revenue"] == 751.0  # PDF 没有的科目由 Yahoo 补
    assert merged["20251231"]["n_cashflow_act"] == 303.0
    assert merged["20241231"]["total_revenue"] == 660.0
    statements = market_statements("港股", datasets)
    assert statements["income"][0]["total_revenue"] == 751.0
    assert statements["balancesheet"][0]["total_assets"] == 2038.0
    # 同期不同币种：不混（PDF 行原样，Yahoo 行丢弃）
    datasets["yahoo_fundamentals"][0]["currency"] = "HKD"
    merged = {row["end_date"]: row for row in merge_hk_statement_rows(datasets)}
    assert merged["20251231"].get("total_revenue") is None and merged["20251231"]["currency"] == "CNY"
    # PDF 币种未知：不借 Yahoo 金额，也不贴 Yahoo 的币种标签
    datasets["yahoo_fundamentals"][0]["currency"] = "CNY"
    datasets["report_statements"][0]["currency"] = None
    merged = {row["end_date"]: row for row in merge_hk_statement_rows(datasets)}
    assert merged["20251231"].get("total_revenue") is None and merged["20251231"]["currency"] is None
    # Yahoo 币种未知：同样不补
    datasets["report_statements"][0]["currency"] = "CNY"
    datasets["yahoo_fundamentals"][0]["currency"] = None
    merged = {row["end_date"]: row for row in merge_hk_statement_rows(datasets)}
    assert merged["20251231"].get("total_revenue") is None and merged["20251231"]["currency"] == "CNY"
    # PDF 没有该期：Yahoo 整行补入（不受币种约束）
    assert merged["20241231"]["total_revenue"] == 660.0


def test_stale_version_rows_are_invisible_until_recomputed(db, monkeypatch):
    """bump 版本后每轮只重算 max_new 份：未重算的旧行不得再当官方数据展示/送分析/屏蔽 Yahoo。"""
    _patch_pipeline(
        monkeypatch, reports={"annual": [ANNUAL_TARGET], "interim": [INTERIM_TARGET]},
        pages_by_url={ANNUAL_TARGET["url"]: ANNUAL_PAGES, INTERIM_TARGET["url"]: INTERIM_PAGES},
    )
    svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=4)
    assert len(svc.load_report_statement_rows(db, SYMBOL, MARKET)) == 4

    bumped = prompts.STATEMENT_PROMPT_VERSION + 1
    monkeypatch.setattr(prompts, "STATEMENT_PROMPT_VERSION", bumped)
    monkeypatch.setattr(svc, "STATEMENT_PROMPT_VERSION", bumped)
    # 读取全部为空：档案数据集、格雷厄姆输入、进度、直接读取
    assert svc.load_report_statement_rows(db, SYMBOL, MARKET) == []
    assert profile_svc.load_symbol_profile(db, SYMBOL, MARKET)["datasets"]["report_statements"] == []
    assert profile_svc.load_graham_inputs(db, SYMBOL, MARKET) is None  # 无 Yahoo 行 → 无任何有效报表
    progress = svc.statement_progress(db, SYMBOL, MARKET)
    assert progress["reports_ok"] == 0 and progress["annual_periods"] == []
    summaries = profile_svc.graham_summaries_for(db, [(SYMBOL, MARKET)])
    assert summaries.get((SYMBOL, MARKET)) is None

    # 只重算 1 份（更新的中报）：只有它写出的期别可见，年报的旧行仍不可见
    result = svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=1)
    assert result["generated"] == 1 and result["remaining"] == 1
    visible = {row["end_date"] + "|" + row["fp"] for row in svc.load_report_statement_rows(db, SYMBOL, MARKET)}
    assert visible == {"20260630|H1", "20250630|H1", "20251231|FY"}
    fy2025 = next(r for r in svc.load_report_statement_rows(db, SYMBOL, MARKET) if r["fp"] == "FY")
    # 20251231|FY 此时来自中报的比较列（年报的旧本期行被中报重算的比较期接管，因旧行版本过期不再权威）
    assert fy2025["prompt_version"] == bumped
    # 重算失败的报告：旧行继续不可见
    monkeypatch.setattr(svc, "chat_completion", lambda *a, **k: (_ for _ in ()).throw(ValueError("坏输出")))
    result = svc.ensure_report_statements(db, SYMBOL, MARKET, max_new=4)
    assert result["failed"] == 1
    assert {row["end_date"] + "|" + row["fp"] for row in svc.load_report_statement_rows(db, SYMBOL, MARKET)} == visible

