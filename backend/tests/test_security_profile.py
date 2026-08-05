"""标的档案：数据集同步/归一/封顶、LLM 分析 job（JSON mode）、API。"""

import threading
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.background_job import BackgroundJob
from app.models.holding import Holding
from app.models.security_profile import SecurityAnalysis, SecurityProfileData
from app.models.user import User
from app.services import security_analysis_jobs as jobs
from app.services import security_profile_service as svc
from app.services.report_digest_prompts import DIGEST_PROMPT_VERSION
from app.services.report_sections import SECTION_EXTRACTOR_VERSION
from app.services.security_analysis_prompts import (
    build_system_prompt,
    parse_analysis_output,
)

from .helpers import reset_tables

RESET_MODELS = [SecurityAnalysis, SecurityProfileData, Holding]


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        reset_tables(session, RESET_MODELS)
        session.query(BackgroundJob).filter(
            BackgroundJob.job_type == "security_analysis"
        ).delete()
        session.commit()
        yield session
        session.rollback()
        reset_tables(session, RESET_MODELS)
        session.query(BackgroundJob).filter(
            BackgroundJob.job_type == "security_analysis"
        ).delete()
        session.commit()
    finally:
        session.close()


def _patch_fetch(monkeypatch, data_by_dataset):
    monkeypatch.setattr(
        svc, "fetch_dataset_rows",
        lambda dataset, symbol, market: list(data_by_dataset.get(dataset, [])),
    )


# ---------------------------------------------------------------------------
# 数据同步
# ---------------------------------------------------------------------------


def test_normalize_row_handles_nan_and_numpy():
    class FakeNumpy:
        def item(self):
            return 42

    row = svc._normalize_row({"a": float("nan"), "b": 1.5, "c": "x", "d": FakeNumpy()})
    assert row == {"a": None, "b": 1.5, "c": "x", "d": 42}


def test_sync_upsert_idempotent_and_refresh(db, monkeypatch):
    _patch_fetch(monkeypatch, {
        "fina_indicator": [
            {"end_date": "20251231", "ann_date": "20260325", "roe": 15.1},
            {"end_date": "20250630", "ann_date": "20250820", "roe": 14.0},
        ],
    })
    result = svc.sync_symbol_profile(db, "600036", "A股")
    assert result["supported"] is True
    assert result["datasets"]["fina_indicator"] == {"rows": 2, "inserted": 2}

    # 重同步：同键刷新不重复；修订值覆盖
    _patch_fetch(monkeypatch, {
        "fina_indicator": [{"end_date": "20251231", "ann_date": "20260325", "roe": 15.5}],
    })
    result = svc.sync_symbol_profile(db, "600036", "A股")
    assert result["datasets"]["fina_indicator"]["inserted"] == 0
    rows = db.query(SecurityProfileData).filter_by(dataset="fina_indicator").all()
    assert len(rows) == 2
    latest = next(r for r in rows if r.period_key == "20251231")
    assert latest.payload["roe"] == 15.5


def test_unsupported_market_is_flagged(db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        svc, "fetch_dataset_rows",
        lambda dataset, symbol, market: calls.append(dataset) or [],
    )
    result = svc.sync_symbol_profile(db, "TSM", "台股")
    assert result["supported"] is False
    assert calls == []


def test_single_dataset_failure_does_not_abort(db, monkeypatch):
    def fetch(dataset, symbol, market):
        if dataset == "pledge_stat":
            raise RuntimeError("抱歉，您每分钟最多访问该接口1次")
        if dataset == "fina_indicator":
            return [{"end_date": "20251231"}]
        return []

    monkeypatch.setattr(svc, "fetch_dataset_rows", fetch)
    result = svc.sync_symbol_profile(db, "600036", "A股")
    assert [f["dataset"] for f in result["failed"]] == ["pledge_stat"]
    assert result["datasets"]["fina_indicator"]["rows"] == 1


def test_daily_basic_pruned_to_recent_rows(db, monkeypatch):
    _patch_fetch(monkeypatch, {
        "daily_basic": [
            {"trade_date": f"2026{month:02d}{day:02d}", "pe": 6.0}
            for month in range(1, 3)
            for day in range(1, 21)
        ],
    })
    svc.sync_symbol_profile(db, "600036", "A股")
    count = db.query(SecurityProfileData).filter_by(dataset="daily_basic").count()
    assert count == svc.DAILY_BASIC_KEEP_ROWS


def test_load_symbol_profile_caps_and_orders_desc(db, monkeypatch):
    _patch_fetch(monkeypatch, {
        "fina_indicator": [
            {"end_date": f"20{yy}1231", "roe": float(yy)} for yy in range(10, 30)
        ],
    })
    svc.sync_symbol_profile(db, "600036", "A股")
    profile = svc.load_symbol_profile(db, "600036", "A股")
    rows = profile["datasets"]["fina_indicator"]
    assert len(rows) == svc.PROFILE_CAPS["fina_indicator"]
    assert rows[0]["end_date"] == "20291231"  # 倒序取最新


def test_statement_rows_keep_merged_latest_revision(db, monkeypatch):
    """三大报表：只留合并报表（report_type=1），同报告期取最新修正版。"""
    _patch_fetch(monkeypatch, {
        "income": [
            # 母公司报表（report_type=6）应被剔除
            {"end_date": "20251231", "report_type": "6", "ann_date": "20260325",
             "n_income": 1.0},
            # 同报告期两次披露：修正版（f_ann_date 更新）应胜出
            {"end_date": "20251231", "report_type": "1", "ann_date": "20260325",
             "f_ann_date": "20260325", "update_flag": "0", "n_income": 100.0},
            {"end_date": "20251231", "report_type": "1", "ann_date": "20260325",
             "f_ann_date": "20260428", "update_flag": "1", "n_income": 105.0},
            {"end_date": "20250630", "report_type": "1", "ann_date": "20250820",
             "f_ann_date": "20250820", "update_flag": "0", "n_income": 50.0},
        ],
    })
    # 直接走 fetch→upsert 全链路（fetch 被 monkeypatch，prepare 由 sync 前的
    # fetch_dataset_rows 应用；此处手动应用以测试预处理语义）
    rows = svc._merged_statement_rows(
        svc.fetch_dataset_rows("income", "600036", "A股")
    )
    svc.upsert_profile_rows(db, "600036", "A股", "income", rows)
    db.commit()

    stored = {
        row.period_key: row.payload
        for row in db.query(SecurityProfileData).filter_by(dataset="income").all()
    }
    assert set(stored) == {"20251231", "20250630"}
    assert stored["20251231"]["n_income"] == 105.0  # 最新修正版胜出
    assert stored["20251231"]["report_type"] == "1"


def test_analysis_input_compacts_statement_fields(db, monkeypatch):
    """[LLM 输入] 三大报表只保留核心科目白名单，非白名单列与空值不进输入。"""
    _patch_fetch(monkeypatch, {
        "income": [{
            "end_date": "20251231", "report_type": "1",
            "total_revenue": 3000.0, "n_income": 100.0, "basic_eps": 1.2,
            "sell_exp": 88.0, "fine_exp": None,  # 非白名单列 / 空值
        }],
        "cashflow": [{
            "end_date": "20251231", "report_type": "1",
            "n_cashflow_act": 150.0, "loss_fv_chg": 3.0,
        }],
    })
    svc.sync_symbol_profile(db, "600036", "A股")

    payload = jobs.build_analysis_input(db, "600036", "A股")
    income_row = payload["profile"]["income"][0]
    assert income_row == {
        "end_date": "20251231", "total_revenue": 3000.0,
        "n_income": 100.0, "basic_eps": 1.2,
    }
    cashflow_row = payload["profile"]["cashflow"][0]
    assert cashflow_row == {"end_date": "20251231", "n_cashflow_act": 150.0}
    # 非报表数据集不受白名单影响
    assert "income/balancesheet/cashflow" in payload["meta"]["data_semantics"]


def test_concurrent_profile_upsert_is_atomic(db):
    barrier = threading.Barrier(2)
    errors: list = []

    def worker():
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            svc.upsert_profile_rows(
                session, "600036", "A股", "fina_indicator",
                [{"end_date": "20251231", "roe": 15.0}],
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 - 断言用
            session.rollback()
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert db.query(SecurityProfileData).count() == 1


# ---------------------------------------------------------------------------
# 输出解析
# ---------------------------------------------------------------------------


def test_parse_analysis_output_validation():
    valid = parse_analysis_output(
        '{"tags":["高股息"],"risk_level":"low","summary":"稳","report_markdown":"## 财务质量趋势\\n好"}'
    )
    assert valid["tags"] == ["高股息"]
    assert valid["risk_level"] == "low"

    with pytest.raises(ValueError, match="不是合法 JSON"):
        parse_analysis_output("not json")
    with pytest.raises(ValueError, match="risk_level"):
        parse_analysis_output(
            '{"tags":["高股息"],"risk_level":"危","summary":"s","report_markdown":"r"}'
        )
    with pytest.raises(ValueError, match="tags"):
        parse_analysis_output('{"tags":"高","risk_level":"low","summary":"s","report_markdown":"r"}')
    with pytest.raises(ValueError, match="report_markdown"):
        parse_analysis_output(
            '{"tags":["高股息"],"risk_level":"low","summary":"s","report_markdown":""}'
        )


def test_parse_enforces_tag_whitelist_contract():
    """[评审回归] 白名单在解析层强制执行：JSON mode 只保证语法不保证遵守 prompt。"""
    # 模型自造标签 → 确定性失败
    with pytest.raises(ValueError, match="白名单外"):
        parse_analysis_output(
            '{"tags":["任意模型自造标签"],"risk_level":"low","summary":"s","report_markdown":"r"}'
        )
    # 数量越界：0 个与 5 个
    with pytest.raises(ValueError, match="1-4"):
        parse_analysis_output(
            '{"tags":[],"risk_level":"low","summary":"s","report_markdown":"r"}'
        )
    with pytest.raises(ValueError, match="1-4"):
        parse_analysis_output(
            '{"tags":["高股息","分红连续","业绩增长","估值偏低","估值偏高"],'
            '"risk_level":"low","summary":"s","report_markdown":"r"}'
        )
    # "数据不足"语义约束：不得配 low；medium 合法
    with pytest.raises(ValueError, match="数据不足"):
        parse_analysis_output(
            '{"tags":["数据不足"],"risk_level":"low","summary":"s","report_markdown":"r"}'
        )
    ok = parse_analysis_output(
        '{"tags":["数据不足"],"risk_level":"medium","summary":"s","report_markdown":"r"}'
    )
    assert ok["tags"] == ["数据不足"]


# ---------------------------------------------------------------------------
# 分析 job
# ---------------------------------------------------------------------------

VALID_LLM_OUTPUT = (
    '{"tags":["高股息","分红连续"],"risk_level":"low",'
    '"summary":"财务稳健，分红连续。",'
    '"report_markdown":"## 财务质量趋势\\n稳健\\n\\n## 历史股东回报\\n连续分红"}'
)


def _run_job(db, monkeypatch, *, llm_content=VALID_LLM_OUTPUT, market="A股",
             symbol="600036", public_name="招商银行", digest_result=None,
             datasets=None):
    _patch_fetch(monkeypatch, datasets or {
        "fina_indicator": [{"end_date": "20251231", "roe": 15.0}],
    })
    # 财报摘要/商业画像管线断网：分析 job 会调用真实实现（打 cninfo/tushare/LLM）
    from app.services import business_profile_service as bp_svc
    from app.services import report_digest_service as digest_svc

    monkeypatch.setattr(
        digest_svc, "ensure_report_digests",
        lambda db_, s, m, max_new: digest_result
        or {"total": 0, "completed": 0, "generated": 0, "remaining": 0, "gaps": []},
    )
    monkeypatch.setattr(bp_svc, "ensure_peer_list", lambda db_, s, m: [])
    monkeypatch.setattr(bp_svc, "ensure_business_profile", lambda db_, s, m: None)
    monkeypatch.setattr(
        jobs, "chat_completion",
        lambda messages, **kw: {
            "content": llm_content,
            "model": "deepseek-v4-pro",
            "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
        },
    )
    monkeypatch.setattr(jobs, "resolve_public_security_name", lambda s, m: public_name)
    job = jobs.start_security_analysis_job(1, symbol, market)
    jobs.run_security_analysis_job(job["id"])
    return db.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()


def test_analysis_job_creates_row(db, monkeypatch):
    job = _run_job(db, monkeypatch)
    assert job.status == "succeeded"

    analysis = db.query(SecurityAnalysis).one()
    assert analysis.tags == ["高股息", "分红连续"]
    assert analysis.risk_level == "low"
    assert analysis.name == "招商银行"
    assert "财务质量趋势" in analysis.content
    assert analysis.total_tokens == 300
    # [评审回归] 字段是"抓取日"而非"数据截止日"：旧报告期（20251231）数据
    # 今天抓取 → data_fetched_at 为今天，数据时效由档案 latest_periods 承载
    assert analysis.data_fetched_at == date.today()
    profile = svc.load_symbol_profile(db, "600036", "A股")
    assert profile["latest_periods"]["fina_indicator"] == "20251231"
    assert job.data["analysis_id"] == analysis.id


def test_analysis_name_never_leaks_user_holding_names(db, monkeypatch):
    """[评审回归] 全局分析不得读取任何用户的持仓名称：两个用户各自的手工
    命名都不能进入全局行，名称只来自公共元数据。"""
    db.add(Holding(user_id=1, symbol="600036", name="用户A的私有备注名", market="A股",
                   quantity=Decimal("100"), avg_cost=Decimal("30"),
                   total_cost=Decimal("3000"), currency="CNY"))
    db.add(Holding(user_id=2, symbol="600036", name="用户B的另一个名字", market="A股",
                   quantity=Decimal("50"), avg_cost=Decimal("31"),
                   total_cost=Decimal("1550"), currency="CNY"))
    db.commit()

    _run_job(db, monkeypatch, public_name="招商银行(公共元数据)")
    analysis = db.query(SecurityAnalysis).one()
    assert analysis.name == "招商银行(公共元数据)"
    assert "用户A" not in (analysis.name or "")
    assert "用户B" not in (analysis.name or "")

    # 公共元数据不可得 → 留空，仍不回退到用户持仓名
    db.query(SecurityAnalysis).delete()
    db.query(BackgroundJob).filter(BackgroundJob.job_type == jobs.JOB_TYPE).delete()
    db.commit()
    _run_job(db, monkeypatch, public_name=None)
    assert db.query(SecurityAnalysis).one().name is None


def test_start_job_rejects_different_symbol_while_active(db, monkeypatch):
    """[评审回归] 活跃任务去重必须校验标的：另一标的请求不得复用现有任务。"""
    first = jobs.start_security_analysis_job(1, "600036", "A股")
    assert first["status"] == "queued"

    with pytest.raises(jobs.AnalysisBusyError, match="600036"):
        jobs.start_security_analysis_job(1, "000001", "A股")

    # 同一标的重复请求：返回同一活跃任务（幂等）
    again = jobs.start_security_analysis_job(1, "600036", "A股")
    assert again["id"] == first["id"]


def test_analysis_input_includes_digests_and_gaps(db, monkeypatch):
    """财报摘要与缺口进入分析输入；摘要缺失时 gaps 如实携带。"""
    svc.upsert_profile_row(db, "600036", "A股", "report_digest", "20251231|annual", {
        "status": "ok", "report_type": "annual", "end_date": "20251231",
        "extractor_version": SECTION_EXTRACTOR_VERSION,
        "prompt_version": DIGEST_PROMPT_VERSION,
        "source_url": "http://example/1.pdf",
        "digest": {"经营回顾": "稳健", "业务分部占比": "零售 57%",
                   "主营收入结构": "净利息", "一次性项目": "无",
                   "会计信号": "无", "关键数字": ["营收 3391 亿"]},
    })
    db.commit()

    job = _run_job(
        db, monkeypatch,
        digest_result={"total": 3, "completed": 1, "generated": 0, "remaining": 0,
                       "gaps": ["20241231 报告下载或章节抽取失败"]},
    )
    assert job.status == "succeeded"
    analysis = db.query(SecurityAnalysis).one()
    payload = analysis.input_payload
    assert payload["report_digests"][0]["digest"]["业务分部占比"] == "零售 57%"
    assert payload["report_digest_gaps"] == ["20241231 报告下载或章节抽取失败"]
    assert "report_digests=" in payload["meta"]["data_semantics"]


def test_analysis_job_invalid_json_is_deterministic_failure(db, monkeypatch):
    job = _run_job(db, monkeypatch, llm_content="劣质输出不是JSON")
    assert job.status == "failed"
    assert "解析失败" in (job.error or "")
    assert job.attempt_count == 1  # 不烧重试
    assert db.query(SecurityAnalysis).count() == 0


def test_analysis_job_unsupported_market_fails_cleanly(db, monkeypatch):
    job = _run_job(db, monkeypatch, market="台股")
    assert job.status == "failed"
    assert "暂不支持基本面数据" in (job.error or "")


# ---------------------------------------------------------------------------
# 美股（EDGAR）
# ---------------------------------------------------------------------------


def _patch_edgar(monkeypatch, *, lookup={"cik": "0000320193", "title": "Apple Inc."},
                 facts=None):
    from app.services import report_fetchers

    monkeypatch.setattr(report_fetchers, "edgar_lookup", lambda symbol: lookup)
    if facts is not None:
        monkeypatch.setattr(report_fetchers, "edgar_companyfacts", lambda cik: facts)
    return report_fetchers


def _usd_item(end, val, *, fp="FY", form="10-K", filed="2026-02-01"):
    return {"end": end, "fp": fp, "form": form, "filed": filed, "val": val}


def test_edgar_pivot_fallback_chain_and_filed_latest(monkeypatch):
    """EDGAR 透视：兜底链首个有值概念生效；同 (end,fp) 多 filing 取 filed
    最新（重述胜）；USD/shares 单位（EPS）可读；end_date 归一为 8 位。"""
    facts = {"facts": {"us-gaap": {
        # 首选概念 RevenueFromContract... 缺失 → 链上第二位 Revenues 生效
        "Revenues": {"units": {"USD": [_usd_item("2025-12-31", 1000.0)]}},
        # 链上两个概念都有值 → 首位 CostOfRevenue 生效（break 语义）
        "CostOfRevenue": {"units": {"USD": [_usd_item("2025-12-31", 600.0)]}},
        "CostOfGoodsAndServicesSold": {
            "units": {"USD": [_usd_item("2025-12-31", 999.0)]}
        },
        # 同 (end,fp) 两次 filing：10-K/A（filed 更新）重述值胜出
        "NetIncomeLoss": {"units": {"USD": [
            _usd_item("2025-12-31", 200.0),
            _usd_item("2025-12-31", 210.0, form="10-K/A", filed="2026-04-01"),
        ]}},
        "EarningsPerShareBasic": {
            "units": {"USD/shares": [_usd_item("2025-12-31", 6.1)]}
        },
    }}}
    _patch_edgar(monkeypatch, facts=facts)

    rows = svc.fetch_dataset_rows("edgar_companyfacts", "AAPL", "美股")
    assert len(rows) == 1
    row = rows[0]
    assert row["end_date"] == "20251231"
    assert row["fp"] == "FY"
    assert row["currency"] == "USD"
    assert row["total_revenue"] == 1000.0
    assert row["cost_of_revenue"] == 600.0
    assert row["n_income_attr_p"] == 210.0
    assert row["basic_eps"] == 6.1


def test_edgar_split_sga_is_summed_for_foreign_issuers(monkeypatch):
    """[回归锁] PDD/BABA 实测都不报 SellingGeneralAndAdministrativeExpense，
    只报 SellingAndMarketing + GeneralAndAdministrative 两条。

    兜底链解决"同一概念不同 tag"，解决不了"一个概念拆成两个 tag"——不求和的话
    sga_exp 整格留空，Beneish M-score 的 SGAI 因子直接算不出来。
    """
    facts = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [
            _usd_item("2025-12-31", 1000.0), _usd_item("2024-12-31", 900.0),
        ]}},
        "SellingAndMarketingExpense": {"units": {"USD": [
            _usd_item("2025-12-31", 300.0), _usd_item("2024-12-31", 250.0),
        ]}},
        "GeneralAndAdministrativeExpense": {"units": {"USD": [
            _usd_item("2025-12-31", 50.0),  # 2024 年只报了营销费
        ]}},
    }}}
    _patch_edgar(monkeypatch, facts=facts)
    rows = {r["end_date"]: r for r in svc.fetch_dataset_rows(
        "edgar_companyfacts", "PDD", "美股"
    )}
    assert rows["20251231"]["sga_exp"] == 350.0
    # [回归锁] 缺分项时必须留空。只披露营销费的年份若把它当完整 SGA，
    # SGAI/M-score 会得到数值正常但系统性偏低的结果——下游无从知道它不完整
    assert rows["20241231"].get("sga_exp") is None


def test_edgar_concept_chain_wins_over_component_sum(monkeypatch):
    """公司自己报了合计科目时，分项求和不得覆盖它（逐期判断：同一家公司
    可能早年报合计、近年改拆分）。"""
    facts = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [
            _usd_item("2025-12-31", 1000.0), _usd_item("2024-12-31", 900.0),
        ]}},
        "SellingGeneralAndAdministrativeExpense": {
            "units": {"USD": [_usd_item("2025-12-31", 400.0)]}
        },
        "SellingAndMarketingExpense": {"units": {"USD": [
            _usd_item("2025-12-31", 300.0), _usd_item("2024-12-31", 250.0),
        ]}},
    }}}
    _patch_edgar(monkeypatch, facts=facts)
    rows = {r["end_date"]: r for r in svc.fetch_dataset_rows(
        "edgar_companyfacts", "X", "美股"
    )}
    assert rows["20251231"]["sga_exp"] == 400.0  # 合计科目胜出
    # 2024 只有营销费一项 → 不求和（分项不齐全）
    assert rows["20241231"].get("sga_exp") is None


def test_edgar_broad_receivable_tags_are_not_treated_as_trade_receivable(monkeypatch):
    """[回归锁] 贷款/票据/其他应收与营业收入没有同一经济含义。

    拿它们兜底 accounts_receiv，会让应收-营收增速差与 Beneish DSRI 生成看似
    完整实则无效的风险信号。PDD 实测只有这两种口径 → 该项如实留空。
    """
    facts = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [_usd_item("2025-12-31", 1000.0)]}},
        "NotesAndLoansReceivableNetCurrent": {
            "units": {"USD": [_usd_item("2025-12-31", 120.0)]}
        },
        "OtherReceivablesNetCurrent": {
            "units": {"USD": [_usd_item("2025-12-31", 80.0)]}
        },
    }}}
    _patch_edgar(monkeypatch, facts=facts)
    rows = svc.fetch_dataset_rows("edgar_companyfacts", "PDD", "美股")
    assert rows[0].get("accounts_receiv") is None


def test_edgar_unregistered_symbol_raises(monkeypatch):
    _patch_edgar(monkeypatch, lookup=None)
    with pytest.raises(ValueError, match="SEC"):
        svc.fetch_dataset_rows("edgar_companyfacts", "ZZZZ", "美股")


def test_us_sync_and_load_route_only_edgar_dataset(db, monkeypatch):
    """[A股回归保护] 注册表按市场分发：美股只同步/加载 edgar_companyfacts，
    不触碰任何 Tushare 数据集。"""
    def fetch(dataset, symbol, market):
        assert dataset == "edgar_companyfacts", f"美股不应拉取 {dataset}"
        return [{"end_date": "20251231", "fp": "FY", "total_revenue": 1000.0}]

    monkeypatch.setattr(svc, "fetch_dataset_rows", fetch)
    result = svc.sync_symbol_profile(db, "AAPL", "美股")
    assert result["supported"] is True
    assert set(result["datasets"]) == {"edgar_companyfacts"}

    profile = svc.load_symbol_profile(db, "AAPL", "美股")
    assert set(profile["datasets"]) == {"edgar_companyfacts"}
    assert profile["latest_periods"]["edgar_companyfacts"] == "20251231|FY"


def test_build_system_prompt_market_branches():
    """prompt 分支：美股禁用 A股专属风险标签并改用 10-K 风险因素；
    A股保留审计/质押/增减持数据源；未知市场回退 A股骨架。"""
    cn = build_system_prompt("A股")
    us = build_system_prompt("美股")
    assert "fina_audit" in cn
    assert "禁止使用" not in cn
    assert "禁止使用" in us
    for banned in ("高质押", "大股东减持", "大股东增持", "解禁临近", "审计非标"):
        assert banned in us  # 明示列入禁用清单
    # 中概股报 20-F 而非 10-K：来源约束必须同时授权两种年报，否则模型
    # 拿到 report_type=20-F 的摘要却没被允许使用它
    assert "10-K" in us and "20-F" in us
    assert "10-K/20-F 风险因素摘要" in us
    # 共享骨架：两市场都保留利润质量章节与同业不展开约束
    for prompt in (cn, us):
        assert "利润质量与会计风险" in prompt
        assert "禁止对同业本身展开任何分析" in prompt
    assert "fina_audit" in build_system_prompt("未知市场")  # 回退 A股


def test_resolve_public_name_us_uses_edgar_title(monkeypatch):
    _patch_edgar(monkeypatch)
    assert jobs.resolve_public_security_name("AAPL", "美股") == "Apple Inc."
    _patch_edgar(monkeypatch, lookup=None)
    assert jobs.resolve_public_security_name("ZZZZ", "美股") is None


def test_analysis_job_us_market_end_to_end(db, monkeypatch):
    """美股分析 job 全链路：EDGAR 数据集入库 → 透视行映射利润质量 →
    市场语义与 prompt 分支进入输入。"""
    job = _run_job(
        db, monkeypatch, market="美股", symbol="AAPL", public_name="Apple Inc.",
        datasets={"edgar_companyfacts": [{
            "end_date": "20251231", "fp": "FY", "currency": "USD",
            "total_revenue": 1000.0, "cost_of_revenue": 600.0,
            "n_income_attr_p": 200.0, "total_assets": 2000.0,
            "n_cashflow_act": 260.0,
        }]},
    )
    assert job.status == "succeeded"
    analysis = db.query(SecurityAnalysis).one()
    assert analysis.market == "美股"
    assert analysis.name == "Apple Inc."

    payload = analysis.input_payload
    assert payload["meta"]["market"] == "美股"
    assert "无审计意见/质押" in payload["meta"]["data_semantics"]
    assert payload["profile"]["edgar_companyfacts"][0]["total_revenue"] == 1000.0
    # 利润质量指标由 EDGAR 透视行映射计算（非 Tushare 报表）
    year = payload["earnings_quality"]["per_year"]["2025"]
    assert year["cfo_ni_ratio"] == 1.3  # 260/200
    assert year["gross_margin"] == 40.0


def test_profile_api_quality_uses_market_statements(db):
    """[缺陷回归] 详情页 earnings_quality 必须按市场映射报表行：美股由 EDGAR
    透视行计算，而非读空的 Tushare 报表键（否则非 A股永远 no_data）。"""
    from app.api.security_profiles import get_symbol_profile

    svc.upsert_profile_row(db, "AAPL", "美股", "edgar_companyfacts", "20250927|FY", {
        "end_date": "20250927", "fp": "FY", "currency": "USD",
        "total_revenue": 1000.0, "n_income_attr_p": 200.0,
        "total_assets": 2000.0, "n_cashflow_act": 300.0,
    })
    db.commit()

    body = get_symbol_profile("美股", "AAPL", None, db)
    assert body["capabilities"]["report_digest"] is True
    assert body["earnings_quality"]["per_year"]["2025"]["cfo_ni_ratio"] == 1.5


# ---------------------------------------------------------------------------
# 港股（Yahoo 结构化）
# ---------------------------------------------------------------------------


def test_to_yahoo_hk_code():
    from app.services.report_fetchers import to_yahoo_hk_code

    assert to_yahoo_hk_code("00700") == "0700.HK"
    assert to_yahoo_hk_code("09988") == "9988.HK"
    assert to_yahoo_hk_code("388") == "0388.HK"
    with pytest.raises(ValueError, match="非法港股代码"):
        to_yahoo_hk_code("ABC")
    with pytest.raises(ValueError, match="非法港股代码"):
        to_yahoo_hk_code("")


def test_yahoo_fundamentals_merges_series_by_year(monkeypatch):
    """Yahoo 时序合并：多序列按 asOfDate 合并每年一行；null 占位跳过；
    未知序列忽略；行结构与 EDGAR 透视行对齐（fp=FY + currency 透传）。"""
    from app.services import report_fetchers as rf

    payload = {"timeseries": {"result": [
        {"meta": {"type": ["annualTotalRevenue"]},
         "annualTotalRevenue": [
             {"asOfDate": "2024-12-31", "currencyCode": "CNY",
              "reportedValue": {"raw": 660.0}},
             {"asOfDate": "2025-12-31", "currencyCode": "CNY",
              "reportedValue": {"raw": 720.0}},
         ]},
        {"meta": {"type": ["annualNetIncome"]},
         "annualNetIncome": [
             None,  # 序列缺年份的 null 占位
             {"asOfDate": "2025-12-31", "currencyCode": "CNY",
              "reportedValue": {"raw": 194.0}},
         ]},
        {"meta": {"type": ["annualUnknownSeries"]}, "annualUnknownSeries": []},
    ]}}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return FakeResponse()

    monkeypatch.setattr(rf.requests, "get", fake_get)
    rows = rf.yahoo_hk_fundamentals("00700")

    assert "0700.HK" in captured["url"]
    assert [r["end_date"] for r in rows] == ["20251231", "20241231"]
    assert rows[0] == {
        "end_date": "20251231", "fp": "FY", "currency": "CNY",
        "total_revenue": 720.0, "n_income_attr_p": 194.0,
    }
    assert "n_income_attr_p" not in rows[1]  # 缺科目年份不补假值


def test_hk_sync_and_load_route_only_yahoo_dataset(db, monkeypatch):
    """[A股回归保护] 港股只同步/加载 yahoo_fundamentals，不触碰 Tushare 数据集。"""
    def fetch(dataset, symbol, market):
        assert dataset == "yahoo_fundamentals", f"港股不应拉取 {dataset}"
        return [{"end_date": "20251231", "fp": "FY", "currency": "CNY",
                 "total_revenue": 720.0}]

    monkeypatch.setattr(svc, "fetch_dataset_rows", fetch)
    result = svc.sync_symbol_profile(db, "00700", "港股")
    assert result["supported"] is True
    assert set(result["datasets"]) == {"yahoo_fundamentals"}

    profile = svc.load_symbol_profile(db, "00700", "港股")
    assert set(profile["datasets"]) == {"yahoo_fundamentals"}
    assert profile["latest_periods"]["yahoo_fundamentals"] == "20251231"


def test_build_system_prompt_hk_branch():
    """港股 prompt：禁用 A股专属标签、明示数据源缺失、risk_level 不得 low。"""
    hk = build_system_prompt("港股")
    assert "禁止使用" in hk
    for banned in ("高质押", "大股东减持", "解禁临近", "审计非标"):
        assert banned in hk
    assert "risk_level 不得为 low" in hk
    # 已接入披露易年报全文：风险来源改为年报「主要風險」章节，但结构化科目
    # 仍只有近 3-5 年，风险等级下限保留
    assert "主要風險" in hk
    assert "仅近 3-5 年" in hk
    assert "利润质量与会计风险" in hk  # 共享骨架保留


def test_analysis_job_hk_market_end_to_end(db, monkeypatch):
    """港股分析 job 全链路：Yahoo 数据集入库 → 透视行映射利润质量（有限科目）
    → 港股语义进入输入。"""
    hk_output = (
        '{"tags":["数据不足"],"risk_level":"medium",'
        '"summary":"仅雅虎年度科目，数据边界有限。",'
        '"report_markdown":"## 财务质量趋势\\n有限"}'
    )
    job = _run_job(
        db, monkeypatch, market="港股", symbol="00700", public_name="腾讯控股",
        llm_content=hk_output,
        datasets={"yahoo_fundamentals": [{
            "end_date": "20251231", "fp": "FY", "currency": "CNY",
            "total_revenue": 720.0, "n_income_attr_p": 200.0,
            "total_assets": 2000.0, "n_cashflow_act": 300.0,
        }]},
    )
    assert job.status == "succeeded"
    analysis = db.query(SecurityAnalysis).one()
    assert analysis.market == "港股"
    assert analysis.name == "腾讯控股"

    payload = analysis.input_payload
    assert payload["meta"]["market"] == "港股"
    # 港股风险来源已改为年报摘要，但年报未必设风险章节——meta 与 system prompt
    # 必须说同一件事，否则同一次调用里两个来源边界互相矛盾
    semantics = payload["meta"]["data_semantics"]
    assert "report_digests=披露易年报全文的 AI 摘要" in semantics
    assert "无审计意见/质押/增减持/解禁数据源" in semantics
    assert "年报未设「主要風險」章节时该项为空" in semantics
    assert payload["profile"]["yahoo_fundamentals"][0]["currency"] == "CNY"
    year = payload["earnings_quality"]["per_year"]["2025"]
    assert year["cfo_ni_ratio"] == 1.5  # 300/200
    assert year["gross_margin"] is None  # 无成本科目 → 留空非报错


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture
def api_user():
    session = SessionLocal()
    try:
        user = session.query(User).filter(User.username == "demo").one()
        original = user.hashed_password
        user.hashed_password = get_password_hash("profile-api-password")
        session.commit()
        yield user.id
        user.hashed_password = original
        session.commit()
    finally:
        session.close()


@pytest.mark.anyio
async def test_profile_api_flow(db, api_user, monkeypatch):
    db.add(Holding(user_id=api_user, symbol="600036", name="招商银行", market="A股",
                   quantity=Decimal("100"), avg_cost=Decimal("30"),
                   total_cost=Decimal("3000"), currency="CNY"))
    db.add(SecurityAnalysis(
        symbol="600036", market="A股", name="招商银行",
        tags=["高股息"], risk_level="low", summary="稳健",
        content="## 全文", model="deepseek-v4-pro", input_payload={},
    ))
    db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/api/auth/token",
            json={"username": "demo", "password": "profile-api-password"},
        )
        auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

        # 持仓分析摘要列表（不含全文）
        listed = await client.get("/api/securities/analyses", headers=auth)
        rows = listed.json()
        assert len(rows) == 1
        assert rows[0]["tags"] == ["高股息"]
        assert "content" not in rows[0]

        # 最新完整分析
        detail = await client.get("/api/securities/A股/600036/analysis", headers=auth)
        assert detail.json()["content"] == "## 全文"
        missing = await client.get("/api/securities/A股/999999/analysis", headers=auth)
        assert missing.status_code == 404

        # 档案端点（空档案也返回结构）
        profile = await client.get("/api/securities/A股/600036/profile", headers=auth)
        body = profile.json()
        assert body["supported"] is True
        assert set(body["datasets"]) == set(svc.DATASETS)
        assert body["capabilities"] == svc.MARKET_CAPABILITIES["A股"]

        # 未配置 LLM key → 409；不支持市场 → 409
        monkeypatch.setattr("app.api.security_profiles.is_llm_configured", lambda: False)
        blocked = await client.post(
            "/api/securities/A股/600036/analysis-jobs", headers=auth
        )
        assert blocked.status_code == 409
        tw = await client.post("/api/securities/台股/2330/analysis-jobs", headers=auth)
        assert tw.status_code == 409

        # [评审回归] 活跃任务存在时另一标的请求 → 409 并点名进行中标的；
        # 同标的重复请求 → 幂等返回同一任务（inline 执行 no-op 保持 queued）
        monkeypatch.setattr("app.api.security_profiles.is_llm_configured", lambda: True)
        monkeypatch.setattr(
            "app.api.security_profiles.run_security_analysis_job", lambda job_id: None
        )
        first = await client.post(
            "/api/securities/A股/600036/analysis-jobs", headers=auth
        )
        assert first.status_code == 200
        other = await client.post(
            "/api/securities/A股/000001/analysis-jobs", headers=auth
        )
        assert other.status_code == 409
        assert "600036" in other.json()["detail"]
        same = await client.post(
            "/api/securities/A股/600036/analysis-jobs", headers=auth
        )
        assert same.status_code == 200
        assert same.json()["id"] == first.json()["id"]

        # 任务端点 404 兜底
        missing_job = await client.get(
            "/api/securities/analysis-jobs/nonexistent", headers=auth
        )
        assert missing_job.status_code == 404


@pytest.mark.anyio
async def test_report_sections_endpoint_skips_failed_rows_before_limit(db, api_user):
    """[评审回归] 最近三期全部抽取失败时，端点仍须返回更早的成功节选——
    成功状态要在限制条数前过滤，不能先 limit(3) 再在 Python 里筛。"""
    for year, status in (
        ("2025", "failed"), ("2024", "failed"), ("2023", "failed"), ("2022", "ok"),
    ):
        svc.upsert_profile_row(
            db, "600036", "A股", "report_section", f"{year}1231|annual",
            {
                "extract_status": status,
                "attempts": 2,
                "sections": {"mdna": f"{year} 经营分析"} if status == "ok" else {},
                "source_url": f"https://static.cninfo.com.cn/{year}.PDF",
            },
        )
    db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/api/auth/token",
            json={"username": "demo", "password": "profile-api-password"},
        )
        auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
        response = await client.get(
            "/api/securities/A股/600036/report-sections", headers=auth
        )

    body = response.json()
    assert [row["period_key"] for row in body] == ["20221231|annual"]
    assert body[0]["sections"]["mdna"] == "2022 经营分析"


@pytest.mark.anyio
async def test_report_sections_endpoint_previews_by_default(db, api_user):
    """抽取期不再截断后单节可达十万字符量级，三份报告的全文足以让这个响应
    到 MB 级——默认只回预览，`?full=1` 才给全文。"""
    from app.api.security_profiles import SECTION_PREVIEW_CHARS

    long_body = "经营分析内容。" * 3_000
    svc.upsert_profile_row(
        db, "600036", "A股", "report_section", "20251231|annual",
        {"extract_status": "ok", "sections": {"mdna": long_body, "business": "短"}},
    )
    db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/api/auth/token",
            json={"username": "demo", "password": "profile-api-password"},
        )
        auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
        preview = (await client.get(
            "/api/securities/A股/600036/report-sections", headers=auth
        )).json()
        full = (await client.get(
            "/api/securities/A股/600036/report-sections?full=1", headers=auth
        )).json()

    assert len(preview[0]["sections"]["mdna"]) == SECTION_PREVIEW_CHARS
    assert preview[0]["truncated_preview"] == {"mdna": True, "business": False}
    assert preview[0]["sections"]["business"] == "短"  # 短节不受影响
    assert full[0]["sections"]["mdna"] == long_body
    assert "truncated_preview" not in full[0]


# ---------------------------------------------------------------------------
# [评审回归] EDGAR 期间口径 / 市场禁用标签
# ---------------------------------------------------------------------------


def test_edgar_rejects_period_mismatched_facts(monkeypatch):
    """[评审回归] (end,fp) 不唯一：同 end/fp/filed 下并存单季与累计值，
    只有 start 不同。必须按期间长度选定口径，不能任由后出现者覆盖。

    实测 AAPL ('2018-09-29','FY') 同时含全年 265.6B 与 Q4 单季 62.9B。
    """
    facts = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [
            # 全年口径（约 364 天）
            {"start": "2017-10-01", "end": "2018-09-29", "fp": "FY",
             "form": "10-K", "filed": "2019-10-31", "val": 265595000000},
            # 同 end/fp/filed 的 Q4 单季值（约 90 天）——必须被丢弃
            {"start": "2018-07-01", "end": "2018-09-29", "fp": "FY",
             "form": "10-K", "filed": "2019-10-31", "val": 62900000000},
            # 季度期：年初至今累计（约 273 天）必须被丢弃，只留单季
            {"start": "2017-10-01", "end": "2018-06-30", "fp": "Q3",
             "form": "10-Q", "filed": "2019-07-31", "val": 202695000000},
            {"start": "2018-04-01", "end": "2018-06-30", "fp": "Q3",
             "form": "10-Q", "filed": "2019-07-31", "val": 53265000000},
        ]}},
        # 时点科目（无 start）不受期间过滤影响
        "Assets": {"units": {"USD": [
            {"end": "2018-09-29", "fp": "FY", "form": "10-K",
             "filed": "2019-10-31", "val": 365725000000},
        ]}},
    }}}
    _patch_edgar(monkeypatch, facts=facts)

    rows = {(r["end_date"], r["fp"]): r
            for r in svc.fetch_dataset_rows("edgar_companyfacts", "AAPL", "美股")}
    assert rows[("20180929", "FY")]["total_revenue"] == 265595000000  # 全年而非单季
    assert rows[("20180929", "FY")]["total_assets"] == 365725000000  # 时点科目照收
    assert rows[("20180630", "Q3")]["total_revenue"] == 53265000000  # 单季而非累计


def test_edgar_restatement_still_wins_within_same_period_kind(monkeypatch):
    """同一口径下的重述（filed 更新）仍应覆盖原值——期间过滤不得削掉这条。"""
    facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        {"start": "2024-10-01", "end": "2025-09-27", "fp": "FY",
         "form": "10-K", "filed": "2025-11-01", "val": 400000000000},
        {"start": "2024-10-01", "end": "2025-09-27", "fp": "FY",
         "form": "10-K/A", "filed": "2026-02-01", "val": 416161000000},
    ]}}}}}
    _patch_edgar(monkeypatch, facts=facts)
    rows = svc.fetch_dataset_rows("edgar_companyfacts", "AAPL", "美股")
    assert rows[0]["total_revenue"] == 416161000000


def test_parse_rejects_market_banned_tags():
    """[评审回归] 美股禁用标签必须在解析层确定性拒绝——prompt 是请求不是保证，
    语法合法的"高质押"若被接受就会持久化并展示在持仓页。"""
    payload = (
        '{"tags":["高质押"],"risk_level":"high","summary":"s","report_markdown":"r"}'
    )
    # A股（有对应数据源）照常接受
    assert parse_analysis_output(payload, market="A股")["tags"] == ["高质押"]
    # 美股确定性失败
    with pytest.raises(ValueError, match="禁用标签"):
        parse_analysis_output(payload, market="美股")
    for tag in ("大股东减持", "大股东增持", "解禁临近", "审计非标"):
        with pytest.raises(ValueError, match="禁用标签"):
            parse_analysis_output(
                f'{{"tags":["{tag}"],"risk_level":"high","summary":"s",'
                '"report_markdown":"r"}',
                market="美股",
            )
    # 市场无关的通用标签不受影响
    ok = parse_analysis_output(
        '{"tags":["业绩增长"],"risk_level":"medium","summary":"s","report_markdown":"r"}',
        market="美股",
    )
    assert ok["tags"] == ["业绩增长"]


def test_analysis_job_us_banned_tag_is_deterministic_failure(db, monkeypatch):
    """job 层贯通：美股输出 A股专属标签 → 确定性失败、不落分析行、不烧重试。"""
    job = _run_job(
        db, monkeypatch, market="美股", symbol="AAPL", public_name="Apple Inc.",
        llm_content=(
            '{"tags":["审计非标"],"risk_level":"high","summary":"s",'
            '"report_markdown":"r"}'
        ),
        datasets={"edgar_companyfacts": [
            {"end_date": "20251231", "fp": "FY", "total_revenue": 1000.0}
        ]},
    )
    assert job.status == "failed"
    assert "禁用标签" in (job.error or "")
    assert job.attempt_count == 1
    assert db.query(SecurityAnalysis).count() == 0


def test_parse_enforces_hk_market_constraints():
    """[评审回归] 港股的禁用标签与 risk_level 下限必须在解析层强制执行——
    只写在 prompt 里时，模型返回 {"tags":["高质押"],"risk_level":"low"} 会被
    接受并持久化。"""
    # 禁用标签（无对应数据源）
    for tag in ("高质押", "大股东减持", "大股东增持", "解禁临近", "审计非标"):
        with pytest.raises(ValueError, match="禁用标签"):
            parse_analysis_output(
                f'{{"tags":["{tag}"],"risk_level":"high","summary":"s",'
                '"report_markdown":"r"}',
                market="港股",
            )
    # risk_level 下限：港股不得 low
    with pytest.raises(ValueError, match="不得低于 medium"):
        parse_analysis_output(
            '{"tags":["业绩增长"],"risk_level":"low","summary":"s","report_markdown":"r"}',
            market="港股",
        )
    # medium/high 正常通过；同一输出在 A股 市场不受这两条约束
    ok = parse_analysis_output(
        '{"tags":["业绩增长"],"risk_level":"medium","summary":"s","report_markdown":"r"}',
        market="港股",
    )
    assert ok["risk_level"] == "medium"
    assert parse_analysis_output(
        '{"tags":["高质押"],"risk_level":"low","summary":"s","report_markdown":"r"}',
        market="A股",
    )["risk_level"] == "low"


def test_analysis_job_hk_low_risk_is_deterministic_failure(db, monkeypatch):
    """job 层贯通：港股返回 risk_level=low → 确定性失败，不落分析行。"""
    job = _run_job(
        db, monkeypatch, market="港股", symbol="00700", public_name="腾讯控股",
        llm_content=(
            '{"tags":["业绩增长"],"risk_level":"low","summary":"s",'
            '"report_markdown":"r"}'
        ),
        datasets={"yahoo_fundamentals": [
            {"end_date": "20251231", "fp": "FY", "currency": "CNY",
             "total_revenue": 720.0}
        ]},
    )
    assert job.status == "failed"
    assert "不得低于 medium" in (job.error or "")
    assert job.attempt_count == 1
    assert db.query(SecurityAnalysis).count() == 0


def test_edgar_concept_fallback_applies_per_period(monkeypatch):
    """[评审回归] 兜底链必须逐期应用而非整条序列一次性 break：公司更换 XBRL
    tag 后首选概念只覆盖近年，旧年份只存在于备用概念里——一次性 break 会让
    旧年份整段丢失。"""
    facts = {"facts": {"us-gaap": {
        # 首选概念只有 2025
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            {"start": "2025-01-01", "end": "2025-12-31", "fp": "FY",
             "form": "10-K", "filed": "2026-02-01", "val": 1000.0},
        ]}},
        # 备用概念只有 2024（换 tag 之前的年份）
        "Revenues": {"units": {"USD": [
            {"start": "2024-01-01", "end": "2024-12-31", "fp": "FY",
             "form": "10-K", "filed": "2025-02-01", "val": 900.0},
        ]}},
    }}}
    _patch_edgar(monkeypatch, facts=facts)

    rows = {r["end_date"]: r
            for r in svc.fetch_dataset_rows("edgar_companyfacts", "AAPL", "美股")}
    assert set(rows) == {"20251231", "20241231"}  # 两期都保留
    assert rows["20251231"]["total_revenue"] == 1000.0
    assert rows["20241231"]["total_revenue"] == 900.0


def test_edgar_higher_priority_concept_wins_within_same_period(monkeypatch):
    """同一报告期两个概念都有值时，链上更靠前者胜——与逐期兜底不冲突，
    且不受概念在 facts 字典中出现顺序的影响。"""
    facts = {"facts": {"us-gaap": {
        # 低优先级概念先出现且 filed 更新，仍不得压过高优先级概念
        "Revenues": {"units": {"USD": [
            {"start": "2024-10-01", "end": "2025-09-27", "fp": "FY",
             "form": "10-K", "filed": "2026-06-01", "val": 111.0},
        ]}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            {"start": "2024-10-01", "end": "2025-09-27", "fp": "FY",
             "form": "10-K", "filed": "2025-11-01", "val": 999.0},
        ]}},
    }}}
    _patch_edgar(monkeypatch, facts=facts)
    rows = svc.fetch_dataset_rows("edgar_companyfacts", "AAPL", "美股")
    assert rows[0]["total_revenue"] == 999.0


def test_edgar_row_cap_reserves_annual_quota(monkeypatch):
    """[评审回归] 按 end_date 一刀切封顶会被数量占优的季度行占满，年报史被
    静默丢弃（实测 AAPL 158 个期间里 FY 仅 20 个，旧 40 行封顶只剩 4 个年度）。
    年度与季度须各留各的额度。"""
    facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": (
        # 20 个年度期（2006-2025）
        [{"start": f"{year}-01-01", "end": f"{year}-12-31", "fp": "FY",
          "form": "10-K", "filed": f"{year + 1}-02-01", "val": float(year)}
         for year in range(2006, 2026)]
        # 12 个季度期（比年度新，一刀切排序会全部挤在前面）
        + [{"start": f"2026-{month:02d}-01", "end": f"2026-{month + 2:02d}-28", "fp": "Q1",
            "form": "10-Q", "filed": "2026-05-01", "val": 1.0}
           for month in range(1, 10)]
    )}}}}}
    _patch_edgar(monkeypatch, facts=facts)

    rows = svc.fetch_dataset_rows("edgar_companyfacts", "AAPL", "美股")
    annual = [r for r in rows if r["fp"] == "FY"]
    quarterly = [r for r in rows if r["fp"] != "FY"]
    assert len(annual) == svc.EDGAR_ANNUAL_KEEP  # 年报额度不被季度挤占
    assert len(quarterly) == svc.EDGAR_QUARTERLY_KEEP
    assert annual[0]["end_date"] == "20251231"  # 年度取最新的 N 期
    assert annual[-1]["end_date"] == f"{2026 - svc.EDGAR_ANNUAL_KEEP}1231"
    # 落库上限须容得下两类额度之和，否则又在读取层被截断
    assert svc.PROFILE_CAPS["edgar_companyfacts"] >= len(rows)


# ---------------------------------------------------------------------------
# 分析进度（阶段回写）
# ---------------------------------------------------------------------------


def test_analysis_job_reports_stage_progress(db, monkeypatch):
    """六个阶段依次落在 data，progress_percent 单调不减，终态 100%。"""
    seen: list = []
    original = jobs.set_job_progress

    def spy(job_id, job_type, **kwargs):
        if "stage" in kwargs:
            seen.append((kwargs.get("stage"), kwargs.get("completed")))
        return original(job_id, job_type, **kwargs)

    monkeypatch.setattr(jobs, "set_job_progress", spy)
    job = _run_job(db, monkeypatch)
    assert job.status == "succeeded"

    stages = [name for name, _ in seen]
    assert stages == [name for name, _ in jobs.ANALYSIS_STAGES] + ["done"]
    completed = [value for _, value in seen]
    assert completed == sorted(completed)  # 单调不减
    assert job.data["progress_percent"] == 100
    assert job.data["completed"] == jobs.STAGE_TOTAL
    assert job.data["stage_label"] == "已完成"


def test_queued_analysis_job_carries_initial_progress_fields(db):
    """排队态就带齐进度字段，前端不必等第一次回写。"""
    job = jobs.start_security_analysis_job(1, "600036", "A股")
    assert job["stage_label"] == "排队中"
    assert job["total"] == jobs.STAGE_TOTAL
    assert job["completed"] == 0
    assert job["progress_percent"] == 0


def test_analysis_stage_callback_failure_does_not_break_analysis(db, monkeypatch):
    """进度回写异常不得拖垮分析本身（回调内吞错）。"""
    _patch_fetch(monkeypatch, {"fina_indicator": [{"end_date": "20251231", "roe": 15.0}]})
    monkeypatch.setattr(
        jobs, "chat_completion",
        lambda messages, **kw: {
            "content": VALID_LLM_OUTPUT, "model": "m",
            "usage": {"total_tokens": 1},
        },
    )
    monkeypatch.setattr(jobs, "resolve_public_security_name", lambda s, m: "招商银行")

    def boom(stage, extra):
        raise RuntimeError("进度服务挂了")

    outcome = jobs.analyze_one(db, "600036", "A股", digest_max_new=0, on_stage=boom)
    assert outcome["status"] == "succeeded"
    assert db.query(SecurityAnalysis).count() == 1


def test_analyze_one_skips_digests_when_max_new_zero(db, monkeypatch):
    """批量 fast 模式的前提：digest_max_new=0 时完全不进摘要管线。"""
    _patch_fetch(monkeypatch, {"fina_indicator": [{"end_date": "20251231", "roe": 15.0}]})
    monkeypatch.setattr(
        jobs, "chat_completion",
        lambda messages, **kw: {"content": VALID_LLM_OUTPUT, "model": "m", "usage": {}},
    )
    monkeypatch.setattr(jobs, "resolve_public_security_name", lambda s, m: None)

    from app.services import report_digest_service as digest_svc

    def explode(*args, **kwargs):
        raise AssertionError("fast 模式不得调用 ensure_report_digests")

    monkeypatch.setattr(digest_svc, "ensure_report_digests", explode)
    outcome = jobs.analyze_one(db, "600036", "A股", digest_max_new=0)
    assert outcome["status"] == "succeeded"
    assert outcome["digest_gaps"] == []
