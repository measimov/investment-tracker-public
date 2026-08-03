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
from app.services.security_analysis_prompts import parse_analysis_output

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
    result = svc.sync_symbol_profile(db, "00700", "港股")
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
             public_name="招商银行"):
    _patch_fetch(monkeypatch, {
        "fina_indicator": [{"end_date": "20251231", "roe": 15.0}],
    })
    monkeypatch.setattr(
        jobs, "chat_completion",
        lambda messages, **kw: {
            "content": llm_content,
            "model": "deepseek-v4-pro",
            "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
        },
    )
    monkeypatch.setattr(jobs, "resolve_public_security_name", lambda s, m: public_name)
    job = jobs.start_security_analysis_job(1, "600036", market)
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


def test_analysis_job_invalid_json_is_deterministic_failure(db, monkeypatch):
    job = _run_job(db, monkeypatch, llm_content="劣质输出不是JSON")
    assert job.status == "failed"
    assert "解析失败" in (job.error or "")
    assert job.attempt_count == 1  # 不烧重试
    assert db.query(SecurityAnalysis).count() == 0


def test_analysis_job_unsupported_market_fails_cleanly(db, monkeypatch):
    job = _run_job(db, monkeypatch, market="港股")
    assert job.status == "failed"
    assert "仅 A 股" in (job.error or "")


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

        # 未配置 LLM key → 409；港股 → 409
        monkeypatch.setattr("app.api.security_profiles.is_llm_configured", lambda: False)
        blocked = await client.post(
            "/api/securities/A股/600036/analysis-jobs", headers=auth
        )
        assert blocked.status_code == 409
        hk = await client.post("/api/securities/港股/00700/analysis-jobs", headers=auth)
        assert hk.status_code == 409

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
