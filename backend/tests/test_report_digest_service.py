"""财报摘要编排：目标规划、缓存命中、attempts 封顶、max_new 护栏、失败降级、回填 job。

全部 mock fetch/download/LLM，不打真实网络。
"""


from datetime import datetime, timedelta, timezone

import pytest

from app.database import SessionLocal
from app.models.background_job import BackgroundJob
from app.models.security_profile import SecurityProfileData
from app.services import report_digest_jobs as backfill_jobs
from app.services import report_digest_service as svc
from app.services.llm_client import LLMClientError
from app.services.report_digest_prompts import parse_digest_output

from .helpers import reset_tables

VALID_DIGEST = (
    '{"经营回顾":"稳健","业务分部占比":"零售 57%","上下游与产业链":"前五客户占比 3%",'
    '"主营收入结构":"净利息为主","成本与费用":"费用率下降","一次性项目":"无重大",'
    '"会计信号":"原文未见明显信号","风险要点":"信用风险","展望":"稳中求进",'
    '"关键数字":["营收 3391 亿元（2025 年度）"]}'
)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        reset_tables(session, [SecurityProfileData])
        session.query(BackgroundJob).filter(
            BackgroundJob.job_type == backfill_jobs.JOB_TYPE
        ).delete()
        session.commit()
        yield session
        session.rollback()
        reset_tables(session, [SecurityProfileData])
        session.query(BackgroundJob).filter(
            BackgroundJob.job_type == backfill_jobs.JOB_TYPE
        ).delete()
        session.commit()
    finally:
        session.close()


def _targets(years):
    return [
        {
            "period_key": f"{year}1231|annual",
            "report_type": "annual",
            "end_date": f"{year}1231",
            "title": f"{year}年年度报告",
            "ann_date": f"{year + 1}-03-28",
            "url": f"http://static.cninfo.com.cn/final/{year}.PDF",
        }
        for year in years
    ]


def _patch_pipeline(monkeypatch, *, years, llm_content=VALID_DIGEST):
    monkeypatch.setattr(
        svc, "cached_report_targets_detailed",
        lambda db, symbol, market, **kw: {"targets": _targets(years), "complete": True},
    )
    monkeypatch.setattr(
        svc, "_ensure_section",
        lambda db, symbol, market, target: {"business": "业务概要", "mdna": "经营分析"},
    )
    calls = []

    def fake_llm(messages, **kw):
        calls.append(messages)
        return {
            "content": llm_content, "model": "deepseek-v4-pro",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }

    monkeypatch.setattr(svc, "chat_completion", fake_llm)
    return calls


# ---------------------------------------------------------------------------
# 目标规划
# ---------------------------------------------------------------------------


def test_plan_targets_prefers_revised_and_filters_noise(monkeypatch):
    announcements = {
        "annual": [
            {"title": "2025年年度报告", "ann_date": "2026-03-28", "url": "u1", "adjunct_size_kb": 1},
            # 修订版（公告日更新）应胜出
            {"title": "2025年年度报告（修订版）", "ann_date": "2026-04-20", "url": "u2", "adjunct_size_kb": 1},
            {"title": "2025年年度报告摘要", "ann_date": "2026-03-28", "url": "u3", "adjunct_size_kb": 1},
            {"title": "2024年年度报告（英文版）", "ann_date": "2025-04-01", "url": "u4", "adjunct_size_kb": 1},
            {"title": "2024年年度报告", "ann_date": "2025-03-28", "url": "u5", "adjunct_size_kb": 1},
        ],
        "semi": [
            {"title": "2026年半年度报告", "ann_date": "2026-08-20", "url": "u6", "adjunct_size_kb": 1},
        ],
    }
    monkeypatch.setattr(
        svc, "cninfo_search_reports",
        lambda symbol, report_type, se_date: announcements[report_type],
    )
    targets = svc.plan_report_targets("600036", "A股")

    by_key = {t["period_key"]: t for t in targets}
    assert by_key["20251231|annual"]["url"] == "u2"  # 修订版
    assert by_key["20241231|annual"]["url"] == "u5"  # 英文版被过滤
    assert "20260630|semi" in by_key
    # 未支持的市场不打任何网络请求（港股已于 PR-F 接入披露易）
    assert svc.plan_report_targets("600519", "沪深B股") == []


# ---------------------------------------------------------------------------
# digest 编排
# ---------------------------------------------------------------------------


def test_digest_generated_and_cached(db, monkeypatch):
    calls = _patch_pipeline(monkeypatch, years=[2025, 2024])
    result = svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert result == {
        "total": 2, "completed": 2, "attempted": 2, "generated": 2, "failed": 0,
        "permanently_failed": 0, "plan_incomplete": False, "remaining": 0,
        "pending_periods": [], "gaps": [], "fatal": None,
    }
    assert len(calls) == 2

    # 二次调用：全部缓存命中，零 LLM 调用
    result = svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert result["generated"] == 0
    assert result["completed"] == 2
    assert len(calls) == 2

    digests = svc.load_report_digests(db, "600036", "A股")
    assert [d["end_date"] for d in digests] == ["20251231", "20241231"]
    assert digests[0]["digest"]["业务分部占比"] == "零售 57%"


def test_max_new_guardrail_reports_remaining(db, monkeypatch):
    calls = _patch_pipeline(monkeypatch, years=[2025, 2024, 2023, 2022, 2021])
    result = svc.ensure_report_digests(db, "600036", "A股", max_new=2)
    assert result["generated"] == 2
    assert result["remaining"] == 3
    assert len(calls) == 2

    # 续跑补齐
    result = svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert result["generated"] == 3
    assert result["completed"] == 5
    assert result["remaining"] == 0


def test_invalid_llm_output_counts_attempts_and_caps(db, monkeypatch):
    _patch_pipeline(monkeypatch, years=[2025], llm_content="不是JSON")
    for _ in range(2):
        result = svc.ensure_report_digests(db, "600036", "A股", max_new=4)
        assert "摘要生成失败" in result["gaps"][0]
    row = db.query(SecurityProfileData).filter_by(dataset="report_digest").one()
    assert row.payload["status"] == "failed"
    assert row.payload["attempts"] == 2

    # attempts 封顶后不再尝试（LLM 不再被调用）
    calls = _patch_pipeline(monkeypatch, years=[2025])
    result = svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert calls == []
    assert "已封顶" in result["gaps"][0]


def test_transient_llm_failure_does_not_count_attempts(db, monkeypatch):
    def fail_llm(messages, **kw):
        raise LLMClientError("上游超时", status_code=None)

    monkeypatch.setattr(
        svc, "cached_report_targets_detailed", lambda db, s, m, **kw: {"targets": _targets([2025]), "complete": True}
    )
    monkeypatch.setattr(
        svc, "_ensure_section", lambda db, s, m, t: {"mdna": "内容"}
    )
    monkeypatch.setattr(svc, "chat_completion", fail_llm)
    svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    row = db.query(SecurityProfileData).filter_by(dataset="report_digest").one()
    assert row.payload["attempts"] == 0  # 瞬时失败不计，下次可重试


@pytest.mark.parametrize(
    ("status_code", "kind"),
    [(401, "llm_auth"), (402, "llm_auth"), (403, "llm_auth"), (429, "llm_rate_limited")],
)
def test_llm_auth_and_rate_errors_surface_structured_fatal(
    db, monkeypatch, status_code, kind
):
    """[回归锁] 无效 Key/欠费/限流必须以**结构化 fatal** 上报，并停止本标的。

    此前它们只变成一句中文 gap：批量调用方看不出区别，把整只标的记成成功，
    36 只全跑一遍空转，最后 UI 还提示完成。
    """
    monkeypatch.setattr(
        svc, "cached_report_targets_detailed", lambda db, s, m, **kw: {"targets": _targets([2025, 2024]), "complete": True}
    )
    monkeypatch.setattr(svc, "_ensure_section", lambda db, s, m, t: {"mdna": "内容"})
    monkeypatch.setattr(svc, "chat_completion", lambda messages, **kw: (
        (_ for _ in ()).throw(LLMClientError("boom", status_code=status_code))
    ))
    result = svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert result["fatal"] is not None
    assert result["fatal"]["kind"] == kind
    assert result["generated"] == 0
    assert result["failed"] == 1  # 第一份就停，不再逐份烧
    assert str(status_code) in result["fatal"]["message"]


def test_llm_not_configured_surfaces_structured_fatal(db, monkeypatch):
    from app.services.llm_client import LLMNotConfiguredError

    monkeypatch.setattr(
        svc, "cached_report_targets_detailed", lambda db, s, m, **kw: {"targets": _targets([2025]), "complete": True}
    )
    monkeypatch.setattr(svc, "_ensure_section", lambda db, s, m, t: {"mdna": "内容"})
    monkeypatch.setattr(svc, "chat_completion", lambda messages, **kw: (
        (_ for _ in ()).throw(LLMNotConfiguredError("未配置 LLM API Key"))
    ))
    result = svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert result["fatal"]["kind"] == "llm_not_configured"


def test_failed_attempts_consume_the_per_round_budget(db, monkeypatch):
    """[回归锁] 成本护栏按**实际尝试数**扣减，不是按成功数。

    按 generated 扣的话，解析失败/瞬时 5xx 已经发出（并可能计费）的 LLM 请求
    不消耗预算，循环会把十年报告全试一遍——"每轮最多 4 份"变成最多 10 次调用，
    批量层再乘上连续 3 只的早停线，约 12 次的失败护栏膨胀到约 30 次。
    """
    llm_calls = []
    _patch_pipeline(monkeypatch, years=list(range(2025, 2015, -1)),
                    llm_content="不是JSON")
    original = svc.chat_completion

    def counting_llm(messages, **kw):
        llm_calls.append(1)
        return original(messages, **kw)

    monkeypatch.setattr(svc, "chat_completion", counting_llm)
    result = svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert len(llm_calls) == 4  # 全部失败也只调 4 次
    assert result["attempted"] == 4
    assert result["failed"] == 4
    assert result["generated"] == 0
    assert result["fatal"] is None
    assert result["remaining"] == 6  # 其余 6 期进 remaining/pending
    assert len(result["pending_periods"]) == 6


def test_section_failures_count_failed_and_consume_budget(db, monkeypatch):
    """[回归锁] 下载/章节抽取全部失败必须计 failed 并消耗预算。

    此前它们只追加 gap：数据源或解析器让某标的所有报告都倒在 section 段时，
    结果是 generated=0, failed=0——批量层当成"全缓存命中/无年报"记成功，
    谎报路径原样存在；且不扣预算，十年报告会全试一遍（每份都是 PDF 下载）。
    """
    monkeypatch.setattr(
        svc, "cached_report_targets_detailed",
        lambda db, s, m, **kw: {
            "targets": _targets(list(range(2025, 2015, -1))), "complete": True,
        },
    )
    section_calls = []
    monkeypatch.setattr(
        svc, "_ensure_section",
        lambda db, s, m, t: section_calls.append(1) or None,
    )
    result = svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert len(section_calls) == 4  # 预算同样限制 section 尝试
    assert result["failed"] == 4
    assert result["generated"] == 0
    assert result["remaining"] == 6


def test_permanently_capped_sections_do_not_consume_budget(db, monkeypatch):
    """历史封顶失败是零成本跳过，不是本轮的尝试：不扣预算、不计 failed——
    否则一个有 4 期坏 PDF 的标的每轮预算都被已知无法修复的期数吃光。"""
    targets = _targets([2025, 2024, 2023])
    monkeypatch.setattr(
        svc, "cached_report_targets_detailed", lambda db, s, m, **kw: {"targets": targets, "complete": True}
    )
    # 前两期：section 封顶失败行（同指纹同版本 attempts 满）
    for target in targets[:2]:
        _write_row(db, "report_section", target["period_key"], {
            "source_fingerprint": svc.source_fingerprint(target),
            "extractor_version": svc.SECTION_EXTRACTOR_VERSION,
            "extract_status": "failed", "attempts": svc.MAX_ATTEMPTS,
            "sections": {},
        })
    monkeypatch.setattr(
        svc, "_ensure_section", lambda db, s, m, t: {"mdna": "内容"}
    )
    monkeypatch.setattr(svc, "chat_completion", lambda messages, **kw: {
        "content": VALID_DIGEST, "model": "m", "usage": {},
    })
    result = svc.ensure_report_digests(db, "600036", "A股", max_new=1)
    assert result["attempted"] == 1  # 只有 2023 那期算尝试
    assert result["generated"] == 1
    assert result["failed"] == 0
    assert sum("已封顶" in gap for gap in result["gaps"]) == 2


def test_incomplete_plan_is_flagged_not_mistaken_for_no_reports(db, monkeypatch):
    """[回归锁] "清单检索失败"与"清单确实为空"必须分开。

    只看 targets 的话两者都是空列表：源站整体故障时每个标的都返回
    total=0/failed=0/fatal=None，批量层全部记成功、前端绿色"生成 0 份"。
    """
    monkeypatch.setattr(
        svc, "cached_report_targets_detailed",
        lambda db, s, m, **kw: {"targets": [], "complete": False},  # partial-empty
    )
    result = svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert result["plan_incomplete"] is True
    assert any("不可信" in gap for gap in result["gaps"])

    # 对照组：complete + empty 才是真实的"该标的无年报"
    monkeypatch.setattr(
        svc, "cached_report_targets_detailed",
        lambda db, s, m, **kw: {"targets": [], "complete": True},
    )
    result = svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert result["plan_incomplete"] is False
    assert result["gaps"] == []


def test_cached_partial_plan_stays_incomplete(db, monkeypatch):
    """缓存命中 partial 行时完整性不得被洗白：短 TTL 只保证尽快重试，
    不改变这份清单当下不可信的性质。"""
    from app.services.security_profile_service import upsert_profile_row
    from datetime import datetime, timezone

    upsert_profile_row(db, "600036", "A股", "report_target_plan", "current", {
        "status": "partial", "failed_kinds": ["annual"],
        "planned_at": datetime.now(timezone.utc).isoformat(),
        "targets": [],
    })
    db.commit()
    planned = svc.cached_report_targets_detailed(db, "600036", "A股")
    assert planned["complete"] is False


def test_capped_reports_are_counted_as_permanently_failed(db, monkeypatch):
    """[回归锁] 封顶行不消耗预算（成本维度），但必须计入 permanently_failed
    （结果维度）——两条封顶路径（摘要封顶 / section 封顶）都要覆盖。

    不单列的话"全部封顶"返回 generated=0/failed=0，批量层记成功，前端弹绿色
    "新生成 0 份"，用户看不到其实所有可回填报告都永久失败了。
    """
    targets = _targets([2025, 2024])
    monkeypatch.setattr(
        svc, "cached_report_targets_detailed", lambda db, s, m, **kw: {"targets": targets, "complete": True}
    )
    # 2025：摘要封顶；2024：section 封顶
    _write_row(db, "report_digest", targets[0]["period_key"], {
        "status": "failed", "attempts": svc.MAX_ATTEMPTS,
        "source_fingerprint": svc.source_fingerprint(targets[0]),
        "extractor_version": svc.SECTION_EXTRACTOR_VERSION,
        "prompt_version": svc.DIGEST_PROMPT_VERSION,
        "digest_tier": "A",
    })
    _write_row(db, "report_section", targets[1]["period_key"], {
        "source_fingerprint": svc.source_fingerprint(targets[1]),
        "extractor_version": svc.SECTION_EXTRACTOR_VERSION,
        "extract_status": "failed", "attempts": svc.MAX_ATTEMPTS,
        "sections": {},
    })
    monkeypatch.setattr(svc, "_ensure_section", lambda db, s, m, t: (
        (_ for _ in ()).throw(AssertionError("封顶行不该触发抽取"))
    ))
    result = svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert result["permanently_failed"] == 2
    assert result["attempted"] == 0  # 成本维度：零消耗
    assert result["failed"] == 0  # 本轮没有新尝试
    assert result["generated"] == 0
    assert result["completed"] == 0


def test_ordinary_generation_failure_counts_failed_without_fatal(db, monkeypatch):
    """输出不合约定是**本份**失败：计 failed、不设 fatal（换个标的可能就好了）。"""
    _patch_pipeline(monkeypatch, years=[2025, 2024], llm_content="不是JSON")
    result = svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert result["fatal"] is None
    assert result["failed"] == 2
    assert result["generated"] == 0


def test_section_failure_reports_gap(db, monkeypatch):
    monkeypatch.setattr(
        svc, "cached_report_targets_detailed", lambda db, s, m, **kw: {"targets": _targets([2025]), "complete": True}
    )
    monkeypatch.setattr(svc, "_ensure_section", lambda db, s, m, t: None)
    result = svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert "下载或章节抽取失败" in result["gaps"][0]
    assert result["completed"] == 0


def test_section_download_failure_persists_failed_row(db, monkeypatch):
    monkeypatch.setattr(
        svc, "download_report_pdf",
        lambda url, **kw: (_ for _ in ()).throw(RuntimeError("下载超时")),
    )
    target = _targets([2025])[0]
    sections = svc._ensure_section(db, "600036", "A股", target)
    assert sections is None
    row = db.query(SecurityProfileData).filter_by(dataset="report_section").one()
    assert row.payload["extract_status"] == "failed"
    assert row.payload["attempts"] == 1


# ---------------------------------------------------------------------------
# 版本指纹：抽取逻辑/prompt 变化必须使缓存失效
# ---------------------------------------------------------------------------


def _write_row(db, dataset, period_key, payload):
    from app.services.security_profile_service import upsert_profile_row

    upsert_profile_row(db, "600036", "A股", dataset, period_key, payload)
    db.commit()


def test_stale_extractor_version_forces_re_extraction(db, monkeypatch):
    """[回归锁] 只按源指纹判缓存，会让修好的抽取器对存量数据完全无效。

    库里那 20 份把「公司简介和主要财务指标」当成业务概要的节选，报告本身没变
    （源指纹一致），不看抽取器版本就会永久命中——修复被自己的缓存遮住。
    """
    target = _targets([2025])[0]
    _write_row(db, "report_section", "20251231|annual", {
        "source_fingerprint": svc.source_fingerprint(target),
        "extractor_version": 1,  # 旧版本
        "extract_status": "ok",
        "sections": {"business": "股票简称 招商银行 注册地址 深圳市", "mdna": "旧内容"},
    })
    downloaded = []
    monkeypatch.setattr(svc, "download_report_pdf",
                        lambda url, **kw: downloaded.append(url) or b"%PDF-")
    monkeypatch.setattr(svc, "pages_to_text", lambda pages: "全文")
    monkeypatch.setattr(svc, "extract_cn_sections", lambda text: {})

    class _FakePdf:
        pages: list = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(svc.pdfplumber, "open", lambda stream: _FakePdf())

    svc._ensure_section(db, "600036", "A股", target)
    assert downloaded, "抽取器版本变化后必须重新下载抽取"

    # 版本一致时才命中缓存
    _write_row(db, "report_section", "20251231|annual", {
        "source_fingerprint": svc.source_fingerprint(target),
        "extractor_version": svc.SECTION_EXTRACTOR_VERSION,
        "extract_status": "ok",
        "sections": {"mdna": "新内容"},
    })
    downloaded.clear()
    sections = svc._ensure_section(db, "600036", "A股", target)
    assert sections == {"mdna": "新内容"}
    assert downloaded == []


@pytest.mark.parametrize("stale_field", ["extractor_version", "prompt_version"])
def test_stale_version_regenerates_digest(db, monkeypatch, stale_field):
    """抽取器或 prompt 任一版本变化都要重摘要（前者改的是输入，后者改的是产出）。"""
    calls = _patch_pipeline(monkeypatch, years=[2025])
    svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert len(calls) == 1

    row = db.query(SecurityProfileData).filter_by(dataset="report_digest").one()
    payload = dict(row.payload)
    payload[stale_field] = 0  # 模拟旧版本行
    _write_row(db, "report_digest", row.period_key, payload)

    result = svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert result["generated"] == 1, f"{stale_field} 过期时未重跑"
    assert len(calls) == 2


def test_legacy_rows_without_version_are_treated_as_v1(db, monkeypatch):
    """历史行没有版本字段——缺字段必须当作 v1（会被 bump 淘汰），不能当作最新。"""
    calls = _patch_pipeline(monkeypatch, years=[2025])
    _write_row(db, "report_digest", "20251231|annual", {
        "status": "ok",
        "source_fingerprint": svc.source_fingerprint(_targets([2025])[0]),
        "report_type": "annual", "end_date": "20251231",
        "digest": {"经营回顾": "旧"},
    })
    result = svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert result["generated"] == 1
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 分档 digest
# ---------------------------------------------------------------------------


def test_digest_tiers_follow_report_recency():
    from app.services.report_digest_prompts import assign_digest_tiers

    targets = _targets([2025, 2024, 2023, 2022, 2021, 2020, 2019]) + [{
        "period_key": "20250630|semi", "report_type": "semi", "end_date": "20250630",
        "title": "2025年半年度报告", "ann_date": "2025-08-28", "url": "u",
    }]
    tiers = assign_digest_tiers(targets)
    assert tiers["20251231|annual"] == "A"  # 最新年报
    assert tiers["20250630|semi"] == "A"  # 中报同属最贴近当下
    assert tiers["20241231|annual"] == "B"
    assert tiers["20211231|annual"] == "B"  # 第 5 期仍在 B
    assert tiers["20201231|annual"] == "C"
    assert tiers["20191231|annual"] == "C"


def test_tier_c_validates_only_compact_fields():
    """[回归锁] C 档若仍按全字段校验，每份都判确定性失败并烧 attempts，
    两次之后永久跳过——分档反而制造缺口。"""
    compact = (
        '{"主营收入结构":"结构","一次性项目":"无","会计信号":"原文未见明显信号",'
        '"关键数字":["营收 100 亿元（2019 年度）"]}'
    )
    digest = parse_digest_output(compact, tier="C")
    assert set(digest) == {"主营收入结构", "一次性项目", "会计信号", "关键数字"}

    with pytest.raises(ValueError, match="缺少字段"):
        parse_digest_output(compact, tier="B")


def test_tier_c_prompt_only_carries_mdna(db, monkeypatch):
    """C 档只送 mdna：旧年份的另外五个字段本来就被 serialize 丢掉，生成即浪费。"""
    from app.services.report_digest_prompts import COMPACT_DIGEST_FIELDS

    years = list(range(2025, 2015, -1))
    calls = _patch_pipeline(
        monkeypatch, years=years,
        llm_content=(
            '{"主营收入结构":"结构","一次性项目":"无","会计信号":"无",'
            '"关键数字":["营收 100 亿元"]}'
        ),
    )
    # A/B 档会因缺字段失败，这里只关心最旧那份走 C 档
    svc.ensure_report_digests(db, "600036", "A股", max_new=12)
    oldest = db.query(SecurityProfileData).filter_by(
        dataset="report_digest", period_key="20161231|annual"
    ).one()
    assert oldest.payload["digest_tier"] == "C"
    assert oldest.payload["status"] == "ok"
    assert set(oldest.payload["digest"]) == {*COMPACT_DIGEST_FIELDS, "关键数字"}
    user_content = calls[-1][1]["content"]
    assert "管理层讨论与分析" in user_content
    assert "公司业务概要" not in user_content


def test_tier_downgrade_does_not_burn_a_rerun(db, monkeypatch):
    """新年报加入后旧报告会 A→B、B→C。**降级不重跑**：已有的 A 档摘要本就比
    C 档更全，花钱重跑一次去换更少的字段是纯亏；C 档省的是尚未生成那些年份
    的钱。反过来（缓存档位比现在需要的更薄）必须重跑。"""
    years = [2025, 2024, 2023, 2022, 2021]
    calls = _patch_pipeline(monkeypatch, years=years)
    svc.ensure_report_digests(db, "600036", "A股", max_new=12)
    assert len(calls) == 5
    stored = {
        row.period_key: row.payload["digest_tier"]
        for row in db.query(SecurityProfileData).filter_by(dataset="report_digest")
    }
    assert stored["20251231|annual"] == "A"
    assert stored["20211231|annual"] == "B"

    # 加入一份更新的年报：全员下移一档，但都是降级 → 零重跑
    calls = _patch_pipeline(monkeypatch, years=[2026, *years])
    result = svc.ensure_report_digests(db, "600036", "A股", max_new=12)
    assert result["generated"] == 1  # 只有新的 2026 那份
    assert len(calls) == 1

    # 反向：把一份 C 档摘要塞进 A 档位置 → 必须重跑
    row = db.query(SecurityProfileData).filter_by(
        dataset="report_digest", period_key="20261231|annual"
    ).one()
    _write_row(db, "report_digest", row.period_key, {**row.payload, "digest_tier": "C"})
    assert svc.ensure_report_digests(db, "600036", "A股", max_new=12)["generated"] == 1


def test_stale_version_rows_are_hidden_from_analysis_and_progress(db, monkeypatch):
    """[回归锁] 一次回填最多重跑 4 份。读取路径只看 status 的话，其余旧版本的
    错误摘要仍是 ok——分析把新旧混在一起，进度还虚报已完成。"""
    calls = _patch_pipeline(monkeypatch, years=[2025, 2024, 2023])
    svc.ensure_report_digests(db, "600036", "A股", max_new=12)
    assert len(calls) == 3

    rows = db.query(SecurityProfileData).filter_by(dataset="report_digest").all()
    for row in rows[:2]:  # 两份标记成旧抽取器版本
        _write_row(db, "report_digest", row.period_key,
                   {**row.payload, "extractor_version": 1})

    assert [d["end_date"] for d in svc.load_report_digests(db, "600036", "A股")] == [
        rows[2].payload["end_date"]
    ]
    assert svc.digest_progress(db, "600036", "A股")["digested"] == 1


def test_pending_periods_reach_the_analysis_input_as_gaps(db, monkeypatch):
    """[回归锁] 版本升级后 10 份旧摘要、单轮只补 2 份：本轮 report_digests 只剩
    2 份，若 gaps 为空，模型会在不知道另外 8 个年份缺失的情况下写"跨年综述"。

    `remaining` 只是个计数，分析 job 只把 gaps 传进 LLM 输入——待补齐的报告期
    必须走 gaps 这条路。
    """
    from app.services import security_analysis_jobs as analysis_jobs

    years = list(range(2025, 2015, -1))
    _patch_pipeline(monkeypatch, years=years)
    svc.ensure_report_digests(db, "600036", "A股", max_new=12)  # 先补齐十份

    # 模拟一次版本升级：全部旧摘要失效
    for row in db.query(SecurityProfileData).filter_by(dataset="report_digest").all():
        _write_row(db, "report_digest", row.period_key,
                   {**row.payload, "extractor_version": 0})

    result = svc.ensure_report_digests(db, "600036", "A股", max_new=2)
    assert result["generated"] == 2
    assert result["remaining"] == 8
    assert len(result["pending_periods"]) == 8

    gap_text = "\n".join(result["gaps"])
    for end_date in result["pending_periods"]:
        assert end_date in gap_text, f"{end_date} 未进入 gaps"

    # 从 job 入口确认它真的落进 LLM 输入（截断到 6 条后仍在）
    payload = analysis_jobs.build_analysis_input(
        db, "600036", "A股", digest_gaps=result["gaps"]
    )
    assert any(
        "尚未生成摘要" in line for line in payload["report_digest_gaps"]
    ), payload.get("report_digest_gaps")
    assert len(payload["report_digests"]) == 2  # 只有本轮重建的两份可用


def test_pending_summary_survives_gap_truncation(db, monkeypatch):
    """[回归锁] 6 条封顶失败 + 2 份新生成 + 2 份待续跑：pending 汇总若 append
    在最后就是第 7 条，正好被 `gaps[:6]` 截掉——模型只看到失败，仍不知道还有
    年份没跑。缺失范围比单条失败详情更该活下来。
    """
    from app.services import security_analysis_jobs as analysis_jobs

    years = list(range(2025, 2015, -1))  # 10 份
    # 先让最旧的 6 份烧到封顶失败
    _patch_pipeline(monkeypatch, years=years[4:], llm_content="不是JSON")
    for _ in range(2):
        svc.ensure_report_digests(db, "600036", "A股", max_new=12)

    _patch_pipeline(monkeypatch, years=years)
    result = svc.ensure_report_digests(db, "600036", "A股", max_new=2)
    assert len(result["gaps"]) > analysis_jobs.MAX_DIGEST_GAPS
    assert result["pending_periods"] == ["20231231", "20221231"]
    assert result["gaps"][0].startswith("以下报告期尚未生成摘要")

    payload = analysis_jobs.build_analysis_input(
        db, "600036", "A股", digest_gaps=result["gaps"]
    )
    listed = payload["report_digest_gaps"]
    assert any("尚未生成摘要" in line for line in listed), listed
    for end_date in result["pending_periods"]:
        assert any(end_date in line for line in listed), listed
    # 截断本身也要可见，否则这 6 条会被当成缺口全集
    assert listed[-1].startswith("（另有") and "条摘要缺口未列出" in listed[-1]


def test_capped_failures_of_stale_versions_are_not_counted_as_permanent(db, monkeypatch):
    """版本升级后 ensure 会重新尝试这些报告期，详情页却还在写"永久失败"。"""
    _patch_pipeline(monkeypatch, years=[2025], llm_content="不是JSON")
    for _ in range(2):
        svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert svc.digest_progress(db, "600036", "A股")["failed_capped"] == 1

    row = db.query(SecurityProfileData).filter_by(dataset="report_digest").one()
    _write_row(db, "report_digest", row.period_key,
               {**row.payload, "extractor_version": 0})
    progress = svc.digest_progress(db, "600036", "A股")
    assert progress["failed_capped"] == 0
    assert progress["digested"] == 0


def test_input_meta_records_what_the_budget_dropped(db, monkeypatch):
    """[回归锁] 裁剪必须落库：'被截掉'和'公司没披露'在摘要里长得一模一样，
    而 prompt 要求原文未提及就写'原文未提及'。"""
    huge = (
        "前言。\n"
        "一、主营业务分析\n" + "主营内容。" * 8_000
        + "\n二、募集资金使用情况\n" + "低价值。" * 8_000
        + "\n三、公司未来发展的展望\n" + "展望。" * 2_000
    )
    monkeypatch.setattr(
        svc, "cached_report_targets_detailed", lambda db, s, m, **kw: {"targets": _targets([2025]), "complete": True}
    )
    monkeypatch.setattr(
        svc, "_ensure_section", lambda db, s, m, t: {"mdna": huge, "business": "短"}
    )
    monkeypatch.setattr(svc, "chat_completion", lambda messages, **kw: {
        "content": VALID_DIGEST, "model": "m", "usage": {},
    })
    svc.ensure_report_digests(db, "600036", "A股", max_new=1)
    payload = db.query(SecurityProfileData).filter_by(dataset="report_digest").one().payload
    meta = payload["input_meta"]["mdna"]
    assert meta["original_chars"] == len(huge.strip())
    assert meta["kept_chars"] < meta["original_chars"]
    assert meta["strategy"] == "structured"
    assert "募集资金使用情况" in meta["dropped_subsections"]
    # 未裁剪的节如实记 full
    assert payload["input_meta"]["business"]["strategy"] == "full"


@pytest.mark.parametrize("tier", ["A", "B", "C"])
def test_tier_budget_is_shared_across_sections_not_per_section(db, monkeypatch, tier):
    """[回归锁] 档位预算是**整份报告**的上限。每节各发一份完整预算的话，
    A 档三节就是 120k 而非声明的 40k，分档省下的 token 无从谈起。"""
    from app.services.report_digest_prompts import tier_spec

    huge = {name: "内容。" * 60_000 for name in ("business", "mdna", "risk_factors")}
    packed, meta = svc._pack_for_digest(huge, tier=tier)
    budget = tier_spec(tier)["budget"]
    assert sum(info["kept_chars"] for info in meta.values()) <= budget
    assert sum(len(body) for body in packed.values()) <= budget
    # mdna 权重最高，拿到的份额必须最大（digest 九字段里六个来自它）
    assert meta["mdna"]["kept_chars"] == max(i["kept_chars"] for i in meta.values())
    if tier == "C":
        assert set(packed) == {"mdna"}  # C 档只送 mdna


def test_rate_limited_llm_call_does_not_burn_attempts(db, monkeypatch):
    """429 是限流不是错误输出。旧实现按 `400 <= status < 500` 判确定性失败，
    两次限流就把该报告期永久封顶。"""
    monkeypatch.setattr(
        svc, "cached_report_targets_detailed", lambda db, s, m, **kw: {"targets": _targets([2025]), "complete": True}
    )
    monkeypatch.setattr(svc, "_ensure_section", lambda db, s, m, t: {"mdna": "内容"})
    monkeypatch.setattr(svc, "chat_completion", lambda messages, **kw: (
        (_ for _ in ()).throw(LLMClientError("rate limited", status_code=429))
    ))
    svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    row = db.query(SecurityProfileData).filter_by(dataset="report_digest").one()
    assert row.payload["attempts"] == 0


# ---------------------------------------------------------------------------
# digest 输出校验与压缩
# ---------------------------------------------------------------------------


def test_parse_digest_output_validation():
    digest = parse_digest_output(VALID_DIGEST)
    assert digest["业务分部占比"] == "零售 57%"
    assert digest["关键数字"] == ["营收 3391 亿元（2025 年度）"]

    with pytest.raises(ValueError, match="不是合法 JSON"):
        parse_digest_output("bad")
    with pytest.raises(ValueError, match="缺少字段"):
        parse_digest_output('{"经营回顾":"x"}')


def test_serialize_digest_compacts_old_years():
    digests = [
        {"end_date": f"{year}1231", "report_type": "annual",
         "digest": {"经营回顾": "长", "主营收入结构": "结构", "一次性项目": "无",
                    "会计信号": "无", "关键数字": ["n"], "展望": "好"}}
        for year in (2026, 2018)
    ]
    compacted = svc.serialize_digest_for_analysis(digests, compact_older_than_years=5)
    recent = next(c for c in compacted if c["end_date"].startswith("2026"))
    old = next(c for c in compacted if c["end_date"].startswith("2018"))
    assert "经营回顾" in recent["digest"]
    assert "经营回顾" not in old["digest"]  # 旧年份压缩为核心四字段
    assert set(old["digest"]) <= {"主营收入结构", "一次性项目", "会计信号", "关键数字"}


# ---------------------------------------------------------------------------
# 回填 job
# ---------------------------------------------------------------------------


def test_backfill_job_runs_batch_and_reports_progress(db, monkeypatch):
    _patch_pipeline(monkeypatch, years=[2025, 2024, 2023, 2022, 2021, 2020])
    job = backfill_jobs.start_report_backfill_job(1, "600036", "A股")
    backfill_jobs.run_report_backfill_job(job["id"])

    stored = db.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()
    assert stored.status == "succeeded"
    assert stored.data["result"]["generated"] == backfill_jobs.BACKFILL_BATCH_SIZE
    assert stored.data["result"]["remaining"] == 2

    # 跨标的活跃任务 → busy；同标的幂等
    job2 = backfill_jobs.start_report_backfill_job(1, "600036", "A股")
    assert job2["id"] != job["id"] or job2["status"] in ("queued", "succeeded")


def test_backfill_job_rejects_other_symbol_while_active(db, monkeypatch):
    monkeypatch.setattr(svc, "cached_report_targets_detailed", lambda db, s, m, **kw: {"targets": [], "complete": True})
    first = backfill_jobs.start_report_backfill_job(1, "600036", "A股")
    assert first["status"] == "queued"
    with pytest.raises(backfill_jobs.AnalysisBusyError, match="600036"):
        backfill_jobs.start_report_backfill_job(1, "000001", "A股")


def test_backfill_job_unsupported_market(db, monkeypatch):
    job = backfill_jobs.start_report_backfill_job(1, "600519", "沪深B股")
    backfill_jobs.run_report_backfill_job(job["id"])
    stored = db.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()
    assert stored.status == "failed"
    assert "暂不支持财报摘要" in (stored.error or "")


# ---------------------------------------------------------------------------
# 港股（披露易年报全文）
# ---------------------------------------------------------------------------


def _patch_hkex(monkeypatch, reports):
    from app.services import report_fetchers

    monkeypatch.setattr(
        report_fetchers, "hkex_annual_reports", lambda symbol, limit=12: reports
    )
    return report_fetchers


def test_plan_hk_targets_from_hkexnews(monkeypatch):
    """披露易年报清单 → targets；摘要/补充件剔除，同财年多份取公告日更新的。"""
    _patch_hkex(monkeypatch, [
        {"title": "2025 年報", "ann_date": "09/04/2026 17:21",
         "url": "https://www1.hkexnews.hk/2026/0409/a.pdf"},
        {"title": "2025 年報（更正）", "ann_date": "20/04/2026 10:00",
         "url": "https://www1.hkexnews.hk/2026/0420/fix.pdf"},
        {"title": "2024 年報摘要", "ann_date": "08/04/2025 17:02",
         "url": "https://www1.hkexnews.hk/2025/0408/sum.pdf"},
        {"title": "2024年度報告", "ann_date": "08/04/2025 17:02",
         "url": "https://www1.hkexnews.hk/2025/0408/b.pdf"},
    ])
    targets = svc.plan_report_targets("00700", "港股")
    assert [t["period_key"] for t in targets] == ["20251231|annual", "20241231|annual"]
    assert targets[0]["url"].endswith("a.pdf")  # 「更正」件被剔除
    assert all(t["report_type"] == "annual" for t in targets)


def test_hkex_failure_marks_plan_incomplete(monkeypatch):
    """源站故障不得被当成"这家公司没有年报"——否则短 TTL 缓存失效语义失灵。"""
    fetchers = _patch_hkex(monkeypatch, [])
    monkeypatch.setattr(
        fetchers, "hkex_annual_reports",
        lambda symbol, limit=12: (_ for _ in ()).throw(RuntimeError("503")),
    )
    planned = svc.plan_report_targets_detailed("00700", "港股")
    assert planned["targets"] == []
    assert planned["complete"] is False


@pytest.mark.parametrize(
    ("title", "ann_date", "expected"),
    [
        ("2025 年報", "09/04/2026 17:21", "20251231"),  # 12 月财年（多数港股）
        ("2025年度報告", "30/04/2026 17:21", "20251231"),
        ("2026 年報", "25/07/2026 09:00", "20260331"),  # 3 月财年（阿里等）
        ("2025 年報", "20/02/2026 08:00", "20251231"),  # 早发的 12 月财年
        ("2025 年報", "", "20251231"),  # 公告日缺失 → 退回 12/31
        ("年報", "09/04/2026 17:21", None),  # 标题无年份 → 无法定位报告期
    ],
)
def test_hk_fiscal_end_inferred_from_announcement_gap(title, ann_date, expected):
    """港股财年不统一（阿里 3/31）。一律按 12/31 会让摘要的报告期与 Yahoo 的
    真实 asOfDate 对不上，LLM 就拿到同一年的两个口径。"""
    assert svc._infer_hk_fiscal_end(title, ann_date) == expected


def test_hk_revision_wins_by_parsed_date_not_string_order(monkeypatch):
    """[回归锁] 披露易公告日是 `DD/MM/YYYY`。按字符串比会认为 "30/03/2025" 比
    "02/04/2025" 新，于是缓存住原件、重刊永远进不来（源指纹也不会变）。"""
    _patch_hkex(monkeypatch, [
        {"title": "2024 年報", "ann_date": "30/03/2025 09:00",
         "url": "https://www1.hkexnews.hk/orig.pdf"},
        {"title": "2024 年報", "ann_date": "02/04/2025 10:00",  # 跨月重刊
         "url": "https://www1.hkexnews.hk/reissued.pdf"},
        {"title": "2023 年報", "ann_date": "29/12/2024 09:00",
         "url": "https://www1.hkexnews.hk/old.pdf"},
        {"title": "2023 年報", "ann_date": "05/01/2025 09:00",  # 跨年重刊
         "url": "https://www1.hkexnews.hk/new-year.pdf"},
    ])
    targets = svc.plan_report_targets("00700", "港股")
    assert len(targets) == 2  # 两个财年各留一份
    urls = {t["end_date"][:4]: t["url"] for t in targets}
    assert urls["2024"].endswith("reissued.pdf")
    assert urls["2023"].endswith("new-year.pdf")


def test_hk_chinese_numeral_year_titles_are_recognized(monkeypatch):
    """[回归锁] 「二零二五年年報」必须解析出报告期。

    实测中海油 00883、绿城 03900 连续多年全用中文数字年份：只认阿拉伯数字时
    每一份都被静默丢弃，清单变成 complete-empty——批量层把"检索正常但一份都
    没识别出来"当成"该标的无年报"记成功，整个标的无声消失。
    """
    _patch_hkex(monkeypatch, [
        {"title": "二零二五年年報", "ann_date": "09/04/2026 17:21",
         "url": "https://www1.hkexnews.hk/2026/0409/a.pdf"},
        {"title": "二零二四年年報", "ann_date": "08/04/2025 09:00",
         "url": "https://www1.hkexnews.hk/2025/0408/b.pdf"},
    ])
    targets = svc.plan_report_targets("00883", "港股")
    assert [t["period_key"] for t in targets] == ["20251231|annual", "20241231|annual"]


def test_hk_unparseable_announcement_date_never_wins(monkeypatch):
    """坏公告日给确定性兜底（date.min），不得覆盖可解析的那一份。"""
    _patch_hkex(monkeypatch, [
        {"title": "2024 年報", "ann_date": "30/03/2025 09:00",
         "url": "https://www1.hkexnews.hk/good.pdf"},
        {"title": "2024 年報", "ann_date": "n/a", "url": "https://www1.hkexnews.hk/bad.pdf"},
    ])
    targets = svc.plan_report_targets("00700", "港股")
    assert targets[0]["url"].endswith("good.pdf")


def test_hk_section_download_uses_hkexnews_source(db, monkeypatch):
    """港股 PDF 必须走披露易的限速桶与 Referer——共用巨潮的会被拒或互相拖慢。"""
    seen = {}

    def fake_download(url, *, source="cninfo"):
        seen["source"] = source
        raise RuntimeError("stop-here")  # 只验证路由，不进 pdfplumber

    monkeypatch.setattr(svc, "download_report_pdf", fake_download)
    target = {
        "period_key": "20251231|annual", "report_type": "annual",
        "end_date": "20251231", "title": "2025 年報", "ann_date": "09/04/2026 17:21",
        "url": "https://www1.hkexnews.hk/2026/0409/a.pdf",
    }
    assert svc._ensure_section(db, "00700", "港股", target) is None
    assert seen["source"] == "hkexnews"


def test_v3_cached_us_sections_are_re_extracted(db, monkeypatch):
    """[回归锁] 美股侧的抽取修复也必须 bump 版本号。

    #118 只改了中文侧却也是 v3；若本 PR 沿用 v3，那批按 v3 缓存的 10-K 节选
    会直接命中旧缓存——iXBRL 泄漏、Item 边界这些修复对存量报告完全不生效，
    关联 digest 也不会重建。
    """
    from app.services import report_fetchers, report_sections

    assert report_sections.SECTION_EXTRACTOR_VERSION > 3
    target = {
        "period_key": "20250927|10-K", "report_type": "10-K", "end_date": "20250927",
        "title": "10-K", "ann_date": "2025-11-01",
        "url": {"cik": 320193, "accession": "a", "document": "d.htm"},
    }
    _write_row(db, "report_section", target["period_key"], {
        "source_fingerprint": svc.source_fingerprint(target),
        "extractor_version": 3,  # #118 时代的缓存
        "extract_status": "ok",
        "sections": {"mdna": "带 iXBRL 噪声的旧节选"},
    })
    downloads = []
    monkeypatch.setattr(
        report_fetchers, "edgar_download_filing",
        lambda cik, accession, document: downloads.append(document) or "<html></html>",
    )
    monkeypatch.setattr(
        report_sections, "extract_us_items", lambda html, form_type="10-K": {}
    )
    svc._ensure_section(db, "AAPL", "美股", target)
    assert downloads, "v3 缓存必须重新下载抽取"


def test_unbounded_item_sections_are_not_offered_for_digest(db, monkeypatch):
    """[回归锁] 带 unbounded_item 的节选是"终止标题没匹配到、一路吃到文末"的
    产物，内容跨了好几章。当成成功节选去摘要，比缺这一节更糟。"""
    from app.services import report_fetchers, report_sections

    monkeypatch.setattr(
        report_fetchers, "edgar_download_filing",
        lambda cik, accession, document: "<html></html>",
    )
    monkeypatch.setattr(
        report_sections, "extract_us_items",
        lambda html, form_type="10-K": {
            "mdna": report_sections.SectionResult(
                text="x" * 900, locator="item_heading", truncated=True,
                confidence=1.0, quality_flags=("unbounded_item",),
            ),
        },
    )
    target = {
        "period_key": "20251231|20-F", "report_type": "20-F", "end_date": "20251231",
        "title": "20-F", "ann_date": "2026-04-20",
        "url": {"cik": 1, "accession": "a", "document": "d.htm"},
    }
    # mdna 被剔除 → 无可用章节 → 落 failed 行而不是拿跨章内容去摘要
    assert svc._ensure_section(db, "BABA", "美股", target) is None
    row = db.query(SecurityProfileData).filter_by(dataset="report_section").one()
    assert row.payload["extract_status"] == "failed"
    assert "管理层讨论与分析" in row.payload["error"]


def test_hk_market_capabilities_expose_report_digest():
    """能力位漏改的话后端支持了而前端不渲染入口——这一条就是防它。"""
    from app.services.security_profile_service import MARKET_CAPABILITIES

    assert "港股" in svc.REPORT_MARKETS
    assert MARKET_CAPABILITIES["港股"]["report_digest"] is True


# ---------------------------------------------------------------------------
# 美股（EDGAR 10-K）
# ---------------------------------------------------------------------------


def _patch_edgar_lookup(monkeypatch, result={"cik": "0000320193", "title": "Apple Inc."}):
    from app.services import report_fetchers

    monkeypatch.setattr(report_fetchers, "edgar_lookup", lambda symbol: result)
    return report_fetchers


def test_plan_us_targets_maps_10k_filings(monkeypatch):
    """美股规划：EDGAR 近十份年报 → period_key/url(dict) 结构；未注册代码→空。"""
    fetchers = _patch_edgar_lookup(monkeypatch)
    monkeypatch.setattr(
        fetchers, "edgar_recent_annual_filings",
        lambda cik, limit: [
            {"form": "10-K", "report_date": "2025-09-27", "filing_date": "2025-11-01",
             "accession": "0000320193-25-000123", "primary_document": "aapl-2025.htm"},
            {"form": "10-K", "report_date": "2024-09-28", "filing_date": "2024-11-01",
             "accession": "0000320193-24-000100", "primary_document": "aapl-2024.htm"},
        ],
    )
    targets = svc.plan_report_targets("AAPL", "美股")
    assert [t["period_key"] for t in targets] == ["20250927|10-K", "20240928|10-K"]
    assert targets[0]["report_type"] == "10-K"
    assert targets[0]["url"] == {
        "cik": "0000320193", "accession": "0000320193-25-000123",
        "document": "aapl-2025.htm",
    }

    _patch_edgar_lookup(monkeypatch, result=None)
    assert svc.plan_report_targets("ZZZZ", "美股") == []


def test_plan_us_targets_accepts_20f_for_foreign_issuers(monkeypatch):
    """[回归锁] 中概股报 20-F 而非 10-K。只认 10-K 会检索到 0 份年报——
    财报摘要与商业画像整块空白，而表面上"没有报错"。"""
    fetchers = _patch_edgar_lookup(
        monkeypatch, result={"cik": "0001737806", "title": "PDD Holdings Inc."}
    )
    monkeypatch.setattr(
        fetchers, "edgar_recent_annual_filings",
        lambda cik, limit: [
            {"form": "20-F", "report_date": "2025-12-31", "filing_date": "2026-04-20",
             "accession": "0001737806-26-000010", "primary_document": "pdd-20251231x20f.htm"},
            # 同一家公司换过表单类型时两份都要在，且缓存键不能撞
            {"form": "10-K", "report_date": "2024-12-31", "filing_date": "2025-04-20",
             "accession": "0001737806-25-000010", "primary_document": "pdd-2024.htm"},
        ],
    )
    targets = svc.plan_report_targets("PDD", "美股")
    assert [t["period_key"] for t in targets] == ["20251231|20-F", "20241231|10-K"]
    assert targets[0]["report_type"] == "20-F"
    assert targets[0]["title"].startswith("20-F")


def test_us_section_extraction_receives_the_form_type(db, monkeypatch):
    """report_type 必须透传给抽取器——否则 20-F 会被按 10-K 的 Item 编号抽。"""
    seen = {}

    def fake_extract(html, *, form_type="10-K"):
        seen["form_type"] = form_type
        return {}

    from app.services import report_fetchers, report_sections

    monkeypatch.setattr(
        report_fetchers, "edgar_download_filing",
        lambda cik, accession, document: "<html></html>",
    )
    monkeypatch.setattr(report_sections, "extract_us_items", fake_extract)
    target = {
        "period_key": "20251231|20-F", "report_type": "20-F", "end_date": "20251231",
        "title": "20-F (2025-12-31)", "ann_date": "2026-04-20",
        "url": {"cik": 1737806, "accession": "a", "document": "d.htm"},
    }
    assert svc._ensure_section(db, "PDD", "美股", target) is None  # 无 mdna → 失败
    assert seen["form_type"] == "20-F"


def test_ensure_section_us_html_branch(db, monkeypatch):
    """美股 _ensure_section 走 EDGAR HTML 下载 + Item 抽取，落库 sections 三键。"""
    from app.services import report_fetchers

    from .test_report_sections import make_10k_html

    monkeypatch.setattr(
        report_fetchers, "edgar_download_filing",
        lambda cik, accession, document: make_10k_html(),
    )
    target = {
        "period_key": "20250927|10-K", "report_type": "10-K",
        "end_date": "20250927", "title": "10-K (2025-09-27)",
        "ann_date": "2025-11-01",
        "url": {"cik": "0000320193", "accession": "acc-1", "document": "doc.htm"},
    }
    sections = svc._ensure_section(db, "AAPL", "美股", target)
    assert sections is not None
    assert set(sections) == {"business", "risk_factors", "mdna"}
    assert "Net sales increased" in sections["mdna"]

    row = (
        db.query(SecurityProfileData)
        .filter_by(symbol="AAPL", market="美股", dataset="report_section")
        .one()
    )
    assert row.payload["extract_status"] == "ok"
    assert row.payload["section_meta"]["mdna"]["locator"] == "item_heading"


# ---------------------------------------------------------------------------
# [评审回归] 传输安全、瞬时失败额度、修订版缓存失效
# ---------------------------------------------------------------------------


def test_report_urls_never_downgrade_to_plain_http(monkeypatch):
    """[评审回归] 全链路 HTTPS：检索出的 PDF URL 与请求 base/Referer 均不得
    降级为明文（响应可被替换 → 污染投资分析）。"""
    from app.services import report_fetchers as rf

    assert rf._CNINFO_BASE.startswith("https://")
    assert rf._CNINFO_STATIC.startswith("https://")
    assert rf._CNINFO_HEADERS["Referer"].startswith("https://")

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "announcements": [{
                    "announcementTitle": "2025年年度报告",
                    "announcementTime": 1774627200000,
                    "adjunctUrl": "finalpage/2026-03-28/1225047778.PDF",
                    "adjunctSize": 32000,
                }],
                "hasMore": False,
            }

    monkeypatch.setattr(rf, "cninfo_org_id", lambda symbol: "gssh0600036")
    monkeypatch.setattr(rf.requests, "post", lambda url, **kw: FakeResponse())
    rows = rf.cninfo_search_reports("600036", report_type="annual", se_date="2016-01-01~2026-08-03")
    assert rows[0]["url"].startswith("https://")

    # 库内残留的旧明文 URL 重放时也必须被拒绝，而不是静默降级
    with pytest.raises(ValueError, match="非 HTTPS"):
        rf.download_report_pdf("http://static.cninfo.com.cn/finalpage/x.PDF")


def test_transient_download_failure_does_not_burn_attempts(db, monkeypatch):
    """[评审回归] 两次瞬时故障（超时/5xx）不消耗永久额度，第三次成功仍能落 ok。"""
    import requests

    target = _targets([2025])[0]

    def timeout(url, **kw):
        raise requests.Timeout("read timeout")

    monkeypatch.setattr(svc, "download_report_pdf", timeout)
    assert svc._ensure_section(db, "600036", "A股", target) is None

    response = requests.Response()
    response.status_code = 503

    def server_error(url, **kw):
        raise requests.HTTPError("503 Server Error", response=response)

    monkeypatch.setattr(svc, "download_report_pdf", server_error)
    assert svc._ensure_section(db, "600036", "A股", target) is None

    row = db.query(SecurityProfileData).filter_by(dataset="report_section").one()
    assert row.payload["attempts"] == 0  # 瞬时失败不计次数
    assert row.payload["extract_status"] == "failed"

    # 恢复后仍可成功（未被永久封顶）
    monkeypatch.setattr(svc, "download_report_pdf", lambda url, **kw: b"%PDF-fake")
    monkeypatch.setattr(
        svc, "pages_to_text", lambda pages: "第三节 管理层讨论与分析\n经营情况" + "内容" * 400
    )
    monkeypatch.setattr(
        svc.pdfplumber, "open",
        lambda stream: _FakePdf(),
    )
    sections = svc._ensure_section(db, "600036", "A股", target)
    assert sections is not None and sections.get("mdna")
    row = db.query(SecurityProfileData).filter_by(dataset="report_section").one()
    assert row.payload["extract_status"] == "ok"


def test_deterministic_extract_failure_still_counts_attempts(db, monkeypatch):
    """确定性失败（PDF 损坏/章节定位失败）照常计 attempts 并封顶。"""
    monkeypatch.setattr(
        svc, "download_report_pdf",
        lambda url, **kw: (_ for _ in ()).throw(ValueError("PDF 超过大小上限 50MB")),
    )
    target = _targets([2025])[0]
    assert svc._ensure_section(db, "600036", "A股", target) is None
    assert svc._ensure_section(db, "600036", "A股", target) is None
    row = db.query(SecurityProfileData).filter_by(dataset="report_section").one()
    assert row.payload["attempts"] == svc.MAX_ATTEMPTS


def test_revised_report_invalidates_cached_section_and_digest(db, monkeypatch):
    """[评审回归] 先缓存原版（ok），同报告期出现修订版（新 URL/公告日）时
    节选与摘要都必须重新生成，而不是被旧缓存永久遮住。"""
    calls = _patch_pipeline(monkeypatch, years=[2025])
    first = svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert first["generated"] == 1
    assert len(calls) == 1

    # 二次运行同一版本：命中缓存零 LLM 调用
    svc.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert len(calls) == 1

    revised = _targets([2025])[0] | {
        "url": "https://static.cninfo.com.cn/final/2025-revised.PDF",
        "ann_date": "2026-04-20",
        "title": "2025年年度报告（修订版）",
    }
    monkeypatch.setattr(
        svc, "cached_report_targets_detailed", lambda db, symbol, market, **kw: {"targets": [revised], "complete": True}
    )
    section_calls = []

    def fake_section(db_, symbol, market, target):
        section_calls.append(target["url"])
        return {"business": "业务概要（修订）", "mdna": "经营分析（修订）"}

    monkeypatch.setattr(svc, "_ensure_section", fake_section)
    third = svc.ensure_report_digests(db, "600036", "A股", max_new=4)

    assert third["generated"] == 1  # 修订版重新生成
    assert section_calls == [revised["url"]]
    assert len(calls) == 2
    row = db.query(SecurityProfileData).filter_by(dataset="report_digest").one()
    assert row.payload["source_fingerprint"] == svc.source_fingerprint(revised)


class _FakePdf:
    """pdfplumber 上下文管理器替身（页文本由 pages_to_text 桩接管）。"""

    pages: list = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# ---------------------------------------------------------------------------
# 年报清单缓存（消除每次分析必打的 2-10 次 cninfo）
# ---------------------------------------------------------------------------


def test_cached_report_targets_hits_within_ttl(db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        svc, "plan_report_targets_detailed",
        lambda symbol, market: calls.append((symbol, market))
        or {"targets": _targets([2025, 2024]), "complete": True, "failed_kinds": []},
    )
    first = svc.cached_report_targets(db, "600036", "A股")
    assert len(first) == 2 and len(calls) == 1

    def explode(symbol, market):
        raise AssertionError("TTL 内不得重新检索 cninfo")

    monkeypatch.setattr(svc, "plan_report_targets_detailed", explode)
    assert svc.cached_report_targets(db, "600036", "A股") == first

    # force_refresh 绕过缓存（详情页手动回填给用户的口子）
    monkeypatch.setattr(
        svc, "plan_report_targets_detailed",
        lambda symbol, market: {
            "targets": _targets([2025]), "complete": True, "failed_kinds": []
        },
    )
    assert len(svc.cached_report_targets(db, "600036", "A股", force_refresh=True)) == 1


def test_cached_report_targets_expires(db, monkeypatch):
    monkeypatch.setattr(
        svc, "plan_report_targets_detailed",
        lambda s, m: {"targets": _targets([2025]), "complete": True, "failed_kinds": []},
    )
    svc.cached_report_targets(db, "600036", "A股")

    row = (
        db.query(SecurityProfileData)
        .filter_by(dataset="report_target_plan", period_key="current")
        .one()
    )
    payload = dict(row.payload)
    payload["planned_at"] = "2020-01-01T00:00:00+00:00"  # 远超 TTL
    svc._upsert(db, "600036", "A股", "report_target_plan", "current", payload)
    db.commit()

    calls = []
    monkeypatch.setattr(
        svc, "plan_report_targets_detailed",
        lambda s, m: calls.append(s)
        or {"targets": _targets([2025]), "complete": True, "failed_kinds": []},
    )
    svc.cached_report_targets(db, "600036", "A股")
    assert calls == ["600036"]


def test_empty_plan_uses_short_ttl(db, monkeypatch):
    """空清单（源站故障或确实无年报）只缓存 1 小时：既不遮住新披露，
    也不让故障期反复轰炸源站。"""
    monkeypatch.setattr(
        svc, "plan_report_targets_detailed",
        lambda s, m: {"targets": [], "complete": True, "failed_kinds": []},
    )
    assert svc.cached_report_targets(db, "600036", "A股") == []

    row = (
        db.query(SecurityProfileData)
        .filter_by(dataset="report_target_plan", period_key="current")
        .one()
    )
    assert row.payload["status"] == "empty"

    # 1 小时内命中缓存
    monkeypatch.setattr(
        svc, "plan_report_targets_detailed",
        lambda s, m: (_ for _ in ()).throw(AssertionError("短 TTL 内不得重试")),
    )
    assert svc.cached_report_targets(db, "600036", "A股") == []

    # 超过 1 小时即重试
    payload = dict(row.payload)
    payload["planned_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()
    svc._upsert(db, "600036", "A股", "report_target_plan", "current", payload)
    db.commit()
    monkeypatch.setattr(
        svc, "plan_report_targets_detailed",
        lambda s, m: {"targets": _targets([2025]), "complete": True, "failed_kinds": []},
    )
    assert len(svc.cached_report_targets(db, "600036", "A股")) == 1


def test_partial_plan_is_not_cached_as_complete(db, monkeypatch):
    """[评审回归] annual 检索失败、semi 成功时 targets 非空，但缺了十年年报。

    按 24h 缓存会让整轮批量分析静默缺年报，随后 24h 新鲜度又跳过这份缺数据的
    分析——必须标 partial 走短 TTL，下次调用重新尝试 annual。
    """
    attempts = []

    def flaky_search(symbol, report_type, se_date):
        attempts.append(report_type)
        if report_type == "annual":
            raise RuntimeError("cninfo 检索超时")
        return [
            {"title": "2026年半年度报告", "ann_date": "2026-08-20", "url": "u-semi",
             "adjunct_size_kb": 1},
        ]

    monkeypatch.setattr(svc, "cninfo_search_reports", flaky_search)
    first = svc.cached_report_targets(db, "600036", "A股")
    assert [t["report_type"] for t in first] == ["semi"]  # 只有半年报

    row = (
        db.query(SecurityProfileData)
        .filter_by(dataset="report_target_plan", period_key="current")
        .one()
    )
    assert row.payload["status"] == "partial"
    assert row.payload["failed_kinds"] == ["annual"]

    # 短 TTL 过期后必须重试 annual（不能命中 24h 的 ok 缓存）
    payload = dict(row.payload)
    payload["planned_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()
    svc._upsert(db, "600036", "A股", "report_target_plan", "current", payload)
    db.commit()

    attempts.clear()

    def healthy_search(symbol, report_type, se_date):
        attempts.append(report_type)
        if report_type == "annual":
            return [
                {"title": "2025年年度报告", "ann_date": "2026-03-28", "url": "u-annual",
                 "adjunct_size_kb": 1},
            ]
        return [
            {"title": "2026年半年度报告", "ann_date": "2026-08-20", "url": "u-semi",
             "adjunct_size_kb": 1},
        ]

    monkeypatch.setattr(svc, "cninfo_search_reports", healthy_search)
    second = svc.cached_report_targets(db, "600036", "A股")
    assert "annual" in attempts  # 真的重试了年报检索
    assert {t["report_type"] for t in second} == {"annual", "semi"}

    row = (
        db.query(SecurityProfileData)
        .filter_by(dataset="report_target_plan", period_key="current")
        .one()
    )
    assert row.payload["status"] == "ok"  # 恢复完整后才转长 TTL


def test_plan_detailed_reports_completeness(monkeypatch):
    """规划层如实报告完整性；兼容函数 plan_report_targets 仍只返回列表。"""
    def only_semi(symbol, report_type, se_date):
        if report_type == "annual":
            raise RuntimeError("boom")
        return [{"title": "2026年半年度报告", "ann_date": "2026-08-20", "url": "u",
                 "adjunct_size_kb": 1}]

    monkeypatch.setattr(svc, "cninfo_search_reports", only_semi)
    detailed = svc.plan_report_targets_detailed("600036", "A股")
    assert detailed["complete"] is False
    assert detailed["failed_kinds"] == ["annual"]
    assert len(svc.plan_report_targets("600036", "A股")) == 1

    # 美股：EDGAR 清单请求失败 → 不完整；代码不在注册表 → 是确定答案不是失败
    from app.services import report_fetchers

    monkeypatch.setattr(
        report_fetchers, "edgar_lookup", lambda s: {"cik": 1, "title": "X"}
    )
    monkeypatch.setattr(
        report_fetchers, "edgar_recent_annual_filings",
        lambda cik, limit: (_ for _ in ()).throw(RuntimeError("edgar down")),
    )
    assert svc.plan_report_targets_detailed("AAPL", "美股")["complete"] is False

    monkeypatch.setattr(report_fetchers, "edgar_lookup", lambda s: None)
    assert svc.plan_report_targets_detailed("ZZZZ", "美股")["complete"] is True
