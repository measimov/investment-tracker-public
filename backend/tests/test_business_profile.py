"""商业画像：输入组装、输出校验、刷新策略、同业名单过滤与上限。"""

import pytest

from app.database import SessionLocal
from app.models.security_profile import SecurityProfileData
from app.services import business_profile_service as svc
from app.services.security_profile_service import upsert_profile_row
from app.services.report_digest_prompts import DIGEST_PROMPT_VERSION
from app.services.report_sections import SECTION_EXTRACTOR_VERSION

from .helpers import reset_tables

VALID_PROFILE = (
    '{"商业模式":"零售银行为主，赚取息差与手续费",'
    '"业务分部":[{"名称":"零售金融","收入占比":"57%","毛利率":"未披露","趋势":"平稳"},'
    '{"名称":"批发金融","收入占比":"41%","毛利率":"未披露","趋势":"平稳"}],'
    '"上游依赖":[{"要素":"客户存款","影响":"负债成本决定息差"}],'
    '"下游需求":[{"客群或场景":"个人信贷","需求驱动":"居民消费与购房"}],'
    '"供应商集中度":"未披露","客户集中度":"前十借款人占比 2.39%",'
    '"行业与竞争":"股份行头部（公司自述口径）",'
    '"估值观察因子":[{"因子":"市场利率","方向":"上游成本","传导":"→息差"},'
    '{"因子":"居民信贷需求","方向":"下游需求","传导":"→收入增速"}]}'
)


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


def _seed_digest(db, end_date="20251231"):
    upsert_profile_row(db, "600036", "A股", "report_digest", f"{end_date}|annual", {
        "status": "ok", "report_type": "annual", "end_date": end_date,
        "extractor_version": SECTION_EXTRACTOR_VERSION,
        "prompt_version": DIGEST_PROMPT_VERSION,
        "source_url": "http://example/a.pdf",
        "digest": {"业务分部占比": "零售 57%", "上下游与产业链": "存款为源",
                   "主营收入结构": "净利息为主", "经营回顾": "稳健",
                   "会计信号": "无", "关键数字": ["营收 3391 亿"]},
    })
    db.commit()


def test_parse_output_validation():
    profile = svc.parse_business_profile_output(VALID_PROFILE)
    assert profile["业务分部"][0]["名称"] == "零售金融"
    assert len(profile["估值观察因子"]) == 2

    with pytest.raises(ValueError, match="不是合法 JSON"):
        svc.parse_business_profile_output("bad")
    with pytest.raises(ValueError, match="缺少字段"):
        svc.parse_business_profile_output('{"商业模式":"x"}')
    with pytest.raises(ValueError, match="估值观察因子"):
        svc.parse_business_profile_output(
            VALID_PROFILE.replace(
                '"估值观察因子":[{"因子":"市场利率","方向":"上游成本","传导":"→息差"},'
                '{"因子":"居民信贷需求","方向":"下游需求","传导":"→收入增速"}]',
                '"估值观察因子":[]',
            )
        )


def test_parse_enforces_declared_array_contract():
    """[评审回归] prompt 声明的下限与元素 schema 必须在解析层强制执行——
    JSON mode 只保证语法，残缺输出不得被当成成功缓存。"""
    # 业务分部少于 prompt 要求的 2 项 → 确定性失败（原来 1 项即通过）
    with pytest.raises(ValueError, match="业务分部 至少需要 2 项"):
        svc.parse_business_profile_output(
            VALID_PROFILE.replace(
                ',{"名称":"批发金融","收入占比":"41%","毛利率":"未披露","趋势":"平稳"}',
                "",
            )
        )
    # 元素缺必需键 → 失败（原来只校验是 dict，详情页会留空列）
    with pytest.raises(ValueError, match=r"业务分部\[1\] 缺少字段: 收入占比"):
        svc.parse_business_profile_output(
            VALID_PROFILE.replace(
                '{"名称":"批发金融","收入占比":"41%","毛利率":"未披露","趋势":"平稳"}',
                '{"名称":"批发金融","毛利率":"未披露","趋势":"平稳"}',
            )
        )
    # 元素键存在但为空串 → 同样失败
    with pytest.raises(ValueError, match=r"估值观察因子\[0\] 缺少字段: 传导"):
        svc.parse_business_profile_output(
            VALID_PROFILE.replace(
                '{"因子":"市场利率","方向":"上游成本","传导":"→息差"}',
                '{"因子":"市场利率","方向":"上游成本","传导":"  "}',
            )
        )
    # 上游依赖/下游需求各自的元素 schema
    with pytest.raises(ValueError, match=r"下游需求\[0\] 缺少字段: 需求驱动"):
        svc.parse_business_profile_output(
            VALID_PROFILE.replace(
                '{"客群或场景":"个人信贷","需求驱动":"居民消费与购房"}',
                '{"客群或场景":"个人信贷"}',
            )
        )


def test_input_assembly_uses_digest_slices_and_business_section(db):
    _seed_digest(db)
    upsert_profile_row(db, "600036", "A股", "report_section", "20251231|annual", {
        "extract_status": "ok",
        "sections": {"business": "业务概要" * 5000, "mdna": "经营分析"},
    })
    db.commit()

    payload = svc.build_business_profile_input(db, "600036", "A股")
    assert payload["report_digest_slices"][0]["业务分部占比"] == "零售 57%"
    assert len(payload["business_section_excerpt"]) == 10_000  # 节选裁剪
    assert payload["source_end_date"] == "20251231"


def test_ensure_profile_caches_and_refreshes_on_new_digest(db, monkeypatch):
    _seed_digest(db)
    calls = []

    def fake_llm(messages, **kw):
        calls.append(messages)
        return {"content": VALID_PROFILE, "model": "deepseek-v4-pro", "usage": {}}

    monkeypatch.setattr(svc, "chat_completion", fake_llm)

    profile = svc.ensure_business_profile(db, "600036", "A股")
    assert profile["商业模式"].startswith("零售银行")
    assert len(calls) == 1

    # 无新报告期 → 缓存命中零调用
    profile = svc.ensure_business_profile(db, "600036", "A股")
    assert len(calls) == 1

    # 新报告期 digest 出现 → 重生成
    _seed_digest(db, end_date="20261231")
    svc.ensure_business_profile(db, "600036", "A股")
    assert len(calls) == 2


def test_input_falls_back_to_earlier_successful_business_section(db):
    """[评审回归] 最新报告期抽取失败时，画像输入仍须使用次新的成功节选，
    而不是整块业务概要缺失。"""
    _seed_digest(db)
    upsert_profile_row(db, "600036", "A股", "report_section", "20251231|annual", {
        "extract_status": "failed", "error": "PDF 损坏", "attempts": 2, "sections": {},
    })
    upsert_profile_row(db, "600036", "A股", "report_section", "20241231|annual", {
        "extract_status": "ok",
        "sections": {"business": "2024 业务概要：零售与批发两大板块", "mdna": "经营分析"},
    })
    db.commit()

    payload = svc.build_business_profile_input(db, "600036", "A股")
    assert payload["business_section_excerpt"].startswith("2024 业务概要")


def test_ensure_profile_regenerates_when_same_period_content_changes(db, monkeypatch):
    """[评审回归] 同一 end_date 下源内容更新（修订版重抽/摘要重生成/人工纠正）
    必须重新生成画像——只比 source_end_date 会让旧画像永久生效。"""
    _seed_digest(db)
    calls = []
    monkeypatch.setattr(
        svc, "chat_completion",
        lambda messages, **kw: calls.append(messages)
        or {"content": VALID_PROFILE, "model": "m", "usage": {}},
    )
    svc.ensure_business_profile(db, "600036", "A股")
    assert len(calls) == 1
    svc.ensure_business_profile(db, "600036", "A股")
    assert len(calls) == 1  # 内容未变 → 缓存命中

    # 同报告期摘要内容被修订（end_date 不变）
    upsert_profile_row(db, "600036", "A股", "report_digest", "20251231|annual", {
        "status": "ok", "report_type": "annual", "end_date": "20251231",
        "extractor_version": SECTION_EXTRACTOR_VERSION,
        "prompt_version": DIGEST_PROMPT_VERSION,
        "source_url": "https://example/a-revised.pdf",
        "digest": {"业务分部占比": "零售 61%（修订）", "上下游与产业链": "存款为源",
                   "主营收入结构": "净利息为主", "经营回顾": "稳健",
                   "会计信号": "无", "关键数字": ["营收 3391 亿"]},
    })
    db.commit()

    svc.ensure_business_profile(db, "600036", "A股")
    assert len(calls) == 2  # 内容指纹变化 → 重生成

    row = (
        db.query(SecurityProfileData)
        .filter_by(symbol="600036", dataset="business_profile", period_key="current")
        .one()
    )
    assert row.payload["input_fingerprint"] == svc.input_fingerprint(
        svc.build_business_profile_input(db, "600036", "A股")
    )


def test_ensure_profile_without_sources_skips_llm(db, monkeypatch):
    def explode(*args, **kw):
        raise AssertionError("无源数据不得调用 LLM")

    monkeypatch.setattr(svc, "chat_completion", explode)
    assert svc.ensure_business_profile(db, "600036", "A股") is None


def test_llm_failure_keeps_previous_profile(db, monkeypatch):
    _seed_digest(db)
    monkeypatch.setattr(
        svc, "chat_completion",
        lambda *a, **k: {"content": VALID_PROFILE, "model": "m", "usage": {}},
    )
    assert svc.ensure_business_profile(db, "600036", "A股") is not None

    _seed_digest(db, end_date="20261231")  # 触发重生成
    monkeypatch.setattr(
        svc, "chat_completion",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("上游故障")),
    )
    profile = svc.ensure_business_profile(db, "600036", "A股")
    assert profile is not None  # 失败降级为旧缓存
    assert profile["商业模式"].startswith("零售银行")


def test_peer_list_filters_industry_and_caps(db, monkeypatch):
    listing = [{"symbol": "600036", "name": "招商银行", "industry": "银行"}] + [
        {"symbol": f"60{i:04d}", "name": f"银行{i}", "industry": "银行"}
        for i in range(40)
    ] + [{"symbol": "600519", "name": "贵州茅台", "industry": "白酒"}]
    monkeypatch.setattr(svc, "_load_stock_basic", lambda: listing)

    peers = svc.ensure_peer_list(db, "600036", "A股")
    assert len(peers) == svc.PEER_LIST_CAP
    assert all(p["industry"] == "银行" for p in peers)
    assert all(p["symbol"] != "600036" for p in peers)  # 自身排除

    stored = svc.load_business_profile(db, "600036", "A股")
    assert stored["industry"] == "银行"
    assert len(stored["peers"]) == svc.PEER_LIST_CAP

    # 港股无同业源（美股走 EDGAR SIC，见 test_us_peer_list_by_sic）
    assert svc.ensure_peer_list(db, "00700", "港股") == []


def test_peer_list_failure_returns_cached(db, monkeypatch):
    upsert_profile_row(db, "600036", "A股", "peer_list", "current", {
        "industry": "银行", "peers": [{"symbol": "601398", "name": "工商银行",
                                       "industry": "银行"}],
    })
    db.commit()
    monkeypatch.setattr(
        svc, "_load_stock_basic",
        lambda: (_ for _ in ()).throw(RuntimeError("tushare 不可用")),
    )
    peers = svc.ensure_peer_list(db, "600036", "A股")
    assert peers[0]["symbol"] == "601398"  # 降级为缓存


def test_us_peer_list_by_sic(db, monkeypatch):
    """美股同业：SIC 同码 CIK 经 company_tickers 反查 ticker/注册名；剔除自身、
    剔除无 ticker 的非上市 filer、上限 30；获取失败时返回已缓存名单。

    [实测回归] browse-edgar atom 的公司名损坏（ARRAY(0x..) 占位），名称
    必须走反查而非 atom。"""
    from app.services import report_fetchers

    monkeypatch.setattr(
        report_fetchers, "edgar_lookup",
        lambda symbol: {"cik": 320193, "title": "Apple Inc."},
    )
    monkeypatch.setattr(
        report_fetchers, "edgar_submissions", lambda cik: {"sic": "3571"}
    )
    # 自身 + 一个非上市 filer（反查无 ticker）+ 40 个上市同业
    companies = [{"cik": 320193}, {"cik": 999999}] + [
        {"cik": i} for i in range(1, 41)
    ]
    monkeypatch.setattr(
        report_fetchers, "edgar_same_sic_companies", lambda sic: companies
    )
    monkeypatch.setattr(
        report_fetchers, "edgar_reverse_lookup",
        lambda cik: None if cik == 999999
        else {"symbol": f"PEER{cik}", "title": f"Peer {cik} Inc."},
    )

    peers = svc.ensure_peer_list(db, "AAPL", "美股")
    assert len(peers) == svc.PEER_LIST_CAP
    assert all(p["name"] != "Apple Inc." for p in peers)  # 自身剔除
    assert all(p["symbol"] for p in peers)  # 非上市 filer 剔除
    assert peers[0] == {"symbol": "PEER1", "name": "Peer 1 Inc.", "industry": "SIC 3571"}

    row = (
        db.query(SecurityProfileData)
        .filter_by(symbol="AAPL", market="美股", dataset="peer_list",
                   period_key="current")
        .one()
    )
    assert len(row.payload["peers"]) == svc.PEER_LIST_CAP

    # EDGAR 故障 → 返回缓存而非空/报错
    def boom(sic):
        raise RuntimeError("edgar down")

    monkeypatch.setattr(report_fetchers, "edgar_same_sic_companies", boom)
    cached = svc.ensure_peer_list(db, "AAPL", "美股")
    assert len(cached) == svc.PEER_LIST_CAP


def test_us_peer_list_missing_sic_returns_empty(db, monkeypatch):
    from app.services import report_fetchers

    monkeypatch.setattr(
        report_fetchers, "edgar_lookup", lambda symbol: {"cik": "1", "title": "X"}
    )
    monkeypatch.setattr(report_fetchers, "edgar_submissions", lambda cik: {"sic": ""})
    assert svc.ensure_peer_list(db, "XXXX", "美股") == []


def test_peer_list_hits_ttl_cache_without_refetching(db, monkeypatch):
    """[评审顺带修] 同业名单原来只在异常时才用缓存行，正常路径每次重新生成——
    每只美股白打 2 次 EDGAR。TTL 内必须直接命中缓存。"""
    from app.services import report_fetchers

    calls = []
    monkeypatch.setattr(
        report_fetchers, "edgar_lookup",
        lambda symbol: {"cik": 320193, "title": "Apple Inc."},
    )
    monkeypatch.setattr(
        report_fetchers, "edgar_submissions",
        lambda cik: calls.append(cik) or {"sic": "3571"},
    )
    monkeypatch.setattr(
        report_fetchers, "edgar_same_sic_companies", lambda sic: [{"cik": 1}, {"cik": 2}]
    )
    monkeypatch.setattr(
        report_fetchers, "edgar_reverse_lookup",
        lambda cik: {"symbol": f"P{cik}", "title": f"Peer {cik}"},
    )

    first = svc.ensure_peer_list(db, "AAPL", "美股")
    assert len(first) == 2 and len(calls) == 1

    second = svc.ensure_peer_list(db, "AAPL", "美股")
    assert second == first
    assert len(calls) == 1  # TTL 内零外呼


def test_peer_list_refetches_after_ttl(db, monkeypatch):
    from datetime import timedelta

    from app.services import report_fetchers

    monkeypatch.setattr(
        report_fetchers, "edgar_lookup", lambda symbol: {"cik": 1, "title": "X"}
    )
    monkeypatch.setattr(report_fetchers, "edgar_submissions", lambda cik: {"sic": "3571"})
    monkeypatch.setattr(
        report_fetchers, "edgar_same_sic_companies", lambda sic: [{"cik": 9}]
    )
    monkeypatch.setattr(
        report_fetchers, "edgar_reverse_lookup",
        lambda cik: {"symbol": "P9", "title": "Peer 9"},
    )
    svc.ensure_peer_list(db, "AAPL", "美股")

    row = svc._peer_list_row(db, "AAPL", "美股")
    row.fetched_at = row.fetched_at - timedelta(days=svc.PEER_LIST_TTL_DAYS + 1)
    db.commit()

    calls = []
    monkeypatch.setattr(
        report_fetchers, "edgar_submissions",
        lambda cik: calls.append(cik) or {"sic": "3571"},
    )
    svc.ensure_peer_list(db, "AAPL", "美股")
    assert calls == [1]  # 过期后重新生成
