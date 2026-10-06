"""LLM 管线杂项整改的回归（issue #145）。

金样最要紧：digest prompt 首段由共享 guardrail 组装后必须与历史文本逐字节
一致——变了而不 bump DIGEST_PROMPT_VERSION，就是全部摘要缓存被静默视为
新鲜、修复被自己的缓存遮住（CLAUDE.md 双版本指纹一节）。
"""

import threading
import time


from app.database import SessionLocal
from app.services import business_profile_service
from app.services import security_analysis_jobs as saj
from app.services.business_profile_prompts import PROFILE_PROMPT_VERSION
from app.services.report_digest_prompts import (
    COMPACT_DIGEST_SYSTEM_PROMPT,
    DIGEST_SYSTEM_PROMPT,
)
from app.services.security_analysis_batch_jobs import ensure_no_conflicting_analysis_job

# 历史首段原文（DIGEST_PROMPT_VERSION=2 时代的字节）。共享 guardrail 组装
# 必须逐字节还原它；故意改措辞时必须 bump 版本再更新此金样。
_DIGEST_FIRST_PARAGRAPH = (
    "你是财报章节摘要助手。只依据用户提供的报告原文节选做摘要，"
    "禁止引入任何对该公司的先验知识；"
    '原文未提及的内容一律写"原文未提及"，不得推测补全。\n\n'
)


def test_digest_prompts_byte_identical_to_pre_guardrail_era():
    """[金样] 共享化不 bump DIGEST_PROMPT_VERSION 的前提：首段字节不变。"""
    assert DIGEST_SYSTEM_PROMPT.startswith(_DIGEST_FIRST_PARAGRAPH)
    assert COMPACT_DIGEST_SYSTEM_PROMPT.startswith(_DIGEST_FIRST_PARAGRAPH)


def test_business_profile_cache_requires_prompt_version(monkeypatch):
    """缓存命中要求输入指纹与 prompt 版本同时匹配（修复前只比输入指纹）。

    构造：库中缓存行 input_fingerprint 与当前输入一致但 prompt_version 缺失
    （按约定当 v1）→ 必须重新生成而非命中；写回后带当前版本 → 第二次命中。
    """
    db = SessionLocal()
    symbol, market = "TESTPPV", "A股"
    payload_input = {
        "symbol": symbol,
        "market": market,
        "report_digest_slices": [{"end_date": "20251231", "业务分部占比": "x"}],
        "business_section_excerpt": "业务概要",
        "financials": {"income": [], "fina_indicator": []},
        "source_end_date": "20251231",
    }
    monkeypatch.setattr(
        business_profile_service, "build_business_profile_input", lambda *a, **k: payload_input
    )
    calls = []

    def fake_completion(messages, **kwargs):
        calls.append(1)
        profile_json = (
            '{"商业模式": "测试", "行业与竞争": "测试", "供应商集中度": "未披露",'
            ' "客户集中度": "未披露",'
            ' "业务分部": [{"名称": "a", "收入占比": "50%", "毛利率": "10%", "趋势": "平稳"},'
            ' {"名称": "b", "收入占比": "50%", "毛利率": "10%", "趋势": "平稳"}],'
            ' "上游依赖": [{"要素": "x", "影响": "y"}],'
            ' "下游需求": [{"客群或场景": "x", "需求驱动": "y"}],'
            ' "估值观察因子": [{"因子": "a", "方向": "上游成本", "传导": "→毛利率"},'
            ' {"因子": "b", "方向": "下游需求", "传导": "→收入增速"}]}'
        )
        return {"content": profile_json, "model": "test"}

    monkeypatch.setattr(business_profile_service, "chat_completion", fake_completion)
    try:
        fingerprint = business_profile_service.input_fingerprint(payload_input)
        # 预置"旧时代"缓存行：指纹匹配但无 prompt_version（当 v1）
        business_profile_service.upsert_profile_row(
            db,
            symbol,
            market,
            "business_profile",
            "current",
            {
                "status": "ok",
                "profile": {"商业模式": "旧缓存"},
                "input_fingerprint": fingerprint,
            },
        )
        db.commit()

        profile = business_profile_service.ensure_business_profile(db, symbol, market)
        assert calls, "prompt 版本缺失（=v1）≠ 当前版本，必须重新生成而不是命中旧缓存"
        assert profile["商业模式"] == "测试"

        calls.clear()
        again = business_profile_service.ensure_business_profile(db, symbol, market)
        assert not calls, "指纹与版本都匹配时应命中缓存"
        assert again["商业模式"] == "测试"
        assert PROFILE_PROMPT_VERSION != "1"  # 版本确实前进过，上面的失配才有意义
    finally:
        from app.models.security_profile import SecurityProfileData

        db.rollback()
        db.query(SecurityProfileData).filter(SecurityProfileData.symbol == symbol).delete(
            synchronize_session=False
        )
        db.commit()
        db.close()


def test_conflicting_job_precheck_serializes_on_advisory_lock():
    """预检在顾问锁上串行化：第二个并发请求要等第一个事务结束才能通过。

    修复前 check-then-create 跨 session，两个不同类型的请求可同时通过预检
    各建一个 job（双倍烧钱）。修复后第二个请求在锁上排队——本用例对旧实现红
    （第二个调用不会被阻塞）。
    """
    session_a = SessionLocal()
    session_b = SessionLocal()
    order = []
    try:
        ensure_no_conflicting_analysis_job(session_a, 1, "security_analysis")
        order.append("a-passed")

        def second():
            ensure_no_conflicting_analysis_job(session_b, 1, "report_digest_backfill")
            order.append("b-passed")

        thread = threading.Thread(target=second)
        thread.start()
        time.sleep(0.5)
        # A 的事务还开着 → B 必须还堵在锁上
        assert order == ["a-passed"], "第二个并发预检应等待第一个事务结束（顾问锁未生效）"
        session_a.rollback()  # 结束 A 的事务，释放 xact 锁
        thread.join(timeout=5)
        assert order == ["a-passed", "b-passed"]
    finally:
        session_a.rollback()
        session_b.rollback()
        session_a.close()
        session_b.close()


def test_analysis_input_shrinks_twice_and_rechecks(monkeypatch):
    """一级收缩后复测；仍超预算才截 events/peers（修复前一次收缩不复检）。"""
    big = "x" * 200
    canned_profile = {"datasets": {"income": [{"f": big} for _ in range(50)]}}
    monkeypatch.setattr(saj, "load_symbol_profile", lambda *a, **k: canned_profile)
    monkeypatch.setattr(
        saj, "load_security_events_for", lambda *a, **k: [{"e": big} for _ in range(40)]
    )
    monkeypatch.setattr(saj, "_compact_profile", lambda datasets: datasets)

    from app.services import earnings_quality, report_digest_service

    monkeypatch.setattr(report_digest_service, "load_report_digests", lambda *a, **k: [])
    monkeypatch.setattr(report_digest_service, "serialize_digest_for_analysis", lambda *a, **k: [])
    monkeypatch.setattr(
        business_profile_service,
        "load_business_profile",
        lambda *a, **k: {
            "profile": None,
            "industry": "测试业",
            "peers": [{"symbol": f"P{i}", "name": big} for i in range(20)],
        },
    )
    from app.services import security_profile_service

    # graham 取数走独立的年度行专取口径（真实 DB 查询），单测桩掉
    monkeypatch.setattr(security_profile_service, "compute_graham_for", lambda *a, **k: None)
    monkeypatch.setattr(
        security_profile_service, "load_annual_statement_datasets", lambda *a, **k: None
    )
    monkeypatch.setattr(
        earnings_quality,
        "market_statements",
        lambda *a, **k: {"income": [], "balancesheet": [], "cashflow": [], "fina_indicator": []},
    )
    monkeypatch.setattr(earnings_quality, "compute_earnings_quality", lambda *a, **k: {})
    from app.services import announcement_service, announcement_sync

    monkeypatch.setattr(
        announcement_sync,
        "sync_status",
        lambda *a, **k: {"status": "synced", "last_synced": None, "reason": None},
    )
    monkeypatch.setattr(
        announcement_service,
        "load_groups",
        lambda *a, **k: [
            {
                "ann_date": "2026-09-01",
                "category_label": "融资",
                "importance": "major",
                "title": big,
                "document_count": 1,
            }
            for _ in range(30)
        ],
    )
    monkeypatch.setattr(saj, "CHAR_BUDGET", 3_000)

    payload = saj.build_analysis_input(None, "TEST", "A股", digest_gaps=[])

    assert len(payload["events"]) == saj.EVENTS_SHRUNK_CAP, "二级收缩应截 events"
    assert len(payload["peers"]["list"]) == saj.PEERS_SHRUNK_CAP, "二级收缩应截 peers"
    assert len(payload["announcements"]) == saj.ANNOUNCEMENTS_SHRUNK_CAP, "二级收缩应截公告"
