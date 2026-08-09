"""批量标的分析：目标集合、进度、失败语义、新鲜度跳过、终止、互斥、API。

全部 monkeypatch analyze_one，不触发真实外呼与 LLM。
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.background_job import BackgroundJob
from app.models.holding import Holding
from app.models.security_profile import SecurityAnalysis
from app.models.broker_account import BrokerAccount
from app.models.security_rule import SecurityRule
from app.models.user import User
from app.services import security_analysis_batch_jobs as batch
from app.services.llm_client import LLMClientError, LLMNotConfiguredError

from .helpers import reset_tables

JOB_TYPES = [
    "security_analysis_batch", "security_analysis", "report_digest_backfill",
]


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        reset_tables(session, [SecurityAnalysis, Holding, SecurityRule, BrokerAccount])
        session.query(BackgroundJob).filter(
            BackgroundJob.job_type.in_(JOB_TYPES)
        ).delete(synchronize_session=False)
        session.commit()
        yield session
        session.rollback()
        reset_tables(session, [SecurityAnalysis, Holding, SecurityRule, BrokerAccount])
        session.query(BackgroundJob).filter(
            BackgroundJob.job_type.in_(JOB_TYPES)
        ).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


def _hold(db, symbol: str, market: str, user_id: int = 1, quantity: str = "100",
          broker_account_id=None):
    db.add(Holding(
        user_id=user_id, symbol=symbol, name=symbol, market=market,
        broker_account_id=broker_account_id,
        quantity=Decimal(quantity), avg_cost=Decimal("10"),
        total_cost=Decimal("1000"), currency="CNY",
    ))
    db.commit()


def _run(db, monkeypatch, *, outcomes=None, side_effect=None, user_id=1, **start_kwargs):
    """启动并执行一个批量任务；outcomes 按调用顺序返回。"""
    calls: list = []

    def fake_analyze(db_, symbol, market, *, digest_max_new=2, on_stage=None):
        calls.append({"symbol": symbol, "market": market, "digest_max_new": digest_max_new})
        if on_stage:
            on_stage("llm_analysis", {})
        if side_effect:
            side_effect(len(calls))
        if outcomes:
            return outcomes[min(len(calls) - 1, len(outcomes) - 1)](symbol, market)
        return {
            "symbol": symbol, "market": market, "status": "succeeded",
            "analysis_id": len(calls), "error": None, "error_kind": None,
            "degraded": [], "digest_gaps": [],
        }

    monkeypatch.setattr(batch, "analyze_one", fake_analyze)
    monkeypatch.setattr(batch.settings, "security_analysis_batch_pause_seconds", 0)
    job = batch.start_batch_analysis_job(db, user_id, **start_kwargs)
    batch.run_batch_analysis_job(job["id"])
    stored = db.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()
    db.refresh(stored)
    return stored, calls


def _ok(symbol, market):
    return {
        "symbol": symbol, "market": market, "status": "succeeded",
        "analysis_id": 1, "error": None, "error_kind": None,
        "degraded": [], "digest_gaps": [],
    }


def _fail(error="boom", kind="parse"):
    def build(symbol, market):
        return {
            "symbol": symbol, "market": market, "status": "failed",
            "analysis_id": None, "error": error, "error_kind": kind,
            "degraded": [], "digest_gaps": [],
        }

    return build


# ---------------------------------------------------------------------------
# 目标集合
# ---------------------------------------------------------------------------


def test_targets_dedupe_filter_and_rotate_markets(db):
    """跨账户去重、非支持市场剔除、清仓剔除、市场轮转排序。

    轮转（A股→美股→港股→…）让同一 Tushare 接口的相邻调用被其他市场拉开，
    是零成本的接口级降频。
    """
    account = BrokerAccount(
        user_id=1, broker="CMB", account_name="测试账户", base_currency="CNY"
    )
    db.add(account)
    db.commit()
    _hold(db, "600036", "A股", broker_account_id=None)
    _hold(db, "600036", "A股", broker_account_id=account.id)  # 同标的多账户 → 去重
    _hold(db, "000001", "A股")
    _hold(db, "AAPL", "美股")
    _hold(db, "00700", "港股")
    _hold(db, "BTC", "加密货币")  # 不支持市场
    _hold(db, "600519", "A股", quantity="0")  # 已清仓

    targets = batch.get_batch_analysis_targets(db, 1)
    assert [(t["symbol"], t["market"]) for t in targets] == [
        ("000001", "A股"), ("AAPL", "美股"), ("00700", "港股"), ("600036", "A股"),
    ]


def test_targets_exclude_rule_driven_symbols(db):
    """排除清单与现金管理标的不进批量：货币基金做基本面分析纯烧 token。"""
    _hold(db, "600036", "A股")
    _hold(db, "511990", "A股")
    _hold(db, "000001", "A股")
    db.add(SecurityRule(
        user_id=1, rule_type="EXCLUDE", symbol="000001", market="A股", payload={},
    ))
    db.add(SecurityRule(
        user_id=1, rule_type="CASH_MANAGEMENT", symbol="511990", market="A股", payload={},
    ))
    db.commit()

    targets = batch.get_batch_analysis_targets(db, 1)
    assert [t["symbol"] for t in targets] == ["600036"]


def test_start_without_targets_raises_and_creates_no_job(db):
    with pytest.raises(batch.NoBatchTargetsError):
        batch.start_batch_analysis_job(db, 1)
    assert db.query(BackgroundJob).filter(
        BackgroundJob.job_type == batch.JOB_TYPE
    ).count() == 0


def test_full_mode_rejects_too_many_symbols(db):
    for index in range(batch.FULL_MODE_MAX_SYMBOLS + 1):
        _hold(db, f"60000{index}", "A股")
    with pytest.raises(batch.NoBatchTargetsError, match="深度模式"):
        batch.start_batch_analysis_job(db, 1, include_report_digests=True)
    # 快速模式不受限
    assert batch.start_batch_analysis_job(db, 1)["total"] == batch.FULL_MODE_MAX_SYMBOLS + 1


# ---------------------------------------------------------------------------
# 执行与进度
# ---------------------------------------------------------------------------


def test_batch_runs_all_targets_and_reports_progress(db, monkeypatch):
    for symbol in ("600036", "000001", "600519"):
        _hold(db, symbol, "A股")

    job, calls = _run(db, monkeypatch)
    assert job.status == "succeeded"
    assert len(calls) == 3
    assert job.data["success_count"] == 3
    assert job.data["completed"] == 3
    assert job.data["progress_percent"] == 100
    assert job.data["current_symbol"] is None  # 收尾清空
    assert len(job.data["results"]) == 3
    assert {r["status"] for r in job.data["results"]} == {"succeeded"}


def test_fast_mode_skips_report_digests(db, monkeypatch):
    """[成本护栏] 默认 fast：不新补财报摘要（但仍使用库内已有的）。"""
    _hold(db, "600036", "A股")
    _, calls = _run(db, monkeypatch)
    assert calls[0]["digest_max_new"] == 0

    db.query(BackgroundJob).filter(BackgroundJob.job_type == batch.JOB_TYPE).delete()
    db.commit()
    _, calls = _run(db, monkeypatch, include_report_digests=True)
    assert calls[0]["digest_max_new"] == batch.FULL_MODE_DIGEST_MAX_NEW


def test_single_symbol_failure_does_not_stop_batch(db, monkeypatch):
    for symbol in ("600036", "000001", "600519"):
        _hold(db, symbol, "A股")

    job, calls = _run(
        db, monkeypatch, outcomes=[_ok, _fail("LLM 输出解析失败"), _ok],
    )
    assert len(calls) == 3  # 失败后继续
    assert job.status == "succeeded"
    assert job.data["success_count"] == 2
    assert job.data["failed_count"] == 1


def test_consecutive_failures_stop_early(db, monkeypatch):
    for index in range(6):
        _hold(db, f"60000{index}", "A股")

    job, calls = _run(db, monkeypatch, outcomes=[_fail()])
    assert len(calls) == batch.MAX_CONSECUTIVE_FAILURES  # 第 4 只不再调用
    assert job.status == "failed"
    assert "连续" in (job.error or "")
    assert "连续" in job.data["abort_reason"]


def test_fatal_error_aborts_immediately(db, monkeypatch):
    """LLM 401/配额致命错误对整批等价：第一只命中即停，不再逐只烧。"""
    for symbol in ("600036", "000001", "600519"):
        _hold(db, symbol, "A股")

    def raise_401(call_index):
        raise LLMClientError("unauthorized", status_code=401)

    job, calls = _run(db, monkeypatch, side_effect=raise_401)
    assert len(calls) == 1
    assert job.status == "failed"
    assert job.data["abort_reason"].startswith("遇到无法继续的错误")


def test_tushare_rate_error_does_not_abort(db, monkeypatch):
    """接口频率错误只是降级信号（该数据集跳过），不得中止整批。"""
    for symbol in ("600036", "000001"):
        _hold(db, symbol, "A股")

    def raise_rate(call_index):
        if call_index == 1:
            raise RuntimeError("抱歉，您每分钟最多访问该接口1次")

    job, calls = _run(db, monkeypatch, side_effect=raise_rate)
    assert len(calls) == 2  # 继续跑完
    assert job.data["failed_count"] == 1
    assert job.data["success_count"] == 1


def test_llm_not_configured_aborts(db, monkeypatch):
    for symbol in ("600036", "000001"):
        _hold(db, symbol, "A股")

    def raise_not_configured(call_index):
        raise LLMNotConfiguredError("未配置 LLM API Key")

    job, calls = _run(db, monkeypatch, side_effect=raise_not_configured)
    assert len(calls) == 1
    assert job.status == "failed"


# ---------------------------------------------------------------------------
# 新鲜度跳过与续跑
# ---------------------------------------------------------------------------


def test_recent_analyses_are_skipped(db, monkeypatch):
    _hold(db, "600036", "A股")
    _hold(db, "000001", "A股")
    db.add(SecurityAnalysis(
        symbol="600036", market="A股", tags=["高股息"], risk_level="low",
        summary="s", content="c", model="m", input_payload={},
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    ))
    db.commit()

    job, calls = _run(db, monkeypatch)
    assert [c["symbol"] for c in calls] == ["000001"]  # 600036 被跳过
    assert job.data["skipped_count"] == 1
    assert job.data["success_count"] == 1
    skipped = [r for r in job.data["results"] if r["status"] == "skipped"]
    assert skipped[0]["symbol"] == "600036"


def test_force_bypasses_freshness(db, monkeypatch):
    _hold(db, "600036", "A股")
    db.add(SecurityAnalysis(
        symbol="600036", market="A股", tags=["高股息"], risk_level="low",
        summary="s", content="c", model="m", input_payload={},
        created_at=datetime.now(timezone.utc),
    ))
    db.commit()

    _, calls = _run(db, monkeypatch, force=True)
    assert [c["symbol"] for c in calls] == ["600036"]


def test_stale_analysis_is_not_skipped(db, monkeypatch):
    _hold(db, "600036", "A股")
    db.add(SecurityAnalysis(
        symbol="600036", market="A股", tags=["高股息"], risk_level="low",
        summary="s", content="c", model="m", input_payload={},
        created_at=datetime.now(timezone.utc) - timedelta(days=3),
    ))
    db.commit()

    _, calls = _run(db, monkeypatch)
    assert [c["symbol"] for c in calls] == ["600036"]


def test_completed_keys_resume_without_reanalyzing(db, monkeypatch):
    """[重试幂等] 被接管/重试时，已完成的标的零重复调用（不重烧 LLM）。"""
    for symbol in ("600036", "000001"):
        _hold(db, symbol, "A股")

    calls: list = []
    monkeypatch.setattr(
        batch, "analyze_one",
        lambda db_, symbol, market, **kw: calls.append(symbol) or _ok(symbol, market),
    )
    monkeypatch.setattr(batch.settings, "security_analysis_batch_pause_seconds", 0)
    job = batch.start_batch_analysis_job(db, 1)

    row = db.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()
    row.data = {**row.data, "completed_keys": ["A股|000001"], "success_count": 1}
    db.commit()

    batch.run_batch_analysis_job(job["id"])
    assert calls == ["600036"]  # 已完成的不再跑


# ---------------------------------------------------------------------------
# 终止
# ---------------------------------------------------------------------------


def test_cancel_stops_at_symbol_boundary(db, monkeypatch):
    """终止在标的边界生效：当前标的跑完即收尾，已生成的分析保留。"""
    for symbol in ("600036", "000001", "600519"):
        _hold(db, symbol, "A股")

    holder: dict = {}

    def fake_analyze(db_, symbol, market, **kwargs):
        if len(holder.setdefault("calls", [])) == 0:
            holder["calls"].append(symbol)
            batch.request_batch_cancel(holder["job_id"], 1)  # 第一只跑完时请求终止
            return _ok(symbol, market)
        holder["calls"].append(symbol)
        return _ok(symbol, market)

    monkeypatch.setattr(batch, "analyze_one", fake_analyze)
    monkeypatch.setattr(batch.settings, "security_analysis_batch_pause_seconds", 0)
    job = batch.start_batch_analysis_job(db, 1)
    holder["job_id"] = job["id"]
    batch.run_batch_analysis_job(job["id"])

    stored = db.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()
    db.refresh(stored)
    assert stored.status == "interrupted"
    assert stored.data["cancelled"] is True
    assert len(holder["calls"]) == 1  # 只跑了第一只
    assert stored.data["success_count"] == 1  # 已生成的保留
    assert "已保留" in stored.data["abort_reason"]


def test_cancel_unknown_job_returns_none(db):
    assert batch.request_batch_cancel("nonexistent", 1) is None


# ---------------------------------------------------------------------------
# 互斥与 API
# ---------------------------------------------------------------------------


@pytest.fixture
def api_user():
    session = SessionLocal()
    try:
        user = session.query(User).filter(User.username == "demo").one()
        original = user.hashed_password
        user.hashed_password = get_password_hash("batch-api-password")
        session.commit()
        yield user.id
        user.hashed_password = original
        session.commit()
    finally:
        session.close()


@pytest.mark.anyio
async def test_batch_api_flow_and_route_isolation(db, api_user, monkeypatch):
    _hold(db, "600036", "A股", user_id=api_user)
    monkeypatch.setattr(
        "app.api.security_profiles.is_llm_configured", lambda: True
    )
    monkeypatch.setattr(
        "app.api.security_profiles.run_batch_analysis_job", lambda job_id: None
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/api/auth/token",
            json={"username": "demo", "password": "batch-api-password"},
        )
        auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

        # 活跃任务列表：无任务时 200 + 空列表（404 会触发前端全局错误通知）
        empty = await client.get("/api/securities/active-analysis-jobs", headers=auth)
        assert empty.status_code == 200 and empty.json() == []

        started = await client.post("/api/securities/analysis-batch-jobs", headers=auth)
        assert started.status_code == 200
        job_id = started.json()["id"]
        assert started.json()["total"] == 1  # start 阶段即算好，前端立刻可显示

        # 重复启动幂等
        again = await client.post("/api/securities/analysis-batch-jobs", headers=auth)
        assert again.json()["id"] == job_id

        # 互斥：批量活跃时单标的分析与摘要回填都 409
        blocked = await client.post(
            "/api/securities/A股/600036/analysis-jobs", headers=auth
        )
        assert blocked.status_code == 409
        assert "批量分析" in blocked.json()["detail"]
        blocked_backfill = await client.post(
            "/api/securities/A股/600036/report-backfill-jobs", headers=auth
        )
        assert blocked_backfill.status_code == 409

        # 活跃任务可查
        active = await client.get("/api/securities/active-analysis-jobs", headers=auth)
        assert [row["id"] for row in active.json()] == [job_id]

        # [路由回归] 批量与单标的两个 job 端点互不串味
        detail = await client.get(
            f"/api/securities/analysis-batch-jobs/{job_id}", headers=auth
        )
        assert detail.status_code == 200
        crossed = await client.get(
            f"/api/securities/analysis-jobs/{job_id}", headers=auth
        )
        assert crossed.status_code == 404

        # 终止
        cancelled = await client.post(
            f"/api/securities/analysis-batch-jobs/{job_id}/cancel", headers=auth
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["cancel_requested"] is True
        assert (
            await client.post(
                "/api/securities/analysis-batch-jobs/nope/cancel", headers=auth
            )
        ).status_code == 404


@pytest.mark.anyio
async def test_batch_api_rejects_without_targets_or_llm(db, api_user, monkeypatch):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/api/auth/token",
            json={"username": "demo", "password": "batch-api-password"},
        )
        auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

        monkeypatch.setattr(
            "app.api.security_profiles.is_llm_configured", lambda: False
        )
        no_llm = await client.post("/api/securities/analysis-batch-jobs", headers=auth)
        assert no_llm.status_code == 409

        monkeypatch.setattr(
            "app.api.security_profiles.is_llm_configured", lambda: True
        )
        no_targets = await client.post(
            "/api/securities/analysis-batch-jobs", headers=auth
        )
        assert no_targets.status_code == 409
        assert "没有可分析" in no_targets.json()["detail"]


def test_batch_job_is_user_scoped(db, monkeypatch):
    _hold(db, "600036", "A股", user_id=1)
    monkeypatch.setattr(
        batch, "analyze_one", lambda db_, s, m, **kw: _ok(s, m)
    )
    job = batch.start_batch_analysis_job(db, 1)
    assert batch.get_batch_analysis_job(job["id"], 2) is None


# ---------------------------------------------------------------------------
# [评审回归] 致命错误走 outcome 路径而非异常
#
# 以下用例**不 monkeypatch analyze_one**，而是打通真实的
# analyze_one → sync_symbol_profile / chat_completion 链路——原实现只在异常
# 分支判致命错误，而真实链路里 LLM 4xx 与 Tushare token 失效都是"返回"而非
# "抛出"，因此中止逻辑实际不可达。
# ---------------------------------------------------------------------------


def _run_real(db, monkeypatch, *, user_id=1):
    from app.services import security_analysis_jobs as jobs

    monkeypatch.setattr(batch.settings, "security_analysis_batch_pause_seconds", 0)
    monkeypatch.setattr(jobs, "resolve_public_security_name", lambda s, m: None)
    # 摘要与画像管线断网（与本用例无关）
    from app.services import business_profile_service as bp_svc
    from app.services import report_digest_service as digest_svc

    monkeypatch.setattr(
        digest_svc, "ensure_report_digests",
        lambda db_, s, m, max_new: {"gaps": []},
    )
    monkeypatch.setattr(bp_svc, "ensure_peer_list", lambda db_, s, m: [])
    monkeypatch.setattr(bp_svc, "ensure_business_profile", lambda db_, s, m: None)

    job = batch.start_batch_analysis_job(db, user_id)
    batch.run_batch_analysis_job(job["id"])
    stored = db.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()
    db.refresh(stored)
    return stored


def test_llm_auth_error_aborts_through_real_analyze_one(db, monkeypatch):
    """LLM 401 经真实 analyze_one 返回 status=failed（不抛），批量仍须立即中止——
    否则会一路请求到连续 3 只才停，每只都白花一次鉴权失败的往返。"""
    from app.services import security_analysis_jobs as jobs
    from app.services import security_profile_service as profile_svc

    for symbol in ("600036", "000001", "600519"):
        _hold(db, symbol, "A股")
    monkeypatch.setattr(
        profile_svc, "fetch_dataset_rows",
        lambda dataset, symbol, market: [{"end_date": "20251231", "roe": 15.0}],
    )
    llm_calls: list = []

    def unauthorized(messages, **kwargs):
        llm_calls.append(1)
        raise LLMClientError("Authentication Fails", status_code=401)

    monkeypatch.setattr(jobs, "chat_completion", unauthorized)

    job = _run_real(db, monkeypatch)
    assert len(llm_calls) == 1  # 只打了一次，没有逐只重试
    assert job.status == "failed"
    assert job.data["abort_reason"].startswith("遇到无法继续的错误")
    assert db.query(SecurityAnalysis).count() == 0


def test_tushare_fatal_aborts_and_skips_degraded_analysis(db, monkeypatch):
    """Tushare token/权限失效经真实同步链路：不得再逐数据集重试、不得生成
    一份没有数据依据的"降级分析"、批量必须立即中止。"""
    from app.services import security_analysis_jobs as jobs
    from app.services import security_profile_service as profile_svc

    for symbol in ("600036", "000001"):
        _hold(db, symbol, "A股")

    fetched: list = []

    def fatal_fetch(dataset, symbol, market):
        fetched.append(dataset)
        raise RuntimeError("抱歉，您没有该接口权限")

    monkeypatch.setattr(profile_svc, "fetch_dataset_rows", fatal_fetch)
    monkeypatch.setattr(
        jobs, "chat_completion",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("致命错误后不应调用 LLM")),
    )

    job = _run_real(db, monkeypatch)
    assert len(fetched) == 1  # 首个数据集致命即停，不再逐集重试
    assert job.status == "failed"
    assert "致命错误" in (job.error or "")
    assert db.query(SecurityAnalysis).count() == 0


def test_ordinary_llm_4xx_is_symbol_level_not_batch_abort(db, monkeypatch):
    """非鉴权类 4xx（如 400 请求过长）只是本标的失败，批量继续。"""
    from app.services import security_analysis_jobs as jobs
    from app.services import security_profile_service as profile_svc

    for symbol in ("600036", "000001"):
        _hold(db, symbol, "A股")
    monkeypatch.setattr(
        profile_svc, "fetch_dataset_rows",
        lambda dataset, symbol, market: [{"end_date": "20251231", "roe": 15.0}],
    )
    calls: list = []

    def bad_request(messages, **kwargs):
        calls.append(1)
        raise LLMClientError("prompt too long", status_code=400)

    monkeypatch.setattr(jobs, "chat_completion", bad_request)

    job = _run_real(db, monkeypatch)
    assert len(calls) == 2  # 两只都尝试了
    assert job.status == "succeeded"  # 单只失败不中止整批
    assert job.data["failed_count"] == 2


def test_analyze_one_marks_fatal_kinds(db, monkeypatch):
    """analyze_one 的 error_kind 契约（批量据此判定是否整批中止）。"""
    from app.services import security_analysis_jobs as jobs
    from app.services import security_profile_service as profile_svc

    monkeypatch.setattr(jobs, "resolve_public_security_name", lambda s, m: None)
    monkeypatch.setattr(
        profile_svc, "fetch_dataset_rows",
        lambda dataset, symbol, market: [{"end_date": "20251231"}],
    )
    for status_code, expected in ((401, "llm_auth"), (402, "llm_auth"),
                                  (429, "llm_auth"), (400, "llm_4xx")):
        monkeypatch.setattr(
            jobs, "chat_completion",
            lambda *a, sc=status_code, **k: (_ for _ in ()).throw(
                LLMClientError("x", status_code=sc)
            ),
        )
        outcome = jobs.analyze_one(db, "600036", "A股", digest_max_new=0)
        assert outcome["error_kind"] == expected
    assert "llm_auth" in jobs.FATAL_ANALYSIS_ERROR_KINDS
    assert "llm_4xx" not in jobs.FATAL_ANALYSIS_ERROR_KINDS


@pytest.mark.anyio
async def test_batch_targets_preview_matches_job_total(db, api_user, monkeypatch):
    """[评审回归] 确认框数量必须与后端真实目标一致：仅按支持市场在前端估算会
    把已清仓、EXCLUDE、CASH_MANAGEMENT 标的算进去，用户看到虚高的数量与
    token 估算，启动后 job.total 又突然变小。"""
    _hold(db, "600036", "A股", user_id=api_user)
    _hold(db, "511990", "A股", user_id=api_user)  # 现金管理
    _hold(db, "000001", "A股", user_id=api_user)  # 排除清单
    _hold(db, "600519", "A股", user_id=api_user, quantity="0")  # 已清仓
    _hold(db, "BTC", "加密货币", user_id=api_user)  # 不支持市场
    db.add(SecurityRule(
        user_id=api_user, rule_type="CASH_MANAGEMENT", symbol="511990",
        market="A股", payload={},
    ))
    db.add(SecurityRule(
        user_id=api_user, rule_type="EXCLUDE", symbol="000001",
        market="A股", payload={},
    ))
    db.commit()

    monkeypatch.setattr("app.api.security_profiles.is_llm_configured", lambda: True)
    monkeypatch.setattr(
        "app.api.security_profiles.run_batch_analysis_job", lambda job_id: None
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/api/auth/token",
            json={"username": "demo", "password": "batch-api-password"},
        )
        auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

        preview = await client.get("/api/securities/analysis-batch-targets", headers=auth)
        assert preview.status_code == 200
        body = preview.json()
        assert body["total"] == 1  # 只剩 600036
        assert [t["symbol"] for t in body["targets"]] == ["600036"]

        started = await client.post("/api/securities/analysis-batch-jobs", headers=auth)
        # 预览数与真实 job.total 必须一致，否则确认框会骗人
        assert started.json()["total"] == body["total"]


def test_losing_ownership_mid_loop_stops_the_batch_immediately(db, monkeypatch):
    """[回归锁] 失权（被接管）后必须立刻停手，不得继续处理剩余标的。

    本模块的 progress() 在 analyze_one 的 try **内部**经 on_stage 被调用，
    所以哨兵异常要显式 re-raise 才不被兜底 except 吞成"本标的失败、继续下一只"
    ——#127 修的正是那个坑，#134 把 progress 收敛到 job_runtime 时必须原样保住。
    僵尸不停手 = 对剩余标的双倍调用 Tushare/EDGAR/LLM，产物两边各写一遍。
    """
    from app.services import job_runtime

    for suffix in range(3):
        _hold(db, f"60000{suffix}", "A股")

    # **瞬时**失权（只让 on_stage 那一次回写返回 None，之后恢复）：这是唯一能
    # 区分两条路径的构造。永久失权时后续每次 progress 都会再抛，循环两种写法
    # 都会停——测不出差别。瞬时失权下：
    #   有 re-raise：哨兵直接逃出循环 → 只处理了第一只；
    #   无 re-raise：被兜底 except 判成"本标的失败"，后续 progress 又能写了
    #                → 若无其事跑完全部三只（这正是要防的僵尸行为）。
    state = {"armed": False, "tripped": False}
    original = job_runtime.set_job_progress

    def spy(job_id, job_type, **kwargs):
        if state["armed"] and not state["tripped"]:
            state["tripped"] = True
            return None  # 模拟这一刻 attempt 已变（被 worker 接管）
        return original(job_id, job_type, **kwargs)

    monkeypatch.setattr(job_runtime, "set_job_progress", spy)

    calls: list = []

    def fake_analyze(db_, symbol, market, *, digest_max_new=2, on_stage=None):
        calls.append(symbol)
        state["armed"] = True
        try:
            if on_stage:
                on_stage("llm_analysis", {})  # 这一下触发哨兵（try 内部）
        finally:
            state["armed"] = False
        return {
            "symbol": symbol, "market": market, "status": "succeeded",
            "analysis_id": 1, "error": None, "error_kind": None,
            "degraded": [], "digest_gaps": [],
        }

    monkeypatch.setattr(batch, "analyze_one", fake_analyze)
    monkeypatch.setattr(batch.settings, "security_analysis_batch_pause_seconds", 0)

    job = batch.start_batch_analysis_job(db, 1)
    batch.run_batch_analysis_job(job["id"])  # 安静退出，不得抛出

    assert len(calls) == 1, f"失权后仍继续分析了剩余标的：{calls}"
    stored = db.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()
    db.refresh(stored)
    assert stored.status != "failed", "失权不是失败，僵尸不得把 job 标成 failed"
