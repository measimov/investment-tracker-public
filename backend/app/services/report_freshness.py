"""「报告类数据」的最新落库时间：分析是否可能过期的判据（#283，由 api/security_profiles 下沉）。

详情页（单标的，`latest_report_data_at`）与持仓页 AI 标签（批量，`latest_report_data_at_batch`）
必须同一判定：摘要按 `load_report_digests` 的口径取最新 DIGEST_LOAD_LIMIT 份当前版本的 ok 行，
港股报表取全部抽取行的最新 fetched_at。此前批量版在路由里照抄了 `limit = 12` 与 `limit * 2`。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models.security_profile import SecurityProfileData
from .report_digest_service import DIGEST_LOAD_LIMIT, digest_versions_current
from .report_statement_service import EXTRACT_DATASET, STATEMENT_MARKETS


def latest_report_data_at(
    digests: List[Dict[str, Any]], statement_progress: Optional[Dict[str, Any]]
) -> Optional[str]:
    """最新一次「报告类数据」落库时间（财报摘要生成 / 港股报表抽取），ISO 串。

    详情页拿它与分析的 created_at 比较：分析早于它 = 分析没吃到最新的摘要/报表，
    标「可能过期」。只看报告类产物，不看 fetched_at（行情/指标的例行同步不意味着
    分析过期）。展示字段，零外呼。
    """
    candidates = [str(item["fetched_at"]) for item in digests if item.get("fetched_at")]
    if statement_progress and statement_progress.get("last_extracted_at"):
        candidates.append(str(statement_progress["last_extracted_at"]))
    if not candidates:
        return None
    return max(candidates, key=lambda text: datetime.fromisoformat(text))


def latest_report_data_at_batch(db: Session, keys: List[tuple]) -> Dict[tuple, Optional[str]]:
    """多个标的的 `latest_data_at`，与详情页 profile 端点**同一判定**（`latest_report_data_at`）：

    - 摘要：按 period_key 倒序看最新 2×DIGEST_LOAD_LIMIT 行、取 status=ok 且双版本当前的前 DIGEST_LOAD_LIMIT 份
      （即 `load_report_digests` 的口径）的 fetched_at；
    - 港股报表：全部抽取行的最新 fetched_at（`statement_progress.last_extracted_at` 的口径）。

    只取 payload 里判定要用的三个键，不把摘要正文拉出库（持仓几十只 × 每只十几份）。
    """
    if not keys:
        return {}
    wanted = set(keys)
    symbols = sorted({symbol for symbol, _ in wanted})
    markets = sorted({market for _, market in wanted})
    payload = SecurityProfileData.payload
    digest_rows = (
        db.query(
            SecurityProfileData.symbol,
            SecurityProfileData.market,
            SecurityProfileData.period_key,
            SecurityProfileData.fetched_at,
            payload["status"].as_string(),
            payload["extractor_version"].as_string(),
            payload["prompt_version"].as_string(),
        )
        .filter(
            SecurityProfileData.dataset == "report_digest",
            SecurityProfileData.symbol.in_(symbols),
            SecurityProfileData.market.in_(markets),
        )
        .order_by(SecurityProfileData.period_key.desc())
        .all()
    )
    digests: Dict[tuple, List[Dict[str, Any]]] = {}
    scanned: Dict[tuple, int] = {}
    limit = DIGEST_LOAD_LIMIT
    for symbol, market, _period, fetched_at, status, extractor, prompt in digest_rows:
        key = (symbol, market)
        if key not in wanted:
            continue
        scanned[key] = scanned.get(key, 0) + 1
        if scanned[key] > limit * 2 or len(digests.get(key, [])) >= limit:
            continue
        meta = {"extractor_version": extractor, "prompt_version": prompt}
        if status != "ok" or not digest_versions_current(meta):
            continue
        digests.setdefault(key, []).append(
            {"fetched_at": fetched_at.isoformat() if fetched_at else None}
        )

    statement_latest: Dict[tuple, Any] = {}
    hk_symbols = sorted({symbol for symbol, market in wanted if market in STATEMENT_MARKETS})
    if hk_symbols:
        for symbol, market, latest in (
            db.query(
                SecurityProfileData.symbol,
                SecurityProfileData.market,
                func.max(SecurityProfileData.fetched_at),
            )
            .filter(
                SecurityProfileData.dataset == EXTRACT_DATASET,
                SecurityProfileData.symbol.in_(hk_symbols),
                SecurityProfileData.market.in_(list(STATEMENT_MARKETS)),
            )
            .group_by(SecurityProfileData.symbol, SecurityProfileData.market)
            .all()
        ):
            if latest is not None:
                statement_latest[(symbol, market)] = latest

    result: Dict[tuple, Optional[str]] = {}
    for key in wanted:
        latest = statement_latest.get(key)
        progress = {"last_extracted_at": latest.isoformat()} if latest else None
        result[key] = latest_report_data_at(digests.get(key, []), progress)
    return result
