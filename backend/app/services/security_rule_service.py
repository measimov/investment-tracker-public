"""账本特例规则读取层（security_rules，issue #82）。

各消费方按类型取规则；EXCLUDE 的两个 getter 保持原
excluded_security_service 的签名，导入器与对账比对零改动。
无缓存：家庭规模下每次调用一查即可。
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from ..models.security_rule import SecurityRule


def _rules(db: Session, user_id: int, rule_type: str) -> List[SecurityRule]:
    return (
        db.query(SecurityRule)
        .filter(SecurityRule.user_id == user_id, SecurityRule.rule_type == rule_type)
        .all()
    )


def get_excluded_symbols(db: Session, user_id: int) -> Set[str]:
    """现金管理排除标的（导入器侧：只归档不入账）。"""
    return {rule.symbol for rule in _rules(db, user_id, "EXCLUDE")}


def get_excluded_keys(db: Session, user_id: int) -> Set[Tuple[str, str]]:
    """排除标的 (symbol, market) 键（对账比对侧：双侧忽略）。"""
    return {
        (rule.symbol, rule.market)
        for rule in _rules(db, user_id, "EXCLUDE")
        if rule.market is not None
    }


def get_cash_management_symbols(db: Session, user_id: int) -> Set[str]:
    """现金管理产品标的（派息按 INTEREST 入账而非股息；与 EXCLUDE 互斥）。"""
    return {rule.symbol for rule in _rules(db, user_id, "CASH_MANAGEMENT")}


def get_relistings(db: Session, user_id: int) -> List[Dict[str, Any]]:
    """转板/重上市映射，形状与原 KNOWN_RELISTINGS 条目一致。"""
    result = []
    for rule in _rules(db, user_id, "RELISTING"):
        payload = rule.payload or {}
        result.append(
            {
                "old_symbol": rule.symbol,
                "old_market": rule.market,
                "old_currency": payload.get("old_currency"),
                "new_symbol": payload.get("new_symbol"),
                "new_market": payload.get("new_market"),
                "new_currency": payload.get("new_currency"),
                "name": payload.get("name"),
            }
        )
    return result


def get_name_overrides(db: Session, user_id: int) -> Dict[Tuple[str, str], str]:
    """手工名称覆盖表，形状与原 KNOWN_SECURITY_NAMES 一致。"""
    return {
        (rule.symbol, rule.market): (rule.payload or {}).get("name", "")
        for rule in _rules(db, user_id, "NAME_OVERRIDE")
        if rule.market is not None and (rule.payload or {}).get("name")
    }


def get_price_gap_exemptions(
    db: Session, user_id: int
) -> List[Tuple[str, str, date, Optional[date]]]:
    """行情缺口豁免 (symbol, market, start, end|None)；end=None 表示开放至今。"""
    result = []
    for rule in _rules(db, user_id, "PRICE_GAP_EXEMPTION"):
        payload = rule.payload or {}
        start_raw = payload.get("start_date")
        if rule.market is None or not start_raw:
            continue
        end_raw = payload.get("end_date")
        result.append(
            (
                rule.symbol,
                rule.market,
                date.fromisoformat(start_raw),
                date.fromisoformat(end_raw) if end_raw else None,
            )
        )
    return result


def get_cmb_cash_business_map(db: Session, user_id: int) -> Dict[str, str]:
    """招商现金业务名 → CashEvent 类型（symbol 列存业务名）。"""
    return {
        rule.symbol: (rule.payload or {}).get("event_type", "")
        for rule in _rules(db, user_id, "CMB_CASH_BUSINESS")
        if (rule.payload or {}).get("event_type")
    }


def get_ads_ratios(
    db: Session, user_id: int, symbols: Optional[Iterable[str]] = None
) -> Dict[str, Decimal]:
    """美股 ADS 换算比覆盖（symbol → 1 ADS 对应的普通股数）；畸形/非正值跳过。

    消费方是 ads_ratio_service.resolve_ads_ratio(s)：用户规则优先于 20-F 封面解析值。"""
    query = db.query(SecurityRule).filter(
        SecurityRule.user_id == user_id,
        SecurityRule.rule_type == "ADS_RATIO",
        SecurityRule.market == "美股",
    )
    if symbols is not None:
        wanted = sorted(set(symbols))
        if not wanted:
            return {}
        query = query.filter(SecurityRule.symbol.in_(wanted))
    result: Dict[str, Decimal] = {}
    for rule in query.all():
        try:
            ratio = Decimal(str((rule.payload or {}).get("ratio")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if ratio.is_finite() and ratio > 0:
            result[rule.symbol] = ratio
    return result


def get_industry_overrides(
    db: Session, user_id: int, keys: Optional[Iterable[Tuple[str, str]]] = None
) -> Dict[Tuple[str, str], str]:
    """手工行业分类 (symbol, market) → 行业名；优先于官方与东方财富来源。

    规则的代码只做了大写（security_rules API 口径），港股「700」这类未补零的写法
    按 normalize_manual_symbol 归一后再匹配账本键。"""
    from .symbol_normalization import normalize_manual_symbol

    wanted = set(keys) if keys is not None else None
    result: Dict[Tuple[str, str], str] = {}
    for rule in _rules(db, user_id, "INDUSTRY"):
        industry = str((rule.payload or {}).get("industry") or "").strip()
        if rule.market is None or not industry:
            continue
        key = (normalize_manual_symbol(rule.symbol, rule.market), rule.market)
        if wanted is None or key in wanted:
            result[key] = industry
    return result
