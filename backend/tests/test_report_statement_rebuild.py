"""港股报表构建层（PR-A）：零下载零 LLM 重建、EPS 单位（仙→元）、资产小计修复、夹层权益、
校验 v4（重列 / 雅虎口径差异）、十年窗口与计划份数、映射额度耗尽的失败语义。

金样 = `tests/fixtures/reports/statement_extracts/hk_*.extracts.json`：生产库里已存的抽取行
（结构化行 + 模型映射，抽取器 v8 / prompt v4）按需裁掉无关报表，数字全部来自真实年报/中报。
"""

import gzip
import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models.security_profile import SecurityProfileData
from app.services import report_statement_checks as checks
from app.services import report_statement_prompts as prompts
from app.services import report_statement_service as svc
from app.services.llm_client import LLMClientError
from app.services.report_statements import (
    STATEMENT_EXTRACTOR_VERSION,
    ParsedStatement,
    StatementRow,
    locate_statements,
)
from app.services.security_profile_service import upsert_profile_row

from .helpers import reset_tables

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "reports" / "statement_extracts"
MARKET = "港股"


def _fixture(symbol):
    return json.loads((FIXTURE_DIR / f"hk_{symbol}.extracts.json").read_text(encoding="utf-8"))


def _current(payload):
    """金样是 v8/v4 抽取行：按当前抽取器/prompt 版本落库（PR-B 升抽取器版本后仍可重建）。"""
    return {
        **payload,
        "extractor_version": STATEMENT_EXTRACTOR_VERSION,
        "prompt_version": prompts.STATEMENT_PROMPT_VERSION,
    }


def _seed(db, symbol, *, only=None):
    data = _fixture(symbol)
    for period_key, payload in data["extracts"].items():
        if only and period_key not in only:
            continue
        upsert_profile_row(db, symbol, MARKET, svc.EXTRACT_DATASET, period_key, _current(payload))
    for end_date, row in data["yahoo_fundamentals"].items():
        upsert_profile_row(db, symbol, MARKET, "yahoo_fundamentals", end_date, row)
    db.commit()
    return data


def _rows(db, symbol, dataset=svc.STATEMENT_DATASET):
    rows = db.query(SecurityProfileData).filter_by(symbol=symbol, market=MARKET, dataset=dataset).all()
    return {row.period_key: row.payload for row in rows}


def _located(symbol, period_key):
    payload = _fixture(symbol)["extracts"][period_key]
    located = {kind: ParsedStatement.from_payload(item) for kind, item in payload["statements"].items()}
    target = svc._extract_target(period_key, payload)
    return located, payload["mapping"], target


def _no_network(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("重建不得下载或调用 LLM")

    monkeypatch.setattr(svc, "chat_completion", boom)
    monkeypatch.setattr(svc, "download_report_pdf", boom)


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


# ---------------------------------------------------------------------------
# A2 EPS 单位（#223）
# ---------------------------------------------------------------------------


def test_per_share_divisor_reads_label_neighbor_heading_and_header():
    located, mapping, _ = _located("01023", "20220630|annual")
    # 01023「－就年內溢利╱（虧損）而言（港仙）」
    assert svc.per_share_divisor(located["income"], mapping["income"]["basic_eps"]) == 100
    located, mapping, _ = _located("02669", "20201231|annual")
    assert svc.per_share_divisor(located["income"], mapping["income"]["basic_eps"]) == 100  # 每股港仙
    located, mapping, _ = _located("02669", "20191231|annual")
    assert svc.per_share_divisor(located["income"], mapping["income"]["basic_eps"]) == 1  # 只写「基本及攤薄」

    def statement(rows, header=()):
        return ParsedStatement(
            kind="income", page_start=1, page_end=1, title="綜合損益表", header=list(header),
            unit_multiplier=1000, currency="HKD", years=[2025, 2024], column_count=2,
            interim_four_columns=False,
            rows=[StatementRow(row_id=f"r{i}", label=label, note="", values=[]) for i, label in rows],
        )

    # 每股盈利小标题在上一行（v9 以后单位行也会进 context）
    parsed = statement([(1, "每股盈利（港仙）"), (2, "基本"), (3, "攤薄")])
    assert svc.per_share_divisor(parsed, ["r2"]) == 100
    # 股息行的「港仙」不是 EPS 的单位
    parsed = statement([(1, "擬派末期股息每股5港仙"), (2, "基本")])
    assert svc.per_share_divisor(parsed, ["r2"]) == 1
    # 表头声明（「以每股人民幣分列示」）；「percent」里的 cent 不算
    assert svc.per_share_divisor(statement([(1, "基本")], header=["（以每股人民幣分列示）"]), ["r1"]) == 100
    assert svc.per_share_divisor(statement([(1, "基本")], header=["margin percent"]), ["r1"]) == 1


def _evidence(symbol, *, yahoo=None):
    data = _fixture(symbol)
    out = []
    for period_key in data["extracts"]:
        located, mapping, target = _located(symbol, period_key)
        item = svc.eps_unit_evidence(located, mapping, target, yahoo_eps=yahoo)
        if item:
            out.append(item)
    return out


def test_propagate_eps_units_label_chain_and_implied_shares():
    """02669：2020 起年报行名写「每股港仙」；2019 年报只写「基本及攤薄」，但本期 16.36 = 2020 年报
    比较列 16.36（报告链）；中报与年报没有同一会计期，按隐含股数（归母净利 / EPS）归入仙。"""
    units = svc.propagate_eps_units(_evidence("02669"))
    assert units["20201231|annual"] == {"divisor": 100, "basis": "label"}
    assert units["20251231|annual"]["basis"] == "label"  # 每股人民幣仙
    assert units["20191231|annual"] == {"divisor": 100, "basis": "chain"}
    assert units["20210630|interim"] == {"divisor": 100, "basis": "shares"}
    assert units["20220630|interim"]["divisor"] == 100
    # 没有任何仙证据的公司不猜
    plain = [dict(e, labelled=False) for e in _evidence("02669")]
    assert {u["divisor"] for u in svc.propagate_eps_units(plain).values()} == {1}


def test_propagate_eps_units_yahoo_seed_and_yuan_report_stays_yuan():
    """01579：单位行（以人民幣分列示）被 v8 解析层丢弃，行名只有「基本」——2024 年报 EPS 76.2 是
    雅虎 0.762 的 100 倍 → 仙；2025 年报改按元列示（0.88），隐含股数差 100 倍，保持元。"""
    yahoo = {k: {"currency": v["currency"], "basic_eps": v["basic_eps"]}
             for k, v in _fixture("01579")["yahoo_fundamentals"].items()}
    units = svc.propagate_eps_units(_evidence("01579", yahoo=yahoo))
    assert units["20241231|annual"] == {"divisor": 100, "basis": "yahoo"}
    assert units["20251231|annual"] == {"divisor": 1, "basis": None}
    assert {u["divisor"] for u in svc.propagate_eps_units(_evidence("01579")).values()} == {1}


def test_build_divides_eps_and_records_unit():
    located, mapping, target = _located("01023", "20220630|annual")
    rows = {f"{r['end_date']}|{r['fp']}": r for r in svc.build_period_rows(located, mapping, target, fingerprint="f")}
    assert rows["20220630|FY"]["basic_eps"] == pytest.approx(0.116)
    assert rows["20220630|FY"]["diluted_eps"] == pytest.approx(0.1156)
    assert rows["20210630|FY"]["basic_eps"] == pytest.approx(-0.1561)
    assert rows["20220630|FY"]["eps_unit"] == {"source_unit": "cents", "divisor": 100, "basis": "label"}
    # 显式给了单位（传播结果）以它为准
    rows = svc.build_period_rows(located, mapping, target, fingerprint="f", eps_unit={"divisor": 1, "basis": None})
    assert rows[0]["basic_eps"] == pytest.approx(11.6) and "eps_unit" not in rows[0]


def test_yahoo_eps_ratio_near_100_is_error_other_differences_info():
    row = {"currency": "CNY", "basic_eps": 76.2, "end_date": "20241231"}
    [eps] = [c for c in checks.cross_check_row(row, yahoo_row={"currency": "CNY", "basic_eps": 0.762})
             if c["id"] == "yahoo_basic_eps"]
    assert eps["severity"] == "error" and eps["status"] == "suspect" and eps["reason"] == "eps_unit_100x"
    assert "basic_eps" in checks.validate_period_row(
        row, extra_checks=[eps])["suspect_fields"]
    [eps] = [c for c in checks.cross_check_row({**row, "basic_eps": 0.80}, yahoo_row={"currency": "CNY", "basic_eps": 0.762})
             if c["id"] == "yahoo_basic_eps"]
    assert eps["severity"] == "info"


# ---------------------------------------------------------------------------
# build v2：EPS 附注号守卫 / 校验 v5：EPS 大差异
# ---------------------------------------------------------------------------


def _real_income(name, report_type="annual"):
    pages_path = Path(__file__).parent / "fixtures" / "reports" / f"{name}.pages.txt.gz"
    with gzip.open(pages_path, "rt", encoding="utf-8") as handle:
        pages = handle.read().split("\x0c")
    return locate_statements(pages, report_type=report_type)["income"]


def test_eps_note_guard_redirects_to_following_basic_row_on_real_parse():
    """00148 2017：生产映射把 basic_eps 指向「Earnings per share 每股盈利 13」（附注号）。抽取器
    v10 解析出其后的 HK$…港元 行后，构建层把映射改指基本/摊薄行，比较期同样取到值。"""
    income = _real_income("hk_00148_20171231")
    heading = next(r for r in income.rows if r.label == "Earnings per share 每股盈利")
    basic = next(r for r in income.rows if r.label.startswith("– Basic"))
    diluted = next(r for r in income.rows if r.label.startswith("– Diluted"))
    owners = next(r for r in income.rows if r.label.startswith("Owners of the Company"))
    mapping = {"income": {"n_income_attr_p": [owners.row_id], "basic_eps": [heading.row_id],
                          "diluted_eps": [heading.row_id]}}
    fixed, repairs = svc.effective_mapping({"income": income}, mapping)
    assert fixed["income"]["basic_eps"] == [basic.row_id]
    assert fixed["income"]["diluted_eps"] == [diluted.row_id]
    assert mapping["income"]["basic_eps"] == [heading.row_id]  # 存储的 LLM 映射不被改写
    target = {"period_key": "20171231|annual", "report_type": "annual", "end_date": "20171231",
              "url": "u", "ann_date": "01/04/2018 16:30", "title": "2017 年報"}
    rows = {f"{r['end_date']}|{r['fp']}": r
            for r in svc.build_period_rows({"income": income}, mapping, target, fingerprint="f")}
    assert rows["20171231|FY"]["basic_eps"] == pytest.approx(5.363)
    assert rows["20171231|FY"]["diluted_eps"] == pytest.approx(5.314)
    assert rows["20161231|FY"]["basic_eps"] == pytest.approx(4.889)
    record = rows["20171231|FY"]["repaired_fields"]["basic_eps"]
    assert record == {"reason": "eps_note_number", "from_row": heading.row_id, "to_row": basic.row_id,
                      "note_number": "13", "to_value": pytest.approx(5.363)}
    # 正确的映射不动
    assert svc.effective_mapping({"income": income}, {"income": {"basic_eps": [basic.row_id]}})[1] == {}


def test_eps_note_guard_drops_when_no_basic_row_follows(db, monkeypatch):
    """03900 2017/2018 年报的已存抽取行（v9 解析：「人民幣0.77元」行整行丢失，小标题「每股盈利 13/14」
    是损益表最后一行）：零下载零 LLM 重建后 basic/diluted_eps 丢弃交给雅虎，不再是 13.0/14.0。"""
    _no_network(monkeypatch)
    data = _seed(db, "03900")
    assert data["extracts"]["20181231|annual"]["mapping"]["income"]["basic_eps"] == ["r33"]
    result = svc.rebuild_report_statements(db, "03900", MARKET)
    assert result["rebuilt"] == 2 and result["failed"] == 0
    rows = _rows(db, "03900")
    for period, note in (("20181231|FY", "14"), ("20171231|FY", "13")):
        row = rows[period]
        assert row.get("basic_eps") is None, period
        assert row["repaired_fields"]["basic_eps"] == {
            "reason": "eps_note_number", "from_row": "r33", "to_row": None, "note_number": note,
            "to_value": None,
        }
        assert row["n_income_attr_p"] is not None  # 其他科目照常
    # 2017 年报的 diluted_eps 也被映射到同一行
    assert rows["20171231|FY"].get("diluted_eps") is None
    assert rows["20171231|FY"]["repaired_fields"]["diluted_eps"]["to_row"] is None
    assert rows["20181231|FY"].get("diluted_eps") is None


def test_eps_note_guard_ignores_real_multi_column_rows():
    """「－就期內溢利╱（虧損）而言（港仙）」（01023，无「基本」字样但两列都有值）与带小数的单值行
    都不是附注号；页脚公司名行（「建滔集團有限公司 2」）没有每股盈利字样也不动。"""
    def statement(rows):
        return ParsedStatement(
            kind="income", page_start=1, page_end=1, title="綜合損益表", header=[],
            unit_multiplier=1000, currency="HKD", years=[2025, 2024], column_count=2,
            interim_four_columns=False,
            rows=[StatementRow(row_id=f"r{i}", label=label, note="", values=[Decimal(v) for v in values],
                               context=list(ctx)) for i, (label, values, ctx) in enumerate(rows, start=1)],
        )

    parsed = statement([
        ("應佔每股盈利╱（虧損）", ["8"], []),
        ("－就期內溢利╱（虧損）而言（港仙）", ["1.45", "-6.96"], ["應佔每股盈利╱（虧損）"]),
        ("時代集團控股有限公司", ["28"], []),
        ("每股股息", ["0.5"], []),
    ])
    for row_id in ("r2", "r3", "r4"):
        assert svc.repair_eps_note_mapping(parsed, {"basic_eps": [row_id]})[1] == {}, row_id
    # 附注号小标题其后没有「基本」字样的行 → 丢弃（不猜「－就期內溢利」是基本还是摊薄）
    fixed, repairs = svc.repair_eps_note_mapping(parsed, {"basic_eps": ["r1"]})
    assert "basic_eps" not in fixed and repairs["basic_eps"]["to_row"] is None


def test_yahoo_eps_large_mismatch_is_error_unless_later_report_agrees():
    # 00148 2024：附注号 13 当成 EPS，雅虎 1.471 → error，分析路径清洗后由雅虎补缺
    row = {"currency": "HKD", "basic_eps": 13.0, "end_date": "20241231", "source_end_date": "20241231"}
    yahoo = {"currency": "HKD", "basic_eps": 1.471, "diluted_eps": 1.471}
    found = {c["id"]: c for c in checks.cross_check_row(row, yahoo_row=yahoo)}
    assert found["yahoo_basic_eps"]["severity"] == "error"
    assert found["yahoo_basic_eps"]["reason"] == "eps_mismatch"
    validation = checks.validate_period_row(row, extra_checks=list(found.values()))
    assert validation["suspect_fields"] == ["basic_eps"]
    assert checks.scrub_suspect_fields({**row, "validation": validation})["basic_eps"] is None
    # 06049 2021：雅虎 2021 行的 EPS 2.01 其实是 2022 年的数；2022 年报比较列 1.53 与我们一致 → info
    row = {"currency": "CNY", "basic_eps": 1.53, "diluted_eps": 1.53, "end_date": "20211231",
           "source_end_date": "20211231"}
    yahoo = {"currency": "CNY", "basic_eps": 2.01, "diluted_eps": 2.01}
    later = {"source_period_key": "20221231|annual", "source_end_date": "20221231", "currency": "CNY",
             "basic_eps": 1.53, "diluted_eps": 1.53}
    found = {c["id"]: c for c in checks.cross_check_row(row, yahoo_row=yahoo, comparative_row=later)}
    assert found["yahoo_basic_eps"]["severity"] == "info"
    assert found["yahoo_basic_eps"]["reason"] == "yahoo_definition_diff"
    assert found["yahoo_diluted_eps"]["reason"] == "yahoo_definition_diff"
    assert checks.validate_period_row(row, extra_checks=list(found.values()))["suspect_fields"] == []
    # 没有更晚报告可对照时同样的差异判 error（宁可交给雅虎，也不让附注号混进去）
    found = {c["id"]: c for c in checks.cross_check_row(row, yahoo_row=yahoo)}
    assert found["yahoo_basic_eps"]["severity"] == "error"
    # 20% 以内仍只记 info（雅虎摊薄/加权股数口径差异）
    found = {c["id"]: c for c in checks.cross_check_row({**row, "basic_eps": 1.80}, yahoo_row=yahoo)}
    assert found["yahoo_basic_eps"]["severity"] == "info"
    # 比较列证据里存下每股盈利，revalidate 重放时同样可解释
    evidence = svc.comparative_evidence({**later, "total_revenue": 1.0})
    assert evidence["basic_eps"] == 1.53 and evidence["diluted_eps"] == 1.53


# ---------------------------------------------------------------------------
# A3 资产小计修复 / A4 夹层权益
# ---------------------------------------------------------------------------


def _period(rows, key):
    """按 end_date|fp 取期间行：v9 读出列期末日后，一份年报可能同时写本期与比较期两行。"""
    [row] = [r for r in rows if f"{r['end_date']}|{r['fp']}" == key]
    return row


def test_repair_picks_current_assets_subtotal_including_held_for_sale_00148():
    """00148 2017：映射取了无标签 r21（43,477,598，不含「分類為待售資產」）；r23 = r21 + r22
    = 45,173,791 才让 资产 = 负债 + 权益 在 0.1% 内闭合（88,294,383）。"""
    located, mapping, target = _located("00148", "20171231|annual")
    row = _period(svc.build_period_rows(located, mapping, target, fingerprint="f"), "20171231|FY")
    assert row["total_cur_assets"] == 45_173_791_000.0
    assert row["total_assets"] == 88_294_383_000.0
    assert row["repaired_fields"]["total_cur_assets"]["from_row"] == "r21"
    assert row["repaired_fields"]["total_cur_assets"]["to_row"] == "r23"
    assert row["derived_fields"]["total_assets"] == ["total_nca", "total_cur_assets"]
    assert row["validation"]["status"] == "ok"


def test_repair_retargets_unlabeled_row_mapped_as_total_assets_01995():
    """01995 2025H1：无标签 r18（6,076,502）是流动资产小计，被映射成总资产——改为流动资产，
    总资产 = 非流动 3,236,587 + 6,076,502 = 9,313,089（= 负债 + 权益）。"""
    located, mapping, target = _located("01995", "20250630|interim")
    rows = {f"{r['end_date']}|{r['fp']}": r for r in svc.build_period_rows(located, mapping, target, fingerprint="f")}
    h1 = rows["20250630|H1"]
    assert h1["total_cur_assets"] == 6_076_502_000.0 and h1["total_assets"] == 9_313_089_000.0
    assert h1["repaired_fields"]["total_cur_assets"]["to_row"] == "r18"
    assert h1["repaired_fields"]["total_assets"]["from_row"] == "r18"
    assert h1["validation"]["status"] == "ok"


def test_repair_skips_when_candidates_are_ambiguous():
    located, mapping, target = _located("00148", "20171231|annual")
    balance = located["balance"]
    # 再造一条数值不同、也能让恒等式在 0.1% 内闭合的无标签行：两个候选 → 不修，仍存疑
    balance.rows.append(StatementRow(row_id="r99", label="", note="", values=[Decimal("45180000"), None]))
    row = _period(svc.build_period_rows(located, mapping, target, fingerprint="f"), "20171231|FY")
    assert "repaired_fields" not in row
    assert row["total_cur_assets"] == 43_477_598_000.0
    assert row["validation"]["status"] == "suspect"


def test_mezzanine_equity_closes_us_gaap_balance_sheet_09618():
    """09618 2020：资产 422,287,794 − 负债 200,668,678 − 权益 204,485,908 = 17,133,208 = 可轉換
    可贖回非控制性權益；合计行「負債、夾層權益及權益總額」不是它。"""
    located, mapping, target = _located("09618", "20201231|annual")
    assert svc.mezzanine_row_ids(located["balance"]) == ["r45"]
    rows = {r["end_date"]: r for r in svc.build_period_rows(located, mapping, target, fingerprint="f")}
    assert rows["20201231"]["mezzanine_equity"] == 17_133_208_000.0
    assert rows["20191231"]["mezzanine_equity"] == 15_964_384_000.0
    for row in rows.values():
        identity = next(c for c in row["validation"]["checks"] if c["id"] == "balance_sheet_identity")
        assert identity["status"] == "ok" and "mezzanine_equity" in identity["fields"]
        assert row["validation"]["status"] == "ok"
    assert "mezzanine_equity" in checks.NUMERIC_FIELDS and prompts.FIELD_KIND["mezzanine_equity"] == "balance"


# ---------------------------------------------------------------------------
# A5 校验 v4：重列与来源优先级
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line,expected", [
    ("（經重列）", True), ("经重列", True), ("（重列）", True), ("（重新表述）", True),
    ("（經重述）", True), ("（已重述）", True), ("(Restated)", True), ("（未經審核） 及經重列）", True),
    ("二零二二年 二零二一年", False), ("經重列項目 1,234,567 2,345,678", False),
])
def test_header_restated_vocabulary(line, expected):
    assert checks.header_restated(["綜合損益表", line]) is expected


def _primary(end, **fields):
    return {"end_date": end, "fp": "FY", "currency": "HKD", "source_end_date": end, **fields}


def _evidence_row(source_end, *, restated=None, **fields):
    evidence = {"source_period_key": f"{source_end}|annual", "source_end_date": source_end,
                "source_report_type": "annual", "currency": "HKD", **fields}
    if restated:
        evidence["restated_by_kind"] = restated
    return evidence


def test_comparative_difference_from_restated_later_report_is_info_and_keeps_value():
    row = _primary("20210630", total_revenue=1_424_879_000.0)
    later = _evidence_row("20220630", restated={"income": True}, total_revenue=1_314_416_000.0)
    [check] = [c for c in checks.cross_check_row(row, comparative_row=later) if c["id"] == "comparative_total_revenue"]
    assert check["severity"] == "info" and check["reason"] == "comparative_restated"
    validation = checks.validate_period_row(row, extra_checks=[check])
    assert validation["status"] == "ok"
    assert checks.restated_fields({"validation": validation}) == ["total_revenue"]
    # 标记在另一张表上不算；没有标记的 >5% 差异仍判存疑（02669 以外的未解释差异）
    for evidence in (_evidence_row("20220630", restated={"cashflow": True}, total_revenue=1_314_416_000.0),
                     _evidence_row("20220630", total_revenue=1_314_416_000.0)):
        [check] = [c for c in checks.cross_check_row(row, comparative_row=evidence)
                   if c["id"] == "comparative_total_revenue"]
        assert check["severity"] == "error" and "reason" not in check
        assert checks.validate_period_row(row, extra_checks=[check])["status"] == "suspect"


def test_yahoo_difference_explained_by_later_report_is_info():
    # 00883 2023：我们 416,609 = 2024 年报比较列；雅虎 421,530 口径不同
    row = {**_primary("20231231", total_revenue=416_609e6), "currency": "CNY"}
    later = {**_evidence_row("20241231", total_revenue=416_609e6), "currency": "CNY"}
    yahoo = {"currency": "CNY", "total_revenue": 421_530e6}
    found = {c["id"]: c for c in checks.cross_check_row(row, yahoo_row=yahoo, comparative_row=later)}
    assert found["yahoo_total_revenue"]["reason"] == "yahoo_definition_diff"
    assert found["yahoo_total_revenue"]["severity"] == "info"
    # 02669 2024 CFO：2025 年报「經重列」比较列 1,338,145 = 雅虎；我们 1,313,056 保留原值
    row = {**_primary("20241231", n_cashflow_act=1_313_056e3), "currency": "CNY"}
    later = {**_evidence_row("20251231", restated={"cashflow": True}, n_cashflow_act=1_338_145e3), "currency": "CNY"}
    yahoo = {"currency": "CNY", "n_cashflow_act": 1_338_145e3}
    found = {c["id"]: c for c in checks.cross_check_row(row, yahoo_row=yahoo, comparative_row=later)}
    assert found["yahoo_n_cashflow_act"]["reason"] == "yahoo_restated"
    assert found["comparative_n_cashflow_act"]["reason"] == "comparative_restated"
    validation = checks.validate_period_row(row, extra_checks=list(found.values()))
    assert validation["status"] == "ok"
    # 没有更晚报告的证据：雅虎差异仍是 error
    found = {c["id"]: c for c in checks.cross_check_row(row, yahoo_row=yahoo)}
    assert found["yahoo_n_cashflow_act"]["severity"] == "error"


# ---------------------------------------------------------------------------
# A1 重建通道（零下载零 LLM）
# ---------------------------------------------------------------------------


def test_rebuild_is_zero_llm_writes_build_version_and_hides_old_build_rows(db, monkeypatch):
    _no_network(monkeypatch)
    _seed(db, "01023")
    # 旧构建写出的行（无 build_version）：对读者不可见
    upsert_profile_row(db, "01023", MARKET, svc.STATEMENT_DATASET, "20220630|FY", {
        "end_date": "20220630", "fp": "FY", "currency": "HKD", "basic_eps": 11.6,
        "extractor_version": STATEMENT_EXTRACTOR_VERSION, "prompt_version": prompts.STATEMENT_PROMPT_VERSION,
    })
    db.commit()
    assert svc.load_report_statement_rows(db, "01023", MARKET) == []

    outcome = svc.rebuild_report_statements(db, "01023", MARKET)
    assert outcome["rebuilt"] == 4 and outcome["failed"] == 0 and outcome["eps_cents_reports"] == 4
    rows = {f"{r['end_date']}|{r['fp']}": r for r in svc.load_report_statement_rows(db, "01023", MARKET)}
    assert rows["20220630|FY"]["basic_eps"] == pytest.approx(0.116)
    assert rows["20220630|FY"]["build_version"] == prompts.STATEMENT_BUILD_VERSION
    # 2021 FY 本期行：2022 年报比较列「經重列」收入 1,314,416 vs 我们 1,424,879 → 已重列（info）保留原值
    fy2021 = rows["20210630|FY"]
    assert fy2021["is_comparative"] is False and fy2021["total_revenue"] == 1_424_879_000.0
    assert fy2021["validation"]["status"] == "ok"
    assert checks.restated_fields(fy2021) == ["total_revenue"]
    assert fy2021["comparative_evidence"]["restated_by_kind"] == {"income": True, "cashflow": True}
    # 2020 H1 同理（2021 中报「及經重列」）
    assert rows["20201231|H1"]["validation"]["status"] == "ok"
    assert checks.restated_fields(rows["20201231|H1"]) == ["total_revenue"]
    extracts = _rows(db, "01023", svc.EXTRACT_DATASET)
    assert all(p["build_version"] == prompts.STATEMENT_BUILD_VERSION and p["eps_divisor"] == 100
               for p in extracts.values())
    # 构建版本已是当前：再跑不动
    assert svc.rebuild_report_statements(db, "01023", MARKET)["rebuilt"] == 0


def test_rebuild_order_and_yahoo_cross_checks_on_real_extracts(db, monkeypatch):
    _no_network(monkeypatch)
    _seed(db, "02669")
    outcome = svc.rebuild_report_statements(db, "02669", MARKET)
    assert outcome["rebuilt"] == 6 and outcome["eps_cents_reports"] == 6
    rows = {f"{r['end_date']}|{r['fp']}": r for r in svc.load_report_statement_rows(db, "02669", MARKET)}
    fy2024 = rows["20241231|FY"]
    assert fy2024["n_cashflow_act"] == 1_313_056_000.0  # 首次披露值保留
    reasons = {c["id"]: c.get("reason") for c in fy2024["validation"]["checks"]}
    assert reasons["yahoo_n_cashflow_act"] == "yahoo_restated"
    assert reasons["comparative_n_cashflow_act"] == "comparative_restated"
    assert fy2024["validation"]["status"] == "ok"
    assert fy2024["basic_eps"] == pytest.approx(0.46)
    assert rows["20191231|FY"]["eps_unit"]["basis"] == "chain"
    assert rows["20210630|H1"]["basic_eps"] == pytest.approx(0.1196)

    _seed(db, "00883")
    svc.rebuild_report_statements(db, "00883", MARKET)
    rows = {f"{r['end_date']}|{r['fp']}": r for r in svc.load_report_statement_rows(db, "00883", MARKET)}
    for end in ("20221231|FY", "20231231|FY"):
        check = next(c for c in rows[end]["validation"]["checks"] if c["id"] == "yahoo_total_revenue")
        assert check["reason"] == "yahoo_definition_diff" and rows[end]["validation"]["status"] == "ok"


def test_rebuild_repairs_and_mezzanine_on_real_extracts(db, monkeypatch):
    _no_network(monkeypatch)
    for symbol in ("00148", "01995", "09618"):
        _seed(db, symbol)
        outcome = svc.rebuild_report_statements(db, symbol, MARKET)
        assert outcome["failed"] == 0
        assert all(r["validation"]["status"] == "ok" for r in svc.load_report_statement_rows(db, symbol, MARKET))
    assert svc.rebuild_report_statements(db, "01995", MARKET, force=True)["repaired_periods"] == [
        "20250630|H1", "20241231|FY",
    ]


def test_rebuild_follows_new_eps_evidence_from_a_later_report(db, monkeypatch):
    """先只有 2019 年报（未标注 → 元），后来 2020 年报（标注港仙）入库：2019 的单位随证据变化
    而重建（抽取行 eps_divisor 与传播结果不一致即重建）。"""
    _no_network(monkeypatch)
    _seed(db, "02669", only={"20191231|annual"})
    svc.rebuild_report_statements(db, "02669", MARKET)
    assert _rows(db, "02669")["20191231|FY"]["basic_eps"] == pytest.approx(16.36)
    _seed(db, "02669", only={"20201231|annual"})
    outcome = svc.rebuild_report_statements(db, "02669", MARKET)
    assert outcome["rebuilt"] == 2
    assert _rows(db, "02669")["20191231|FY"]["basic_eps"] == pytest.approx(0.1636)


def test_rebuild_dry_run_leaves_database_untouched(db, monkeypatch):
    _no_network(monkeypatch)
    _seed(db, "00148")
    svc.rebuild_report_statements(db, "00148", MARKET, commit=False)
    assert _rows(db, "00148")["20171231|FY"]["total_cur_assets"] == 45_173_791_000.0  # 事务内可见
    db.rollback()
    assert _rows(db, "00148") == {}
    assert "build_version" not in _rows(db, "00148", svc.EXTRACT_DATASET)["20171231|annual"]


def test_ensure_rebuilds_stale_build_without_spending_budget(db, monkeypatch):
    _no_network(monkeypatch)
    data = _seed(db, "00148")
    payload = data["extracts"]["20171231|annual"]
    target = {"title": payload["title"], "ann_date": payload["ann_date"], "url": payload["source_url"]}
    monkeypatch.setattr(
        svc, "hkex_reports",
        lambda symbol, *, report_type="annual", limit=12: [target] if report_type == "annual" else [],
    )
    result = svc.ensure_report_statements(db, "00148", MARKET, max_new=0)
    assert result["rebuilt"] == 1 and result["completed"] == 1 and result["attempted"] == 0
    assert result["suspect"] == 0
    progress = svc.statement_progress(db, "00148", MARKET)
    assert progress["planned_annual"] == 1 and progress["planned_interim"] == 0
    assert progress["reports_ok"] == 1 and progress["reports_stale"] == 0


def test_rebuild_script_diff_lists_changes(db, monkeypatch):
    _no_network(monkeypatch)
    spec = importlib.util.spec_from_file_location(
        "rebuild_script", Path(__file__).parent.parent / "scripts" / "rebuild_report_statements.py"
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    _seed(db, "00148")
    before = {"20171231|FY": {"total_cur_assets": 43_477_598_000.0, "validation": {"status": "suspect"}}}
    svc.rebuild_report_statements(db, "00148", MARKET)
    lines = script.diff_statement_rows(before, script._snapshot(db, "00148"))
    # 本期行：存疑转 ok、小计 r21→r23。抽取器 v9 按列期末日读出 00148 的比较列后，同一份年报
    # 还会多写一条 2016 比较期行（v8 表头年份是 [2017, 2017]，没有比较列）
    [current] = [line for line in lines if "20171231|FY" in line]
    assert "suspect → ok" in current and "r21→r23" in current
    assert all("20161231|FY" in line for line in lines if line is not current)


# ---------------------------------------------------------------------------
# A6 映射额度 / A7 十年窗口 / A8 计划份数
# ---------------------------------------------------------------------------


def test_output_exhausted_mapping_is_deterministic_failure(db, monkeypatch):
    seen = {}

    def exhausted(messages, **kwargs):
        seen.update(kwargs)
        raise LLMClientError("LLM 输出为空（finish_reason=length）", finish_reason="length")

    data = _fixture("00148")
    payload = data["extracts"]["20171231|annual"]
    upsert_profile_row(db, "00148", MARKET, svc.EXTRACT_DATASET, "20171231|annual", {
        **_current(payload), "status": "failed", "attempts": 0, "mapping": None,
        "source_fingerprint": f"{payload['source_url']}|{payload['ann_date']}",
    })
    db.commit()
    target = {"title": payload["title"], "ann_date": payload["ann_date"], "url": payload["source_url"]}
    monkeypatch.setattr(svc, "hkex_reports", lambda symbol, *, report_type="annual", limit=12: [target] if report_type == "annual" else [])
    monkeypatch.setattr(svc, "chat_completion", exhausted)
    monkeypatch.setattr(svc, "download_report_pdf", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应下载")))
    result = svc.ensure_report_statements(db, "00148", MARKET, max_new=4)
    assert result["failed"] == 1
    assert seen["max_tokens"] == svc.settings.statement_max_output_tokens == 32768
    assert _rows(db, "00148", svc.EXTRACT_DATASET)["20171231|annual"]["attempts"] == 1  # 计 attempts
    # 5xx 仍是瞬时（不计 attempts）
    monkeypatch.setattr(svc, "chat_completion", lambda *a, **k: (_ for _ in ()).throw(LLMClientError("503", status_code=503)))
    svc.ensure_report_statements(db, "00148", MARKET, max_new=4)
    assert _rows(db, "00148", svc.EXTRACT_DATASET)["20171231|annual"]["attempts"] == 1


def test_window_follows_plan_membership_not_calendar_years():
    """窗口 = 计划的实际成员（PR #230 评审 P2）：报告缺年时计划延伸到更早年份，仍在计划内。"""
    # 年报缺 2016：计划最近 10 份 = 2025…2017 + 2015
    annual = [f"{y}1231|annual" for y in range(2025, 2016, -1)] + ["20151231|annual"]
    plan = {"period_keys": annual + ["20260630|interim", "20250630|interim"]}
    assert not svc.outside_statement_window("20151231|annual", plan)  # 自然年截断会误判
    assert svc.outside_statement_window("20141231|annual", plan)  # 早于全部计划年报
    assert svc.outside_statement_window("20240630|interim", plan)  # 早于全部计划中报
    assert not svc.outside_statement_window("20270630|interim", plan)  # 不在计划但更新：保守
    # 没有完整计划：保守，一律不算窗口外
    assert not svc.outside_statement_window("20051231|annual", None)
    assert not svc.outside_statement_window("20051231|annual", {"period_keys": []})


def test_progress_out_of_window_uses_stored_plan(db):
    keys = ["20251231|annual", "20161231|annual", "20151231|annual", "20260630|interim", "20160630|interim"]
    for key in keys:
        upsert_profile_row(db, "01579", MARKET, svc.EXTRACT_DATASET, key, {
            "status": "ok", "end_date": key[:8], "report_type": key.split("|")[1],
            "extractor_version": 1, "prompt_version": 1,
        })
    db.commit()
    # 从未完整规划过：保守，全部计入待重抽；也不做「没有中报」的结论
    progress = svc.statement_progress(db, "01579", MARKET)
    assert progress["reports_out_of_window"] == 0 and progress["reports_stale"] == 5
    assert progress["planned_interim"] is None

    upsert_profile_row(db, "01579", MARKET, svc.PLAN_DATASET, svc.PLAN_PERIOD_KEY, {
        "planned_annual": 2, "planned_interim": 1,
        "period_keys": ["20251231|annual", "20161231|annual", "20260630|interim"],
    })
    db.commit()
    progress = svc.statement_progress(db, "01579", MARKET)
    assert progress["reports_out_of_window"] == 2 and progress["reports_stale"] == 3


def test_rerun_script_keeps_gap_year_report_in_plan(db):
    """缺年时仍在计划内的旧报告（2015）版本过期 → 重跑脚本必须把它列为待重跑，不能整标的跳过。"""
    spec = importlib.util.spec_from_file_location(
        "rerun_script", Path(__file__).parent.parent / "scripts" / "rerun_report_statements.py"
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    annual = [f"{y}1231|annual" for y in range(2025, 2016, -1)] + ["20151231|annual"]
    for key in annual:
        stale = key == "20151231|annual"
        upsert_profile_row(db, "02669", MARKET, svc.EXTRACT_DATASET, key, {
            "status": "ok", "end_date": key[:8], "report_type": "annual",
            "extractor_version": 1 if stale else svc.STATEMENT_EXTRACTOR_VERSION,
            "prompt_version": 1 if stale else svc.STATEMENT_PROMPT_VERSION,
        })
    upsert_profile_row(db, "02669", MARKET, svc.PLAN_DATASET, svc.PLAN_PERIOD_KEY, {
        "planned_annual": 10, "planned_interim": 0, "period_keys": annual,
    })
    db.commit()
    assert [row.period_key for row in script._stale_rows(db, "02669")] == ["20151231|annual"]


def test_comparative_merge_carries_per_statement_build_metadata():
    """比较期按表合并：重列标记 / EPS 单位（损益表）/ 小计修复（资产负债表）随该表的来源取舍。"""
    def row(end, kinds, **fields):
        return {
            "end_date": "20241231", "fp": "FY", "currency": "HKD", "is_comparative": True,
            "source_period_key": f"{end}|annual", "source_report_type": "annual", "source_end_date": end,
            "source_by_kind": {k: {"period_key": f"{end}|annual", "end_date": end, "report_type": "annual"}
                               for k in kinds},
            "source_pages": {k: [1, 2] for k in kinds},
            "extractor_version": STATEMENT_EXTRACTOR_VERSION,
            "prompt_version": prompts.STATEMENT_PROMPT_VERSION,
            "build_version": prompts.STATEMENT_BUILD_VERSION,
            **fields,
        }

    older = row("20250630", ["income", "balance"], total_revenue=1.0, total_assets=2.0,
                eps_unit={"divisor": 100}, restated_by_kind={"income": True, "balance": True},
                repaired_fields={"total_assets": {"to_row": "r9"}})
    newer = row("20251231", ["income"], total_revenue=3.0)
    merged = svc.merge_comparative_row(older, newer)
    assert merged["total_revenue"] == 3.0 and merged["total_assets"] == 2.0
    assert "eps_unit" not in merged  # 损益表换成了新来源（未以仙列示）
    assert merged["restated_by_kind"] == {"balance": True}
    assert merged["repaired_fields"] == {"total_assets": {"to_row": "r9"}}  # 资产负债表来源未变
    # repaired_fields 按科目所属报表逐条取舍：损益表换来源时 EPS 附注号修复随之替换，资产小计修复保留
    older_with_eps = {**older, "repaired_fields": {"total_assets": {"to_row": "r9"},
                                                   "basic_eps": {"to_row": None}}}
    merged = svc.merge_comparative_row(older_with_eps, newer)
    assert merged["repaired_fields"] == {"total_assets": {"to_row": "r9"}}
    newer_with_eps = {**newer, "repaired_fields": {"basic_eps": {"to_row": "r21"}}}
    merged = svc.merge_comparative_row(older, newer_with_eps)
    assert merged["repaired_fields"] == {"total_assets": {"to_row": "r9"}, "basic_eps": {"to_row": "r21"}}
    assert merged["build_version"] == prompts.STATEMENT_BUILD_VERSION
