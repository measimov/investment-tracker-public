"""`security_profile_data` 单行读写（#281）：按 (symbol, market, dataset, period_key) 取一行 /
原子 upsert 一行。

此前放在 security_profile_service（档案注册表、EDGAR 透视、同步、格雷厄姆取数混在一起），
报告摘要、港股报表、ADS、股息表格、尾部同步等十来个模块只为这两个函数就依赖整个档案服务。
注册表驱动的批量写入 `upsert_profile_rows` 依赖数据集注册表，仍留在 security_profile_service。
"""

from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..models.security_profile import SecurityProfileData


def load_profile_row(
    db: Session, symbol: str, market: str, dataset: str, period_key: str
) -> Optional[SecurityProfileData]:
    return (
        db.query(SecurityProfileData)
        .filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == market,
            SecurityProfileData.dataset == dataset,
            SecurityProfileData.period_key == period_key,
        )
        .first()
    )


def upsert_profile_row(
    db: Session,
    symbol: str,
    market: str,
    dataset: str,
    period_key: str,
    payload: Dict[str, Any],
) -> None:
    """显式 period_key 的单行原子 upsert（报告节选/摘要等非注册表数据集用）。"""
    stmt = pg_insert(SecurityProfileData).values(
        [
            {
                "symbol": symbol,
                "market": market,
                "dataset": dataset,
                "period_key": period_key[:40],
                "payload": payload,
            }
        ]
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_security_profile_identity",
        set_={"payload": stmt.excluded.payload, "fetched_at": func.now()},
    )
    db.execute(stmt)
