"""观点摘要 job：解析强制、输入组装、单 job、批量语义、API 列表口径。

全部 monkeypatch scan_matched_utterances / chat_completion，不触发真实外呼。
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.background_job import BackgroundJob
from app.models.holding import Holding
from app.models.security_opinion import SecurityOpinionSummary
from app.models.user import User
from app.models.watchlist_item import WatchlistItem
from app.services import opinion_summary_batch_jobs as batch
from app.services import opinion_summary_jobs as jobs
from app.services.llm_client import LLMClientError
from app.services.opinion_summary_prompts import parse_opinion_output

from .helpers import reset_tables

JOB_TYPES = ["opinion_summary", "opinion_summary_batch", "security_analysis"]
RESET_MODELS = [SecurityOpinionSummary, WatchlistItem, Holding]

NOW = datetime.now(timezone.utc)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        reset_tables(session, RESET_MODELS)
        session.query(BackgroundJob).filter(
            BackgroundJob.job_type.in_(JOB_TYPES)
        ).delete(synchronize_session=False)
        session.commit()
        yield session
        session.rollback()
        reset_tables(session, RESET_MODELS)
        session.query(BackgroundJob).filter(
            BackgroundJob.job_type.in_(JOB_TYPES)
        ).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


def _hold(db, symbol, market, user_id=1, quantity="100"):
    db.add(Holding(
        user_id=user_id, symbol=symbol, name=symbol, market=market,
        quantity=Decimal(quantity), avg_cost=Decimal("10"),
        total_cost=Decimal("1000"), currency="CNY",
    ))
    db.commit()


def _watch(db, symbol, market, user_id=1):
    db.add(WatchlistItem(user_id=user_id, symbol=symbol, market=market))
    db.commit()


def _utt(author="管我财", days_ago=1.0, body="看好", kind="homepage_post", key=None):
    at = NOW - timedelta(days=days_ago)
    return {
        "utterance_key": key or f"u-{author}-{days_ago}",
        "kind": kind, "author_name": author, "text": body,
        "context_text": None, "context_author_name": None,
        "post_url": None, "created_at": at,
    }


def _llm_output(tags=None, stances=None):
    return json.dumps({
        "tags": tags or ["偏多"],
        "summary": "总体偏多",
        "author_stances": stances if stances is not None else [
            {"author": "管我财", "stance": "看多", "recent_change": "无", "evidence": "看好"}
        ],
        "report_markdown": "## 近期观点变化\n无",
    }, ensure_ascii=False)


def _fake_completion(content):
    return {
        "content": content, "model": "test-model",
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


# --------------------------------------------------------------------------- #
# parse_opinion_output 强制规则
# --------------------------------------------------------------------------- #
def _stats(recent=2, baseline=3, author="管我财", **extra_authors):
    """构造 author_stats；extra_authors 形如 甲=(recent, baseline)。"""
    stats = {author: {"recent": recent, "baseline": baseline}}
    for name, (r, b) in extra_authors.items():
        stats[name] = {"recent": r, "baseline": b}
    return stats


def _parse(content, author_stats=None, **stats_kwargs):
    return parse_opinion_output(
        content, author_stats=author_stats or _stats(**stats_kwargs)
    )


def test_parse_happy_path_truncates_and_normalizes():
    out = json.loads(_llm_output(tags=["近期转多", "多空分歧"], stances=[
        {"author": "管我财", "stance": "看多", "recent_change": "转多", "evidence": "证" * 200}
    ]))
    out["summary"] = "长" * 500
    parsed = _parse(json.dumps(out, ensure_ascii=False))
    assert parsed["tags"] == ["近期转多", "多空分歧"]
    assert len(parsed["summary"]) == 300
    assert len(parsed["author_stances"][0]["evidence"]) == 120


@pytest.mark.parametrize(
    ("mutate", "hint"),
    [
        (lambda d: d.update(tags=[]), "1-4"),
        (lambda d: d.update(tags=["偏多", "偏空", "中性观望", "讨论沉寂", "事件驱动讨论"]), "1-4"),
        (lambda d: d.update(tags=["自造标签"]), "白名单"),
        (lambda d: d.update(tags=["偏多", "偏多"]), "重复"),
        (lambda d: d.update(tags=["近期转多", "近期转空"]), "互斥"),
        (lambda d: d.update(summary=""), "summary"),
        (lambda d: d.update(report_markdown=" "), "report_markdown"),
        (lambda d: d.update(author_stances=[{"author": "幻觉作者", "stance": "看多",
                                             "recent_change": "无", "evidence": "x"}]), "作者"),
        (lambda d: d["author_stances"][0].update(stance="强烈看多"), "stance"),
        (lambda d: d["author_stances"][0].update(recent_change="翻多"), "recent_change"),
    ],
)
def test_parse_rejects_invalid_output(mutate, hint):
    data = json.loads(_llm_output())
    mutate(data)
    with pytest.raises(ValueError) as excinfo:
        _parse(json.dumps(data, ensure_ascii=False))
    assert hint in str(excinfo.value)


def test_parse_not_json_rejected():
    with pytest.raises(ValueError):
        _parse("这不是 JSON")


def test_parse_grounding_recent_zero_bans_change_tags():
    for tag in ("近期转多", "近期转空", "新增关注", "关注度上升"):
        with pytest.raises(ValueError, match="近期窗口零发言"):
            _parse(_llm_output(tags=[tag]), recent=0, baseline=5)


def test_parse_grounding_baseline_rules():
    with pytest.raises(ValueError, match="新增关注"):
        _parse(_llm_output(tags=["新增关注"]), recent=2, baseline=3)
    # baseline=0 且该作者确实标了「新增」才允许「新增关注」
    newly = _llm_output(tags=["新增关注"], stances=[
        {"author": "管我财", "stance": "看多", "recent_change": "新增", "evidence": "x"}
    ])
    parsed = _parse(newly, recent=2, baseline=0)
    assert parsed["tags"] == ["新增关注"]
    with pytest.raises(ValueError, match="讨论沉寂"):
        _parse(_llm_output(tags=["讨论沉寂"]), recent=2, baseline=0)


def test_parse_grounding_author_change_needs_own_baseline():
    stances = [{"author": "管我财", "stance": "看多", "recent_change": "转多", "evidence": "x"}]
    with pytest.raises(ValueError, match="不可能「转多」"):
        _parse(_llm_output(stances=stances), recent=2, baseline=0)


def test_parse_per_author_change_rejects_global_count_alibi():
    """评审 P1 场景：A 只在 baseline、B 只在 recent，给 B 标「转多」。

    旧实现用全局 baseline>0 当依据会放行；逐作者接地必须拒绝——B 自己
    没有 baseline，谈不上「转」。
    """
    stats = _stats(recent=0, baseline=5, author="甲", 乙=(3, 0))
    stances = [
        {"author": "甲", "stance": "看多", "recent_change": "无", "evidence": "x"},
        {"author": "乙", "stance": "看多", "recent_change": "转多", "evidence": "x"},
    ]
    with pytest.raises(ValueError, match="乙 无 baseline"):
        _parse(_llm_output(tags=["偏多"], stances=stances), author_stats=stats)


def test_parse_author_set_must_match_exactly():
    stats = _stats(recent=2, baseline=3, author="甲", 乙=(1, 1))
    only_one = [{"author": "甲", "stance": "看多", "recent_change": "无", "evidence": "x"}]
    with pytest.raises(ValueError, match="缺少输入作者"):
        _parse(_llm_output(tags=["偏多"], stances=only_one), author_stats=stats)

    duplicated = only_one + only_one
    with pytest.raises(ValueError, match="重复"):
        _parse(
            _llm_output(tags=["偏多"], stances=duplicated),
            author_stats=_stats(author="甲"),
        )


def test_parse_top_level_change_tags_need_author_backing():
    """顶层「近期转多」没有任何作者通过「转多」校验时必须拒绝。"""
    with pytest.raises(ValueError, match="缺少逐作者依据"):
        _parse(_llm_output(tags=["近期转多"]))  # 默认 stance 是「无」


def test_parse_author_new_flag_needs_empty_baseline():
    stances = [{"author": "管我财", "stance": "看多", "recent_change": "新增", "evidence": "x"}]
    with pytest.raises(ValueError, match="不得标「新增」"):
        _parse(_llm_output(tags=["偏多"], stances=stances), recent=2, baseline=3)


# --------------------------------------------------------------------------- #
# build_opinion_input
# --------------------------------------------------------------------------- #
def test_build_input_groups_and_splits_windows():
    matched = [
        _utt(author="甲", days_ago=2, body="近期看多"),
        _utt(author="甲", days_ago=90, body="早先看空"),
        _utt(author="乙", days_ago=40, body="观望", kind="comment_reply"),
    ]
    payload = jobs.build_opinion_input(
        matched, symbol="600519", market="A股", xq_symbol="SH600519",
        recent_days=30, lookback_days=180, now=NOW,
    )
    assert payload["stats"] == {
        "utterance_count": 3, "recent_count": 1, "author_count": 2,
        "author_stats": {"甲": {"recent": 1, "baseline": 1}, "乙": {"recent": 0, "baseline": 1}},
        "latest_utterance_at": (NOW - timedelta(days=2)).isoformat(),
    }
    assert [row["text"] for row in payload["authors"]["甲"]["recent"]] == ["近期看多"]
    assert [row["text"] for row in payload["authors"]["甲"]["baseline"]] == ["早先看空"]
    assert payload["authors"]["乙"]["baseline"][0]["kind"] == "评论回复"
    assert payload["meta"]["recent_days"] == 30


def test_build_input_shrinks_baseline_and_notes_truncation():
    matched = [_utt(author="甲", days_ago=100 + i, body="长" * 300, key=f"u{i}")
               for i in range(120)]
    payload = jobs.build_opinion_input(
        matched, symbol="600519", market="A股", xq_symbol="SH600519",
        recent_days=30, lookback_days=180, now=NOW,
    )
    assert len(payload["authors"]["甲"]["baseline"]) == jobs._BASELINE_KEEP_PER_AUTHOR
    # 逐作者统计必须是收缩前的真实条数，不能被"只留最新 10 条"污染
    assert payload["stats"]["author_stats"]["甲"] == {"recent": 0, "baseline": 120}
    assert "110 条" in payload["meta"]["truncation_note"]
    # 留下的是最新的（days_ago 最小 = 时间最近）
    kept_first = payload["authors"]["甲"]["baseline"][0]
    assert kept_first["date"] == (NOW - timedelta(days=109)).date().isoformat()


# --------------------------------------------------------------------------- #
# 单标的 job
# --------------------------------------------------------------------------- #
def _run_single(db, monkeypatch, *, matched, llm_content=None, user_id=1,
                symbol="600519", market="A股"):
    calls = {"llm": 0}

    def fake_scan(db_, wanted, *, since):
        return {next(iter(wanted)): matched}

    def fake_chat(messages, **kwargs):
        calls["llm"] += 1
        return _fake_completion(llm_content or _llm_output())

    monkeypatch.setattr(jobs, "scan_matched_utterances", fake_scan)
    monkeypatch.setattr(jobs, "chat_completion", fake_chat)
    monkeypatch.setattr(jobs, "resolve_public_security_name", lambda s, m: "测试名称")
    job = jobs.start_opinion_summary_job(user_id, symbol, market)
    jobs.run_opinion_summary_job(job["id"])
    stored = db.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()
    db.refresh(stored)
    return stored, calls


def test_single_job_success_persists_all_fields(db, monkeypatch):
    matched = [_utt(author="管我财", days_ago=2), _utt(author="管我财", days_ago=60)]
    stored, calls = _run_single(db, monkeypatch, matched=matched)
    assert stored.status == "succeeded"
    assert calls["llm"] == 1
    row = db.query(SecurityOpinionSummary).one()
    assert (row.symbol, row.market, row.name) == ("600519", "A股", "测试名称")
    assert row.tags == ["偏多"]
    assert row.author_stances[0]["author"] == "管我财"
    assert row.model == "test-model"
    assert row.total_tokens == 30
    assert (row.utterance_count, row.recent_utterance_count) == (2, 1)
    assert row.recent_days == 30 and row.lookback_days == 180
    assert row.latest_utterance_at is not None
    assert row.input_payload["stats"]["utterance_count"] == 2
    assert stored.data["summary_id"] == row.id


def test_single_job_no_matches_fails_without_llm(db, monkeypatch):
    stored, calls = _run_single(db, monkeypatch, matched=[])
    assert stored.status == "failed"
    assert "未提及" in stored.error
    assert calls["llm"] == 0
    assert db.query(SecurityOpinionSummary).count() == 0


def test_single_job_parse_failure_is_deterministic(db, monkeypatch):
    stored, _ = _run_single(
        db, monkeypatch, matched=[_utt()], llm_content='{"tags": ["自造"]}'
    )
    assert stored.status == "failed"
    assert "解析失败" in stored.error
    assert stored.attempt_count == 1  # 确定性失败不烧重试


def test_single_job_source_unavailable(db, monkeypatch):
    def raise_unavailable(db_, wanted, *, since):
        raise jobs.OpinionSourceUnavailable("数据源未接入")

    monkeypatch.setattr(jobs, "scan_matched_utterances", raise_unavailable)
    job = jobs.start_opinion_summary_job(1, "600519", "A股")
    jobs.run_opinion_summary_job(job["id"])
    stored = db.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()
    assert stored.status == "failed"
    assert "数据源未接入" in stored.error


def test_single_job_busy_on_other_symbol(db, monkeypatch):
    jobs.start_opinion_summary_job(1, "600519", "A股")
    with pytest.raises(jobs.AnalysisBusyError):
        jobs.start_opinion_summary_job(1, "000001", "A股")


# --------------------------------------------------------------------------- #
# 批量 job
# --------------------------------------------------------------------------- #
def _prime_source(monkeypatch, matched_map, *, stale=False):
    """把批量模块的外部表依赖替换为固定数据。matched_map 按雪球码给行。"""
    fresh = {
        "available": True, "latest_scan_at": NOW.isoformat(),
        "latest_utterance_at": NOW.isoformat(), "stale": stale,
    }
    monkeypatch.setattr(batch, "source_freshness", lambda db_: dict(fresh))
    monkeypatch.setattr(
        batch, "scan_matched_utterances",
        lambda db_, wanted, *, since: {
            key: rows for key, rows in matched_map.items() if key in wanted
        },
    )


def _run_batch(db, monkeypatch, *, matched_map, outcomes=None, user_id=1, force=False):
    calls = []

    def fake_summarize(db_, symbol, market, *, matched=None, on_stage=None):
        calls.append({"symbol": symbol, "market": market, "matched": matched})
        if on_stage:
            on_stage("llm_summary", {})
        if outcomes:
            outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome(symbol, market)
        return {
            "symbol": symbol, "market": market, "status": "succeeded",
            "summary_id": len(calls), "error": None, "error_kind": None,
            "tags": ["偏多"],
        }

    _prime_source(monkeypatch, matched_map)
    monkeypatch.setattr(batch, "summarize_one", fake_summarize)
    monkeypatch.setattr(batch, "PAUSE_SECONDS", 0)
    job = batch.start_opinion_batch_job(db, user_id, force=force)
    batch.run_opinion_batch_job(job["id"])
    stored = db.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()
    db.refresh(stored)
    return stored, calls


def _fail(error="boom", kind="parse"):
    def build(symbol, market):
        return {
            "symbol": symbol, "market": market, "status": "failed",
            "summary_id": None, "error": error, "error_kind": kind,
        }

    return build


def test_batch_targets_union_watchlist_origin_and_pruning(db, monkeypatch):
    """持仓∪自选、B股纳入、零匹配剔除、origin 标注。"""
    _hold(db, "600519", "A股")
    _hold(db, "200596", "B股")           # B股持仓：观点口径纳入
    _hold(db, "BTC", "加密货币")          # 非观点市场剔除
    _watch(db, "00700", "港股")           # 纯自选
    _watch(db, "600519", "A股")           # 与持仓重叠 → both
    _hold(db, "999999", "A股")            # 无人提及 → 零匹配剔除
    _prime_source(monkeypatch, {
        "SH600519": [_utt(days_ago=2), _utt(days_ago=60)],
        "SZ200596": [_utt(days_ago=5)],
        "00700": [_utt(days_ago=1)],
    })
    preview = batch.get_opinion_batch_targets(db, 1)
    by_key = {f"{t['market']}|{t['symbol']}": t for t in preview["targets"]}
    assert set(by_key) == {"A股|600519", "B股|200596", "港股|00700"}
    assert by_key["A股|600519"]["origin"] == "both"
    assert by_key["B股|200596"]["origin"] == "holding"
    assert by_key["港股|00700"]["origin"] == "watchlist"
    assert by_key["A股|600519"]["matched_count"] == 2
    assert by_key["A股|600519"]["recent_count"] == 1


def test_batch_runs_targets_and_passes_matched(db, monkeypatch):
    _hold(db, "600519", "A股")
    _watch(db, "00700", "港股")
    rows_a = [_utt(days_ago=2)]
    stored, calls = _run_batch(
        db, monkeypatch,
        matched_map={"SH600519": rows_a, "00700": [_utt(days_ago=1)]},
    )
    assert stored.status == "succeeded"
    assert stored.data["success_count"] == 2
    assert {c["symbol"] for c in calls} == {"600519", "00700"}
    # 批量把预扫结果透传给 summarize_one，不重复全扫
    assert next(c for c in calls if c["symbol"] == "600519")["matched"] == rows_a


def test_batch_freshness_dual_condition(db, monkeypatch):
    """窗口内跳过；窗口外但无新发言也跳过；有新发言则重跑；force 全跑。"""
    _hold(db, "600519", "A股")
    _hold(db, "000001", "A股")
    old = NOW - timedelta(days=3)
    for symbol, latest_utt in (("600519", NOW - timedelta(days=2)), ("000001", old - timedelta(days=1))):
        db.add(SecurityOpinionSummary(
            symbol=symbol, market="A股", tags=["偏多"], author_stances=[],
            summary="旧摘要", content="x", model="m", input_payload={},
            recent_days=30, lookback_days=180, utterance_count=1,
            recent_utterance_count=0, latest_utterance_at=latest_utt,
            created_at=old,
        ))
    db.commit()
    matched_map = {
        # 600519 有晚于摘要锚点的新发言 → 必须重跑
        "SH600519": [_utt(days_ago=1, key="new")],
        # 000001 的匹配发言都早于摘要锚点 → 跳过
        "SZ000001": [_utt(days_ago=10, key="old")],
    }
    stored, calls = _run_batch(db, monkeypatch, matched_map=matched_map)
    assert stored.status == "succeeded"
    assert {c["symbol"] for c in calls} == {"600519"}
    assert stored.data["skipped_count"] == 1

    # force：两只都跑
    db.query(BackgroundJob).filter(BackgroundJob.job_type == batch.JOB_TYPE).delete()
    db.commit()
    stored, calls = _run_batch(db, monkeypatch, matched_map=matched_map, force=True)
    assert {c["symbol"] for c in calls} == {"600519", "000001"}


def test_batch_fatal_outcome_aborts(db, monkeypatch):
    _hold(db, "600519", "A股")
    _hold(db, "000001", "A股")
    stored, calls = _run_batch(
        db, monkeypatch,
        matched_map={"SH600519": [_utt()], "SZ000001": [_utt()]},
        outcomes=[_fail("API key 无效", "llm_auth")],
    )
    assert stored.status == "failed"
    assert len(calls) == 1  # 第二只没跑
    assert "无法继续" in stored.data["abort_reason"]


def test_batch_transient_exception_aborts_on_auth(db, monkeypatch):
    _hold(db, "600519", "A股")
    _hold(db, "000001", "A股")
    stored, calls = _run_batch(
        db, monkeypatch,
        matched_map={"SH600519": [_utt()], "SZ000001": [_utt()]},
        outcomes=[LLMClientError("401", status_code=401)],
    )
    assert stored.status == "failed"
    assert len(calls) == 1


def test_batch_consecutive_failures_early_stop(db, monkeypatch):
    for symbol in ("600519", "000001", "600036", "000858"):
        _hold(db, symbol, "A股")
    stored, calls = _run_batch(
        db, monkeypatch,
        matched_map={key: [_utt()] for key in ("SH600519", "SZ000001", "SH600036", "SZ000858")},
        outcomes=[_fail()],  # 全部失败
    )
    assert stored.status == "failed"
    assert len(calls) == batch.MAX_CONSECUTIVE_FAILURES
    assert "连续" in stored.error


def test_batch_no_targets_raises(db, monkeypatch):
    _prime_source(monkeypatch, {})
    _hold(db, "600519", "A股")
    with pytest.raises(batch.NoBatchTargetsError):
        batch.start_opinion_batch_job(db, 1)


def test_batch_source_unavailable_raises(db, monkeypatch):
    monkeypatch.setattr(
        batch, "source_freshness",
        lambda db_: {"available": False, "latest_scan_at": None,
                     "latest_utterance_at": None, "stale": False},
    )
    with pytest.raises(batch.OpinionSourceUnavailable):
        batch.start_opinion_batch_job(db, 1)


# --------------------------------------------------------------------------- #
# API：列表口径与降级
# --------------------------------------------------------------------------- #
@pytest.fixture
def api_user():
    session = SessionLocal()
    try:
        user = session.query(User).filter(User.username == "demo").one()
        original = user.hashed_password
        user.hashed_password = get_password_hash("opinion-api-password")
        session.commit()
        yield user.id
        user.hashed_password = original
        session.commit()
    finally:
        session.close()


async def _login(client):
    login = await client.post(
        "/api/auth/token",
        json={"username": "demo", "password": "opinion-api-password"},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.mark.anyio
async def test_opinion_summaries_counts_and_missing_summary_rows(db, api_user, monkeypatch):
    import app.services.xueqiu_opinion_source as src

    _hold(db, "600519", "A股", user_id=api_user)
    _watch(db, "00700", "港股", user_id=api_user)
    anchor = NOW - timedelta(days=5)
    db.add(SecurityOpinionSummary(
        symbol="600519", market="A股", tags=["近期转多"], author_stances=[],
        summary="观点摘要", content="x", model="m", input_payload={},
        recent_days=30, lookback_days=180, utterance_count=3,
        recent_utterance_count=1, latest_utterance_at=anchor,
    ))
    db.commit()
    monkeypatch.setattr(
        src, "source_freshness",
        lambda db_: {"available": True, "latest_scan_at": NOW.isoformat(),
                     "latest_utterance_at": NOW.isoformat(), "stale": False},
    )
    monkeypatch.setattr(
        src, "scan_matched_utterances",
        lambda db_, wanted, *, since: {
            "SH600519": [_utt(days_ago=1, key="new"), _utt(days_ago=10, key="old")],
            "00700": [_utt(days_ago=2, key="hk")],
        },
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _login(client)
        resp = await client.get("/api/securities/opinion-summaries", headers=auth)
    body = resp.json()
    assert body["source_available"] is True
    items = {f"{item['market']}|{item['symbol']}": item for item in body["items"]}
    a = items["A股|600519"]
    assert a["tags"] == ["近期转多"]
    assert a["matched_count"] == 2
    assert a["new_utterance_count"] == 1  # 只有晚于摘要锚点的那条算新
    assert a["origin"] == "holding"
    hk = items["港股|00700"]
    assert hk["id"] is None and hk["matched_count"] == 1 and hk["new_utterance_count"] == 1


@pytest.mark.anyio
async def test_opinion_endpoints_degrade_without_table(db, api_user, monkeypatch):
    """表不存在：列表不 5xx 且已存摘要照常返回；启动端点 409。"""
    _hold(db, "600519", "A股", user_id=api_user)
    db.add(SecurityOpinionSummary(
        symbol="600519", market="A股", tags=["偏多"], author_stances=[],
        summary="历史摘要", content="x", model="m", input_payload={},
        recent_days=30, lookback_days=180, utterance_count=1,
        recent_utterance_count=0,
    ))
    db.commit()
    monkeypatch.setattr("app.api.security_profiles.is_llm_configured", lambda: True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _login(client)
        listing = await client.get("/api/securities/opinion-summaries", headers=auth)
        start_single = await client.post(
            "/api/securities/A股/600519/opinion-jobs", headers=auth
        )
        start_batch = await client.post("/api/securities/opinion-batch-jobs", headers=auth)
        feed = await client.get("/api/securities/opinion-feed", headers=auth)
    body = listing.json()
    assert listing.status_code == 200
    assert body["source_available"] is False
    assert body["items"][0]["summary"] == "历史摘要"
    assert body["items"][0]["matched_count"] is None
    assert start_single.status_code == 409 and "未接入" in start_single.json()["detail"]
    assert start_batch.status_code == 409 and "未接入" in start_batch.json()["detail"]
    assert feed.status_code == 200 and feed.json()["source_available"] is False


@pytest.mark.anyio
async def test_opinion_summaries_degrade_after_real_sql_error(db, api_user):
    """评审 P2 的端到端口径：外部表列漂移（真实 SQL 错误）后，同一请求里的
    后续查询（持仓/自选/最新摘要）必须照常工作并返回降级响应，而非 500。
    不打任何 monkeypatch——走真实 reader 路径。"""
    from sqlalchemy import text as sa_text

    _hold(db, "600519", "A股", user_id=api_user)
    db.execute(sa_text(
        "CREATE TABLE IF NOT EXISTS xueqiu_archiver_utterances "
        "(utterance_key text PRIMARY KEY, created_at_ms bigint)"  # 刻意缺 last_seen_at
    ))
    db.commit()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            auth = await _login(client)
            listing = await client.get("/api/securities/opinion-summaries", headers=auth)
        assert listing.status_code == 200
        assert listing.json()["source_available"] is False
    finally:
        db.execute(sa_text("DROP TABLE IF EXISTS xueqiu_archiver_utterances"))
        db.commit()


@pytest.mark.anyio
async def test_opinion_feed_per_author_cap_and_symbol_filter(db, api_user, monkeypatch):
    """作者动态：逐作者封顶（不做全局截断）、total 如实、作者按最新发言排序、
    symbol 过滤单标的。全局截断曾让高产作者把其他人整段挤掉（"只有管我财"）。"""
    import app.services.xueqiu_opinion_source as src

    _hold(db, "600519", "A股", user_id=api_user)
    _hold(db, "000001", "A股", user_id=api_user)
    monkeypatch.setattr(
        src, "source_freshness",
        lambda db_: {"available": True, "latest_scan_at": NOW.isoformat(),
                     "latest_utterance_at": NOW.isoformat(), "stale": False},
    )

    # 高产作者甲 60 条（600519）+ 低产作者乙 2 条（000001，最新一条比甲新）
    def fake_scan(db_, wanted, *, since):
        result = {}
        if "SH600519" in wanted:
            result["SH600519"] = [
                _utt(author="甲", days_ago=1 + i * 0.1, key=f"a{i}") for i in range(60)
            ]
        if "SZ000001" in wanted:
            result["SZ000001"] = [
                _utt(author="乙", days_ago=0.5, key="b0"),
                _utt(author="乙", days_ago=9, key="b1"),
            ]
        return result

    monkeypatch.setattr(src, "scan_matched_utterances", fake_scan)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _login(client)
        feed = await client.get(
            "/api/securities/opinion-feed?per_author=10", headers=auth
        )
        body = feed.json()
        groups = {g["author"]: g for g in body["authors"]}
        # 逐作者封顶 + total 如实：乙不因甲高产而消失
        assert groups["甲"]["total"] == 60 and len(groups["甲"]["items"]) == 10
        assert groups["乙"]["total"] == 2 and len(groups["乙"]["items"]) == 2
        # 作者按各自最新发言倒序：乙（0.5 天前）排在甲（1 天前）之前
        assert [g["author"] for g in body["authors"]] == ["乙", "甲"]

        # 单标的过滤：只回 600519 的匹配（详情页「相关作者动态」）
        single = await client.get(
            "/api/securities/opinion-feed?symbol=600519&market=A股", headers=auth
        )
        assert [g["author"] for g in single.json()["authors"]] == ["甲"]

        # 参数校验：symbol/market 必须成对；不支持市场 409
        lonely = await client.get("/api/securities/opinion-feed?symbol=600519", headers=auth)
        assert lonely.status_code == 422
        bad = await client.get(
            "/api/securities/opinion-feed?symbol=BTC&market=加密货币", headers=auth
        )
        assert bad.status_code == 409


@pytest.mark.anyio
async def test_opinion_summary_detail_with_previous(db, api_user):
    for index, tags in enumerate((["偏空"], ["近期转多"])):
        db.add(SecurityOpinionSummary(
            symbol="600519", market="A股", tags=tags, author_stances=[],
            summary=f"第{index}版", content=f"## 全文{index}", model="m",
            input_payload={}, recent_days=30, lookback_days=180,
            utterance_count=1, recent_utterance_count=0,
            created_at=NOW - timedelta(days=1 - index),
        ))
    db.commit()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _login(client)
        detail = await client.get("/api/securities/A股/600519/opinion-summary", headers=auth)
        missing = await client.get("/api/securities/A股/999999/opinion-summary", headers=auth)
    body = detail.json()
    assert body["tags"] == ["近期转多"] and body["content"] == "## 全文1"
    assert body["previous"]["tags"] == ["偏空"]
    assert missing.status_code == 404


def test_batch_mutex_with_analysis_family(db):
    """观点批量与标的分析互斥（ensure_no_conflicting_analysis_job 覆盖新类型）。"""
    from app.services.background_job_store import create_or_get_active_job
    from app.services.security_analysis_batch_jobs import (
        ensure_no_conflicting_analysis_job,
    )
    from app.services.security_analysis_jobs import AnalysisBusyError

    create_or_get_active_job("opinion_summary_batch", 1, {"targets": []})
    with pytest.raises(AnalysisBusyError, match="批量观点摘要"):
        ensure_no_conflicting_analysis_job(db, 1, "security_analysis")
