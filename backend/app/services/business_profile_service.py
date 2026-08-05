"""商业画像合成与同业名单（owner 核心诉求：产业链视角的估值因子）。

business_profile（dataset=business_profile, period_key=current）是独立缓存
的合成产物：商业模式/业务分部占比/上游依赖/下游需求/集中度/估值观察
因子——看同业与因子估值是高频复用场景，不应每次重跑主分析。

同业名单（dataset=peer_list）来自 Tushare stock_basic 行业分类（客观数据，
非 LLM 先验）；LLM 分析只可"提及"同业名单，禁止对同业展开分析（它们的
数据不在输入里）。
"""

import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..core.logging import get_app_logger
from ..models.security_profile import SecurityProfileData
from .llm_client import chat_completion
from .report_digest_service import load_report_digests
from .security_profile_service import load_symbol_profile, upsert_profile_row

logger = get_app_logger(__name__)

PEER_LIST_CAP = 30
# 同业名单 TTL：行业分类/SIC 同码几个月才变一次，没必要每次分析都重新外呼
PEER_LIST_TTL_DAYS = 30

# 业务概要节选向前找几期（最新报告抽取失败时用上一期，不至于整块缺失）
BUSINESS_SECTION_LOOKBACK = 5

PROFILE_FIELDS = ("商业模式", "行业与竞争", "供应商集中度", "客户集中度")

# 数组字段的输出契约：(字段, 最少项, 最多项, 每项必需的字符串键)
# prompt 声明的下限必须在解析层强制执行——JSON mode 只保证语法。
PROFILE_ARRAY_SPECS = (
    ("业务分部", 2, 6, ("名称", "收入占比", "毛利率", "趋势")),
    ("上游依赖", 1, 4, ("要素", "影响")),
    ("下游需求", 1, 4, ("客群或场景", "需求驱动")),
    ("估值观察因子", 2, 5, ("因子", "方向", "传导")),
)

BUSINESS_PROFILE_SYSTEM_PROMPT = """你是商业画像分析助手。只依据用户提供的财报摘要、业务概要原文节选与财务科目数据，为一家上市公司生成结构化商业画像；禁止引入任何对该公司的先验知识，原文与数据未提及的写"数据未提及"。

输出严格 JSON（无 markdown 围栏）：
{"商业模式": "怎么赚钱：产品/服务、定价方式、渠道，≤300字",
 "业务分部": [{"名称": "...", "收入占比": "如 35%（数据未提及则写'未披露'）", "毛利率": "...", "趋势": "上升|下降|平稳|未知"}],
 "上游依赖": [{"要素": "原材料/采购项/资金来源", "影响": "对成本或毛利的传导说明"}],
 "下游需求": [{"客群或场景": "...", "需求驱动": "..."}],
 "供应商集中度": "如'前五供应商占比 X%'；未披露则写'未披露'",
 "客户集中度": "同上",
 "行业与竞争": "报告自述的行业格局与竞争位置（注明为公司自述口径）",
 "估值观察因子": [{"因子": "具体可跟踪变量（如某原材料价格/某行业需求指标）", "方向": "上游成本|下游需求|政策|其他", "传导": "→毛利率 / →收入增速 等传导说明"}]}

业务分部 2-6 项、上游依赖/下游需求各 1-4 项、估值观察因子 2-5 项。"""


def build_business_profile_input(db: Session, symbol: str, market: str) -> Dict[str, Any]:
    """合成输入：近 3 份 digest 的业务字段 + 最新业务概要节选 + 近 3 年核心科目。"""
    digests = load_report_digests(db, symbol, market, limit=3)
    digest_slices = [
        {
            "end_date": entry.get("end_date"),
            "业务分部占比": (entry.get("digest") or {}).get("业务分部占比"),
            "上下游与产业链": (entry.get("digest") or {}).get("上下游与产业链"),
            "主营收入结构": (entry.get("digest") or {}).get("主营收入结构"),
        }
        for entry in digests
    ]
    # 取最新一份**成功且含 business 节**的节选：只看最新一行会让最新报告
    # 抽取失败时，已有的上一期业务概要完全不进画像输入
    business_section = ""
    section_rows = (
        db.query(SecurityProfileData)
        .filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == market,
            SecurityProfileData.dataset == "report_section",
            SecurityProfileData.payload["extract_status"].as_string() == "ok",
        )
        .order_by(SecurityProfileData.period_key.desc())
        .limit(BUSINESS_SECTION_LOOKBACK)
        .all()
    )
    for row in section_rows:
        candidate = ((row.payload or {}).get("sections") or {}).get("business") or ""
        if candidate.strip():
            business_section = candidate[:10_000]
            break

    profile = load_symbol_profile(
        db, symbol, market,
        caps={"income": 3, "fina_indicator": 3, "daily_basic": 1},
    )
    return {
        "symbol": symbol,
        "market": market,
        "report_digest_slices": digest_slices,
        "business_section_excerpt": business_section,
        "financials": {
            "income": profile["datasets"].get("income", []),
            "fina_indicator": profile["datasets"].get("fina_indicator", []),
        },
        "source_end_date": digests[0]["end_date"] if digests else None,
    }


def parse_business_profile_output(content: str) -> Dict[str, Any]:
    """校验商业画像 JSON；非法抛 ValueError（确定性失败）。"""
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise ValueError(f"商业画像输出不是合法 JSON: {content[:200]}") from exc
    if not isinstance(data, dict):
        raise ValueError("商业画像输出必须是 JSON 对象")
    for field in PROFILE_FIELDS:
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f"商业画像缺少字段: {field}")
    for field, min_items, max_items, required_keys in PROFILE_ARRAY_SPECS:
        items = data.get(field)
        if not isinstance(items, list):
            raise ValueError(f"商业画像 {field} 必须是数组")
        if len(items) < min_items:
            raise ValueError(
                f"商业画像 {field} 至少需要 {min_items} 项，收到 {len(items)} 项"
            )
        for index, item in enumerate(items[:max_items]):
            if not isinstance(item, dict):
                raise ValueError(f"商业画像 {field}[{index}] 必须是对象")
            for key in required_keys:
                value = item.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"商业画像 {field}[{index}] 缺少字段: {key}")
        data[field] = items[:max_items]
    return {
        key: data[key]
        for key in (*PROFILE_FIELDS, *(spec[0] for spec in PROFILE_ARRAY_SPECS))
    }


def input_fingerprint(payload_input: Dict[str, Any]) -> str:
    """画像输入的内容指纹。

    只比 source_end_date 会让同报告期的内容更新（修订版重新抽取、摘要重生成、
    人工纠正）永远命中旧缓存——画像与实际源数据长期不一致。
    """
    material = {
        "digests": payload_input.get("report_digest_slices"),
        "section": payload_input.get("business_section_excerpt"),
        "financials": payload_input.get("financials"),
    }
    serialized = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def ensure_business_profile(db: Session, symbol: str, market: str) -> Optional[Dict[str, Any]]:
    """输入内容指纹变化时重生成；否则命中缓存。失败返回 None 不上抛。"""
    row = (
        db.query(SecurityProfileData)
        .filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == market,
            SecurityProfileData.dataset == "business_profile",
            SecurityProfileData.period_key == "current",
        )
        .first()
    )
    payload_input = build_business_profile_input(db, symbol, market)
    if not payload_input["report_digest_slices"] and not payload_input["business_section_excerpt"]:
        return (row.payload or {}).get("profile") if row else None  # 无源数据不生成

    fingerprint = input_fingerprint(payload_input)
    stored = row.payload if row else None
    if stored and stored.get("status") == "ok":
        if stored.get("input_fingerprint") == fingerprint:
            return stored.get("profile")  # 缓存命中（输入内容未变）

    try:
        completion = chat_completion(
            [
                {"role": "system", "content": BUSINESS_PROFILE_SYSTEM_PROMPT},
                {"role": "user", "content": (
                    "请基于下方 JSON 数据生成商业画像（严格按 system 约定输出 JSON）：\n\n"
                    "```json\n"
                    + json.dumps(payload_input, ensure_ascii=False, separators=(",", ":"),
                                 default=str)
                    + "\n```"
                )},
            ],
            response_format={"type": "json_object"},
        )
        profile = parse_business_profile_output(completion["content"])
        upsert_profile_row(db, symbol, market, "business_profile", "current", {
            "status": "ok",
            "profile": profile,
            "source_end_date": payload_input["source_end_date"],
            "input_fingerprint": fingerprint,
            "model": completion.get("model"),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        db.commit()
        return profile
    except Exception as exc:  # 商业画像失败不阻断主分析
        db.rollback()
        logger.warning("商业画像生成失败 %s/%s: %s", symbol, market, str(exc)[:200])
        return (stored or {}).get("profile") if stored else None


# ---------------------------------------------------------------------------
# 同业名单（Tushare stock_basic 行业分类；进程内缓存）
# ---------------------------------------------------------------------------

_stock_basic_cache: List[Dict[str, Any]] = []
_stock_basic_loaded_at = 0.0
_stock_basic_lock = threading.Lock()
_STOCK_BASIC_TTL_SECONDS = 24 * 3600


def _load_stock_basic() -> List[Dict[str, Any]]:
    global _stock_basic_loaded_at
    with _stock_basic_lock:
        if _stock_basic_cache and time.monotonic() - _stock_basic_loaded_at < _STOCK_BASIC_TTL_SECONDS:
            return _stock_basic_cache
        from .stock_price_service import tushare_query

        df = tushare_query(
            "stock_basic", list_status="L", fields="ts_code,symbol,name,industry"
        )
        _stock_basic_cache.clear()
        _stock_basic_cache.extend(df.to_dict("records"))
        _stock_basic_loaded_at = time.monotonic()
        logger.info("stock_basic 行业目录加载：%d 条", len(_stock_basic_cache))
        return _stock_basic_cache


def _us_peers_by_sic(symbol: str) -> List[Dict[str, Any]]:
    """美股同业：EDGAR submissions 取本司 SIC → browse-edgar 同码公司 CIK
    → company_tickers 反查 ticker/注册名（无 ticker 的非上市 filer 不入名单）。"""
    from .report_fetchers import (
        edgar_lookup,
        edgar_reverse_lookup,
        edgar_same_sic_companies,
        edgar_submissions,
    )

    lookup = edgar_lookup(symbol)
    if not lookup:
        return []
    submissions = edgar_submissions(lookup["cik"])
    sic = str(submissions.get("sic") or "").strip()
    if not sic:
        return []
    peers = []
    for company in edgar_same_sic_companies(sic):
        if company["cik"] == lookup["cik"]:
            continue
        listed = edgar_reverse_lookup(company["cik"])
        if not listed:
            continue  # 有 10-K 但无 ticker 的 filer，对同业比较无意义
        peers.append({
            "symbol": listed["symbol"],
            "name": listed["title"],
            "industry": f"SIC {sic}",
        })
        if len(peers) >= PEER_LIST_CAP:
            break
    return peers


def _peer_list_row(db: Session, symbol: str, market: str):
    return (
        db.query(SecurityProfileData)
        .filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == market,
            SecurityProfileData.dataset == "peer_list",
            SecurityProfileData.period_key == "current",
        )
        .first()
    )


def _peer_list_cache_hit(row) -> Optional[List[Dict[str, Any]]]:
    """TTL 内的缓存名单；未命中返回 None。

    同行业名单几个月才变一次，而原实现只在**异常时**才回退缓存——正常路径
    每次分析都重新生成，每只美股白打 2 次 EDGAR。
    """
    if row is None or not (row.payload or {}).get("peers"):
        return None
    fetched_at = getattr(row, "fetched_at", None)
    if fetched_at is None:
        return None
    age = datetime.now(timezone.utc) - fetched_at.replace(tzinfo=timezone.utc)
    if age.total_seconds() < PEER_LIST_TTL_DAYS * 86400:
        return list(row.payload.get("peers") or [])
    return None


def ensure_peer_list(db: Session, symbol: str, market: str) -> List[Dict[str, Any]]:
    """同行业名单（客观数据）：TTL 内命中缓存，否则生成/刷新；失败回退缓存或空。

    A股=Tushare stock_basic 行业分类；美股=EDGAR SIC 同码经 company_tickers
    反查 ticker；港股无同业源。
    """
    if market == "美股":
        row = _peer_list_row(db, symbol, market)
        cached = _peer_list_cache_hit(row)
        if cached is not None:
            return cached
        try:
            peers = _us_peers_by_sic(symbol)
            if peers:
                upsert_profile_row(db, symbol, market, "peer_list", "current", {
                    "industry": peers[0]["industry"], "peers": peers,
                })
                db.commit()
            return peers
        except Exception as exc:
            db.rollback()
            logger.warning("美股同业名单获取失败 %s: %s", symbol, str(exc)[:150])
            return (row.payload or {}).get("peers", []) if row else []
    if market != "A股":
        return []
    row = _peer_list_row(db, symbol, market)
    cached = _peer_list_cache_hit(row)
    if cached is not None:
        return cached
    try:
        listing = _load_stock_basic()
        industry = next(
            (str(entry.get("industry") or "")
             for entry in listing if str(entry.get("symbol")) == symbol),
            "",
        )
        if not industry:
            return (row.payload or {}).get("peers", []) if row else []
        peers = [
            {"symbol": str(entry.get("symbol")), "name": entry.get("name"),
             "industry": industry}
            for entry in listing
            if str(entry.get("industry") or "") == industry
            and str(entry.get("symbol")) != symbol
        ][:PEER_LIST_CAP]
        upsert_profile_row(db, symbol, market, "peer_list", "current", {
            "industry": industry, "peers": peers,
        })
        db.commit()
        return peers
    except Exception as exc:
        db.rollback()
        logger.warning("同业名单获取失败 %s: %s", symbol, str(exc)[:150])
        return (row.payload or {}).get("peers", []) if row else []


def load_business_profile(db: Session, symbol: str, market: str) -> Dict[str, Any]:
    """详情页读取：{profile, peers, industry}。"""
    result: Dict[str, Any] = {"profile": None, "peers": [], "industry": None}
    for dataset, key in (("business_profile", "profile"), ("peer_list", None)):
        row = (
            db.query(SecurityProfileData)
            .filter(
                SecurityProfileData.symbol == symbol,
                SecurityProfileData.market == market,
                SecurityProfileData.dataset == dataset,
                SecurityProfileData.period_key == "current",
            )
            .first()
        )
        if not row:
            continue
        payload = row.payload or {}
        if dataset == "business_profile" and payload.get("status") == "ok":
            result["profile"] = payload.get("profile")
        elif dataset == "peer_list":
            result["peers"] = payload.get("peers", [])
            result["industry"] = payload.get("industry")
    return result
