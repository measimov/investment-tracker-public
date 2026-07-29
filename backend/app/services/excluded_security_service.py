"""排除清单查询助手：导入侧按 symbol 匹配，对账比对侧按 (symbol, market) 精确匹配。"""

from typing import Set, Tuple

from sqlalchemy.orm import Session

from ..models.excluded_security import ExcludedSecurity


def get_excluded_symbols(db: Session, user_id: int) -> Set[str]:
    """券商对账单导入用：对账单代码空间内 symbol 无歧义，按 symbol 匹配。"""
    rows = (
        db.query(ExcludedSecurity.symbol)
        .filter(ExcludedSecurity.user_id == user_id)
        .all()
    )
    return {row[0] for row in rows}


def get_excluded_keys(db: Session, user_id: int) -> Set[Tuple[str, str]]:
    """对账比对用：与持仓/比对键同构的 (symbol, market) 精确匹配。"""
    rows = (
        db.query(ExcludedSecurity.symbol, ExcludedSecurity.market)
        .filter(ExcludedSecurity.user_id == user_id)
        .all()
    )
    return {(row[0], row[1]) for row in rows}
