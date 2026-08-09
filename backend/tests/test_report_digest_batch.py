"""批量财报摘要回填：目标、预览、进度、失败语义、续跑、取消、互斥、API。

全部 monkeypatch ensure_report_digests，不触发真实外呼与 LLM。
"""

from decimal import Decimal

import httpx
import pytest

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.background_job import BackgroundJob
from app.models.holding import Holding
from app.models.security_profile import SecurityProfileData
from app.models.user import User
from app.services import report_digest_batch_jobs as batch
from app.services.security_analysis_batch_jobs import NoBatchTargetsError
from app.services.security_profile_service import upsert_profile_row

from .helpers import reset_tables

JOB_TYPES = [
    "report_digest_batch", "security_analysis_batch",
    "security_analysis", "report_digest_backfill",
]


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        reset_tables(session, [SecurityProfileData, Holding])
        session.query(BackgroundJob).filter(
            BackgroundJob.job_type.in_(JOB_TYPES)
        ).delete(synchronize_session=False)
        session.commit()
        yield session
        session.rollback()
        reset_tables(session, [SecurityProfileData, Holding])
        session.query(BackgroundJob).filter(
            BackgroundJob.job_type.in_(JOB_TYPES)
        ).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


def _hold(db, symbol: str, market: str, user_id: int = 1):
    db.add(Holding(
        user_id=user_id, symbol=symbol, name=symbol, market=market,
        quantity=Decimal("100"), avg_cost=Decimal("10"),
        total_cost=Decimal("1000"), currency="CNY",
    ))
    db.commit()


def _ok(symbol, *, total=8, completed=4, generated=4, remaining=4, gaps=None,
        failed=0, fatal=None, permanently_failed=0, plan_incomplete=False):
    return {
        "total": total, "completed": completed, "generated": generated,
        "failed": failed, "permanently_failed": permanently_failed,
        "plan_incomplete": plan_incomplete,
        "remaining": remaining, "pending_periods": [],
        "gaps": gaps or [], "fatal": fatal,
    }


def _run(db, monkeypatch, *, outcomes=None, side_effect=None, user_id=1):
    calls: list = []

    def fake_ensure(db_, symbol, market, *, max_new):
        calls.append({"symbol": symbol, "market": market, "max_new": max_new})
        if side_effect:
            side_effect(len(calls))
        if outcomes:
            maker = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            return maker(symbol)
        return _ok(symbol)

    monkeypatch.setattr(batch, "ensure_report_digests", fake_ensure)
    monkeypatch.setattr(batch.settings, "security_analysis_batch_pause_seconds", 0)
    job = batch.start_digest_batch_job(db, user_id)
    batch.run_digest_batch_job(job["id"])
    return batch.get_digest_batch_job(job["id"], user_id), calls


# ---------------------------------------------------------------------------
# 目标与预览
# ---------------------------------------------------------------------------


def test_preview_counts_are_db_only(db, monkeypatch):
    """预览必须纯 DB 统计：外呼被显式炸掉仍能工作。"""
    from app.services import report_fetchers

    for name in ("cninfo_search_reports", "hkex_annual_reports"):
        monkeypatch.setattr(
            report_fetchers, name,
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("预览不得外呼")),
        )
    _hold(db, "600036", "A股")
    _hold(db, "00700", "港股")
    upsert_profile_row(db, "600036", "A股", "report_digest", "20251231|annual",
                       _current_digest_payload())
    db.commit()

    preview = batch.preview_digest_backfill(db, 1)
    assert preview["targets_total"] == 2
    assert preview["targets_without_digest"] == 1  # 00700 一份都没有
    assert preview["digests_existing"] == 1
    assert preview["per_symbol_budget"] == batch.DIGEST_BATCH_PER_SYMBOL


def _current_digest_payload(**overrides):
    from app.services.report_digest_prompts import DIGEST_PROMPT_VERSION
    from app.services.report_sections import SECTION_EXTRACTOR_VERSION

    payload = {
        "status": "ok",
        "extractor_version": SECTION_EXTRACTOR_VERSION,
        "prompt_version": DIGEST_PROMPT_VERSION,
    }
    payload.update(overrides)
    return payload


def test_preview_counts_only_rows_the_readers_would_use(db):
    """预览口径必须与读取路径一致：failed 行与版本过期行不算"已有摘要"。

    此前只按 dataset 数行：封顶失败行（attempts 用尽）、版本 bump 后的过期行
    都被计入——版本一升，预览显示"已有上百份"，而回填与分析实际一份都用不上。
    """
    _hold(db, "600036", "A股")
    # 1 份当前有效 + 1 份封顶失败 + 1 份版本过期：只有第一份算数
    upsert_profile_row(db, "600036", "A股", "report_digest", "20251231|annual",
                       _current_digest_payload())
    upsert_profile_row(db, "600036", "A股", "report_digest", "20241231|annual",
                       _current_digest_payload(status="failed"))
    upsert_profile_row(db, "600036", "A股", "report_digest", "20231231|annual",
                       {"status": "ok"})  # 缺版本字段 = 历史 v1 行
    _hold(db, "00700", "港股")
    # 00700 只有一份过期行：等价于"一份摘要都没有"
    upsert_profile_row(db, "00700", "港股", "report_digest", "20251231|annual",
                       _current_digest_payload(extractor_version=1))
    db.commit()

    preview = batch.preview_digest_backfill(db, 1)
    assert preview["digests_existing"] == 1, "失败/过期行被算进了已有摘要"
    assert preview["targets_without_digest"] == 1, "只剩过期行的标的应视同没有摘要"


def test_start_without_targets_raises(db):
    with pytest.raises(NoBatchTargetsError):
        batch.start_digest_batch_job(db, 1)
    assert db.query(BackgroundJob).filter_by(job_type=batch.JOB_TYPE).count() == 0


# ---------------------------------------------------------------------------
# 执行语义
# ---------------------------------------------------------------------------


def test_backfills_all_targets_and_aggregates(db, monkeypatch):
    _hold(db, "600036", "A股")
    _hold(db, "00700", "港股")
    _hold(db, "PDD", "美股")
    job, calls = _run(db, monkeypatch)
    assert job["status"] == "succeeded"
    assert job["completed"] == 3
    assert job["success_count"] == 3
    assert job["digests_generated"] == 12  # 3 × 4
    assert job["symbols_with_remaining"] == 3  # 每只都还有 remaining=4
    assert all(call["max_new"] == batch.DIGEST_BATCH_PER_SYMBOL for call in calls)
    # 市场轮转：相邻两只不同市场
    order = [f"{c['market']}/{c['symbol']}" for c in calls]
    assert order == ["A股/600036", "美股/PDD", "港股/00700"]


def test_single_symbol_failure_continues(db, monkeypatch):
    _hold(db, "600036", "A股")
    _hold(db, "600000", "A股")

    def boom_first(n):
        if n == 1:
            raise RuntimeError("意外崩溃")

    job, calls = _run(db, monkeypatch, side_effect=boom_first)
    assert job["status"] == "succeeded"
    assert job["failed_count"] == 1
    assert job["success_count"] == 1
    assert len(calls) == 2


def test_consecutive_failures_stop_early(db, monkeypatch):
    for index in range(5):
        _hold(db, f"60000{index}", "A股")

    def always_boom(n):
        raise RuntimeError("持续失败")

    job, calls = _run(db, monkeypatch, side_effect=always_boom)
    assert job["status"] == "failed"
    assert "连续" in (job.get("abort_reason") or "")
    assert len(calls) == batch.MAX_CONSECUTIVE_FAILURES  # 早停，没跑完 5 只


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("llm_not_configured", "未配置 LLM API Key（LLM_REPORT_API_KEY）"),
        ("llm_auth", "LLM 调用失败（HTTP 401）：invalid api key"),
        ("llm_rate_limited", "LLM 调用失败（HTTP 429）：rate limited"),
    ],
)
def test_fatal_kind_aborts_batch(db, monkeypatch, kind, message):
    """[回归锁] 整批中止靠**结构化 kind**，不是中文 gap 文案。

    无效 Key / 欠费 / 限流换个标的照样失败：继续跑只是把整批拖成"看起来在跑"
    的空转，最后 UI 还提示成功。
    """
    _hold(db, "600036", "A股")
    _hold(db, "600000", "A股")
    job, calls = _run(db, monkeypatch, outcomes=[
        lambda s: _ok(s, generated=0, failed=1, fatal={"kind": kind, "message": message}),
    ])
    assert job["status"] == "failed"
    assert message[:20] in (job.get("abort_reason") or "")
    assert job["failed_count"] == 1
    assert job["success_count"] == 0
    assert len(calls) == 1  # 第一只就中止


def test_abort_does_not_depend_on_gap_wording(db, monkeypatch):
    """[回归锁] gap 文案改成任意别的字样，中止判据仍必须生效。

    旧实现匹配 "未配置 LLM API Key" 子串——`report_digest_service` 里那句话
    一改，批量就退化成"全跑一遍白转"且谎报成功。
    """
    _hold(db, "600036", "A股")
    _hold(db, "600000", "A股")
    job, calls = _run(db, monkeypatch, outcomes=[
        lambda s: _ok(
            s, generated=0, failed=1,
            gaps=["LLM key missing"],  # 文案完全变了
            fatal={"kind": "llm_not_configured", "message": "LLM key missing"},
        ),
    ])
    assert job["status"] == "failed"
    assert len(calls) == 1


def test_all_reports_failed_is_not_counted_as_success(db, monkeypatch):
    """[回归锁] generated=0 且 failed>0 = 本轮每一份都失败，不是成功。

    只看 generated 会把"全部生成失败"记成成功、整批继续跑、UI 提示完成。
    对照组：generated=0 且 failed=0（全部命中缓存或该标的无年报）才是成功。
    """
    _hold(db, "600036", "A股")
    _hold(db, "600000", "A股")
    job, _ = _run(db, monkeypatch, outcomes=[
        lambda s: _ok(s, total=4, completed=0, generated=0, failed=4, remaining=0,
                      gaps=["20241231 摘要生成失败"]),
        lambda s: _ok(s, total=4, completed=4, generated=0, failed=0, remaining=0),
    ])
    assert job["failed_count"] == 1
    assert job["success_count"] == 1  # 全缓存命中的那只仍算成功
    assert job["digests_generated"] == 0
    assert job["status"] == "succeeded"  # 单只失败不中止整批


def test_consecutive_all_failed_symbols_stop_early(db, monkeypatch):
    """[回归锁] 连续多只标的"每份都失败"（含 section 段失败）必须触发早停。

    section 失败不进 failed 计数的话，这些标的都被记成成功，早停线永远
    踩不到——数据源整体故障时批量会把几十只全跑一遍。
    """
    for index in range(5):
        _hold(db, f"60000{index}", "A股")
    job, calls = _run(db, monkeypatch, outcomes=[
        lambda s: _ok(s, total=4, completed=0, generated=0, failed=4, remaining=0,
                      gaps=["报告下载或章节抽取失败"] * 4),
    ])
    assert job["status"] == "failed"
    assert "连续" in (job.get("abort_reason") or "")
    assert len(calls) == batch.MAX_CONSECUTIVE_FAILURES  # 没跑完 5 只


def test_all_reports_permanently_failed_is_not_success(db, monkeypatch):
    """[回归锁] 零尝试、零成品、全部封顶的标的必须记失败。

    封顶行不消耗预算是成本维度的正确选择，但结果维度它就是"这只标的所有
    可回填报告都永久失败"——记成功会让前端弹绿色"新生成 0 份"。
    对照组：缓存命中 + 部分封顶 = 有缺口的成功（blocked 计数外显）。
    """
    _hold(db, "600036", "A股")
    _hold(db, "600000", "A股")
    job, _ = _run(db, monkeypatch, outcomes=[
        # 第一只：全部封顶（摘要封顶 + section 封顶混合也一样）
        lambda s: _ok(s, total=4, completed=0, generated=0, failed=0,
                      remaining=0, permanently_failed=4,
                      gaps=["摘要生成失败（已封顶）"] * 4),
        # 第二只：3 份缓存命中 + 1 份封顶 = 有缺口的成功
        lambda s: _ok(s, total=4, completed=3, generated=0, failed=0,
                      remaining=0, permanently_failed=1),
    ])
    assert job["status"] == "succeeded"  # 单只失败不中止整批
    assert job["failed_count"] == 1
    assert job["success_count"] == 1
    assert job["digests_blocked"] == 5  # 两只的封顶数都外显（4 + 1）
    failed_row = next(r for r in job["results"] if r["status"] == "failed")
    assert "永久失败" in failed_row["error"]


def test_incomplete_plan_with_no_output_is_failure_and_trips_early_stop(db, monkeypatch):
    """[回归锁] 清单检索失败且零产出的标的记失败并计连败。

    源站整体故障时每个标的都是 partial-empty，不计失败 = 全量绿色完成、
    生成 0 份；计连败 = 连续三只即早停，不再逐只空转。
    对照组：complete-empty（真实无年报）仍是成功。
    """
    for index in range(5):
        _hold(db, f"60000{index}", "A股")
    job, calls = _run(db, monkeypatch, outcomes=[
        lambda s: _ok(s, total=0, completed=0, generated=0, remaining=0,
                      plan_incomplete=True,
                      gaps=["年报清单检索失败或不完整（数据源故障）"]),
    ])
    assert job["status"] == "failed"
    assert "连续" in (job.get("abort_reason") or "")
    assert len(calls) == batch.MAX_CONSECUTIVE_FAILURES
    assert job["success_count"] == 0


def test_incomplete_plan_with_output_is_still_not_success(db, monkeypatch):
    """[回归锁] partial 清单**有产出也不能记成功**。

    annual 检索失败而 semi 成功是本仓库自己的真实形态：那 1 份半年报会正常
    生成，产出非零——只在"零产出"时判失败的话，这里弹绿色完成，十年年报的
    缺口被完全隐藏。已生成的照常入账；连续上游 partial 仍要早停。
    """
    for index in range(5):
        _hold(db, f"60000{index}", "A股")
    job, calls = _run(db, monkeypatch, outcomes=[
        # 半年报生成成功 + 年报清单检索失败
        lambda s: _ok(s, total=1, completed=1, generated=1, remaining=0,
                      plan_incomplete=True,
                      gaps=["年报清单检索失败或不完整（数据源故障），本轮覆盖范围不可信"]),
    ])
    assert job["success_count"] == 0  # 一只都不得记成功
    assert job["status"] == "failed"  # 连续 partial 触发早停
    assert len(calls) == batch.MAX_CONSECUTIVE_FAILURES
    assert job["digests_generated"] == batch.MAX_CONSECUTIVE_FAILURES  # 产出仍入账
    row = job["results"][0]
    assert row["status"] == "failed"
    assert "本轮已生成 1 份仍保留" in row["error"]


def test_incomplete_plan_with_cache_hits_is_still_not_success(db, monkeypatch):
    """缓存命中（completed>0、零新生成）的 partial 同样不是成功。"""
    _hold(db, "600036", "A股")
    _hold(db, "600000", "A股")
    job, _ = _run(db, monkeypatch, outcomes=[
        lambda s: _ok(s, total=1, completed=1, generated=0, remaining=0,
                      plan_incomplete=True),
        lambda s: _ok(s, total=4, completed=4, generated=0, remaining=0),
    ])
    assert job["success_count"] == 1  # 只有 complete 清单的那只算成功
    assert job["failed_count"] == 1


def test_complete_empty_plan_is_still_success(db, monkeypatch):
    _hold(db, "515180", "A股")  # ETF：cninfo 无年报，是确定的答案
    job, _ = _run(db, monkeypatch, outcomes=[
        lambda s: _ok(s, total=0, completed=0, generated=0, remaining=0),
    ])
    assert job["status"] == "succeeded"
    assert job["success_count"] == 1
    assert job["failed_count"] == 0


def test_fatal_abort_preserves_generated_and_blocked_counts(db, monkeypatch):
    """[回归锁] fatal 中止前已落库的工作必须入账。

    ensure 是逐报告循环：完全可能先生成 2 份、跳过 1 份封顶，再在第 4 份
    撞上 401——中止时把 generated/blocked 记成 0，总数与结果行都在撒谎。
    """
    _hold(db, "600036", "A股")
    job, _ = _run(db, monkeypatch, outcomes=[
        lambda s: _ok(
            s, total=6, completed=2, generated=2, failed=1, remaining=0,
            permanently_failed=1,
            fatal={"kind": "llm_auth", "message": "LLM 调用失败（HTTP 401）：bad key"},
        ),
    ])
    assert job["status"] == "failed"
    assert job["digests_generated"] == 2  # 中止前的产出入账
    assert job["digests_blocked"] == 1
    row = job["results"][0]
    assert row["generated"] == 2 and row["blocked"] == 1


def test_blocked_counts_survive_the_round_failure_branch(db, monkeypatch):
    """[回归锁] "历史封顶 + 本轮失败"混合时 blocked 不得漏计。

    ensure 先跳过封顶报告再处理后续，failed>0 与 permanently_failed>0 完全
    可能同时返回；本轮失败分支优先命中，漏加 digests_blocked 的话前端少报
    已知的永久失败——与"成功与失败标的都汇总 blocked"的契约不一致。
    """
    _hold(db, "600036", "A股")
    job, _ = _run(db, monkeypatch, outcomes=[
        # 1 份历史封顶 + 1 份本轮抽取失败（评审给出的混合形态）
        lambda s: _ok(s, total=2, completed=0, generated=0, failed=1,
                      remaining=0, permanently_failed=1,
                      gaps=["摘要生成失败（已封顶）", "报告下载或章节抽取失败"]),
    ])
    assert job["failed_count"] == 1
    assert job["digests_blocked"] == 1  # 混合形态下不得漏计
    failed_row = job["results"][0]
    assert failed_row["status"] == "failed"
    assert failed_row["blocked"] == 1  # 结果行同样携带明细


def test_capped_symbols_do_not_trip_the_early_stop(db, monkeypatch):
    """[回归锁] 全封顶标的记失败但**不计入连败早停**。

    封顶是零成本的历史结果，说明不了本轮环境的健康度——前三只恰好全封顶就
    终止整批，会跳过后面所有仍可正常回填的持仓。早停只看本轮真实尝试的失败。
    """
    for index in range(4):
        _hold(db, f"60000{index}", "A股")
    outcomes = (
        # 前三只：全封顶（历史结果，零尝试）
        [lambda s: _ok(s, total=4, completed=0, generated=0, failed=0,
                       remaining=0, permanently_failed=4)] * 3
        # 第四只：正常回填成功——必须被执行到
        + [lambda s: _ok(s, total=4, completed=4, generated=4, remaining=0)]
    )
    calls_seen: list = []

    def fake_ensure(db_, symbol, market, *, max_new):
        calls_seen.append(symbol)
        return outcomes[len(calls_seen) - 1](symbol)

    monkeypatch.setattr(batch, "ensure_report_digests", fake_ensure)
    monkeypatch.setattr(batch.settings, "security_analysis_batch_pause_seconds", 0)
    job = batch.start_digest_batch_job(db, 1)
    batch.run_digest_batch_job(job["id"])
    final = batch.get_digest_batch_job(job["id"], 1)

    assert len(calls_seen) == 4  # 第四只被执行，没有被前三只的封顶挡住
    assert final["status"] == "succeeded"
    assert final["failed_count"] == 3  # 封顶标的仍如实记失败
    assert final["success_count"] == 1
    assert final["digests_generated"] == 4
    assert final["digests_blocked"] == 12


def test_real_attempt_failures_still_stop_early_after_capped_symbols(db, monkeypatch):
    """对照组：封顶不算连败，但本轮真实尝试的连续失败仍要早停——
    两只封顶夹在中间也不得打断真实失败的连击计数（它们不清零）。"""
    for index in range(6):
        _hold(db, f"60000{index}", "A股")
    capped = lambda s: _ok(s, total=4, completed=0, generated=0, failed=0,  # noqa: E731
                           remaining=0, permanently_failed=4)
    real_fail = lambda s: _ok(s, total=4, completed=0, generated=0, failed=4,  # noqa: E731
                              remaining=0)
    outcomes = [real_fail, capped, real_fail, capped, real_fail]
    calls_seen: list = []

    def fake_ensure(db_, symbol, market, *, max_new):
        calls_seen.append(symbol)
        return outcomes[min(len(calls_seen) - 1, len(outcomes) - 1)](symbol)

    monkeypatch.setattr(batch, "ensure_report_digests", fake_ensure)
    monkeypatch.setattr(batch.settings, "security_analysis_batch_pause_seconds", 0)
    job = batch.start_digest_batch_job(db, 1)
    batch.run_digest_batch_job(job["id"])
    final = batch.get_digest_batch_job(job["id"], 1)

    assert final["status"] == "failed"  # 三次真实失败触发早停
    assert "连续" in (final.get("abort_reason") or "")
    assert len(calls_seen) == 5  # 真实失败 ×3 + 封顶 ×2，第六只未开始


def test_completed_keys_resume_without_rework(db, monkeypatch):
    _hold(db, "600036", "A股")
    _hold(db, "600000", "A股")

    calls: list = []

    def fake_ensure(db_, symbol, market, *, max_new):
        calls.append(symbol)
        return _ok(symbol)

    monkeypatch.setattr(batch, "ensure_report_digests", fake_ensure)
    monkeypatch.setattr(batch.settings, "security_analysis_batch_pause_seconds", 0)
    job = batch.start_digest_batch_job(db, 1)
    # 模拟第一次执行做完第一只后被接管：手工写入 completed_keys
    from app.services.background_job_store import claim_job, update_job

    claimed = claim_job(job["id"], batch.JOB_TYPE)
    update_job(job["id"], batch.JOB_TYPE, data_updates={
        "completed_keys": ["A股|600000"], "completed": 1, "success_count": 1,
    })
    claimed["data"]["completed_keys"] = ["A股|600000"]
    claimed["data"]["success_count"] = 1
    batch.execute_digest_batch_job(claimed)

    assert calls == ["600036"]  # 已完成的 600000 不重跑
    final = batch.get_digest_batch_job(job["id"], 1)
    assert final["status"] == "succeeded"
    assert final["completed"] == 2


def test_cancel_stops_at_symbol_boundary(db, monkeypatch):
    _hold(db, "600036", "A股")
    _hold(db, "600000", "A股")
    job_holder: dict = {}

    def cancel_after_first(n):
        if n == 1:
            batch.request_digest_batch_cancel(job_holder["id"], 1)

    calls: list = []

    def fake_ensure(db_, symbol, market, *, max_new):
        calls.append(symbol)
        cancel_after_first(len(calls))
        return _ok(symbol)

    monkeypatch.setattr(batch, "ensure_report_digests", fake_ensure)
    monkeypatch.setattr(batch.settings, "security_analysis_batch_pause_seconds", 0)
    job = batch.start_digest_batch_job(db, 1)
    job_holder["id"] = job["id"]
    batch.run_digest_batch_job(job["id"])

    final = batch.get_digest_batch_job(job["id"], 1)
    assert final["status"] == "interrupted"
    assert final["cancelled"] is True
    assert len(calls) == 1  # 第二只未开始


def test_job_is_user_scoped(db, monkeypatch):
    _hold(db, "600036", "A股", user_id=1)
    job, _ = _run(db, monkeypatch, user_id=1)
    assert batch.get_digest_batch_job(job["id"], user_id=2) is None


# ---------------------------------------------------------------------------
# 冷启动 runner 注册
# ---------------------------------------------------------------------------


def test_runner_is_registered_on_fresh_app_import():
    """[回归锁] fresh 进程只导入 app.main、不访问任何 digest 路由时，
    runner 必须已注册——worker 只认领 _runners 里有的 job_type，懒加载的
    模块在冷启动时不会被导入，重启后 queued/租约过期的任务永远无人接管。

    必须用子进程：本测试进程早已 import 过这些模块，进程内断言恒真。
    """
    import os
    import subprocess
    import sys

    code = (
        "import app.main; from app.services.job_worker import _runners; "
        "print(','.join(sorted(_runners)))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=120,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env={**os.environ},
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    registered = set(proc.stdout.strip().split(","))
    assert "report_digest_batch" in registered
    assert "report_digest_backfill" in registered  # 单标的回填同病，一并锁住


# ---------------------------------------------------------------------------
# 互斥与 API
# ---------------------------------------------------------------------------


def test_exclusive_with_other_analysis_jobs(db, monkeypatch):
    """互斥在 API 层通过 ensure_no_conflicting_analysis_job 生效（start_* 本身
    不查——与批量分析同构）。断言：回填活跃时其他分析类入口会被拦，反向亦然。"""
    from app.services.security_analysis_batch_jobs import (
        AnalysisBusyError,
        ensure_no_conflicting_analysis_job,
    )

    _hold(db, "600036", "A股")
    batch.start_digest_batch_job(db, 1)  # 活跃的批量回填
    for caller in ("security_analysis_batch", "security_analysis",
                   "report_digest_backfill"):
        with pytest.raises(AnalysisBusyError, match="批量财报摘要回填"):
            ensure_no_conflicting_analysis_job(1, caller)
    # 自己不拦自己（重复点按钮命中 create_or_get_active_job 的幂等返回）
    ensure_no_conflicting_analysis_job(1, batch.JOB_TYPE)


@pytest.mark.anyio
async def test_digest_backfill_api_flow(db, monkeypatch):
    user = db.query(User).filter(User.username == "demo").first()
    user.hashed_password = get_password_hash("digest-batch-password")
    db.commit()
    _hold(db, "600036", "A股", user_id=user.id)

    monkeypatch.setattr(
        batch, "ensure_report_digests",
        lambda db_, symbol, market, *, max_new: _ok(symbol),
    )
    monkeypatch.setattr(batch.settings, "security_analysis_batch_pause_seconds", 0)
    from app.services.llm_client import is_llm_configured  # noqa: F401
    import app.api.security_profiles as api_module

    monkeypatch.setattr(api_module, "is_llm_configured", lambda: True)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/api/auth/token",
            json={"username": "demo", "password": "digest-batch-password"},
        )
        auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

        preview = (await client.get(
            "/api/securities/digest-backfill-preview", headers=auth
        )).json()
        assert preview["targets_total"] == 1
        assert preview["targets_without_digest"] == 1

        started = (await client.post(
            "/api/securities/digest-backfill-jobs", headers=auth
        )).json()
        assert started["type"] == batch.JOB_TYPE

        # BackgroundTasks 在响应后执行；ASGITransport 会等它跑完
        polled = (await client.get(
            f"/api/securities/digest-backfill-jobs/{started['id']}", headers=auth
        )).json()
        assert polled["status"] == "succeeded"
        assert polled["digests_generated"] == 4


def test_losing_ownership_mid_loop_stops_the_batch_immediately(db, monkeypatch):
    """[回归锁] 失权（被接管）后必须立刻停手，不得继续回填剩余标的。

    本模块的 progress() 调用点在 try **之外**（与批量分析相反），#134 把
    progress 收敛到 job_runtime 时两边的调用位置都必须原样保住——统一成同一
    个位置就是行为变更。僵尸不停手 = 对剩余标的重复下载年报 PDF 并烧 LLM token。
    """
    from app.services import job_runtime

    for suffix in range(3):
        _hold(db, f"60000{suffix}", "A股")

    # 瞬时失权：只让第一只之后的那次回写返回 None，之后恢复。这样"停手"只能
    # 由哨兵造成，而不是因为后续每次回写都失败。
    state = {"armed": False, "tripped": False}
    original = job_runtime.set_job_progress

    def spy(job_id, job_type, **kwargs):
        if state["armed"] and not state["tripped"]:
            state["tripped"] = True
            return None
        return original(job_id, job_type, **kwargs)

    monkeypatch.setattr(job_runtime, "set_job_progress", spy)

    calls: list = []

    def fake_ensure(db_, symbol, market, *, max_new):
        calls.append(symbol)
        state["armed"] = True  # 本只跑完后的那次回写即失权
        return _ok(symbol)

    monkeypatch.setattr(batch, "ensure_report_digests", fake_ensure)
    monkeypatch.setattr(batch.settings, "security_analysis_batch_pause_seconds", 0)

    job = batch.start_digest_batch_job(db, 1)
    batch.run_digest_batch_job(job["id"])  # 安静退出，不得抛出

    assert len(calls) == 1, f"失权后仍继续回填了剩余标的：{calls}"
    stored = db.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()
    db.refresh(stored)
    assert stored.status != "failed", "失权不是失败，僵尸不得把 job 标成 failed"
