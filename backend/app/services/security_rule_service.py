"""账本特例规则读取层（security_rules，issue #82）。

各消费方按类型取规则；EXCLUDE 的两个 getter 保持原
excluded_security_service 的签名，导入器与对账比对零改动。
无缓存：家庭规模下每次调用一查即可。
"""

from datetime import date
from typing import Any, Dict, List, Optional, Set, Tuple

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
