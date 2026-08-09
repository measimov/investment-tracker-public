from typing import Type

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models.broker_account import BrokerAccount
from ..models.broker_fund_flow import BrokerFundFlow
from ..models.ibkr_activity_flow import IbkrActivityFlow


def get_owned_record(
    db: Session,
    model: Type,
    record_id: int,
    user_id: int,
    not_found_detail: str,
):
    record = (
        db.query(model)
        .filter(
            model.id == record_id,
            model.user_id == user_id,
        )
        .first()
    )
    if record is None:
        raise HTTPException(status_code=404, detail=not_found_detail)
    return record


def validate_owned_references(db: Session, user_id: int, data: dict) -> None:
    """请求体里出现的引用 id 必须归属当前用户（否则 404）。

    此前 transactions 与 corporate_actions 各存一份逐字相同的实现（issue #137）。
    """
    references = {
        "broker_account_id": (BrokerAccount, "Broker account not found"),
    }
    for field, (model, detail) in references.items():
        record_id = data.get(field)
        if record_id is not None:
            get_owned_record(db, model, record_id, user_id, detail)


def ensure_record_is_mutable(
    db: Session,
    user_id: int,
    record,
    *,
    source_link_field: str,
    detail: str,
) -> None:
    """券商导入产物只读：带批次链接或被来源流水引用的记录一律 409。

    transactions 与 corporate_actions 的双胞胎守卫收敛为一份（issue #137）；
    差异只有来源流水上的链接列名（transaction_id / corporate_action_id）
    与文案里的名词。
    """
    if record.import_batch_id is not None:
        raise HTTPException(status_code=409, detail=detail)
    broker_source = (
        db.query(BrokerFundFlow.id)
        .filter(
            BrokerFundFlow.user_id == user_id,
            getattr(BrokerFundFlow, source_link_field) == record.id,
        )
        .first()
    )
    ibkr_source = (
        db.query(IbkrActivityFlow.id)
        .filter(
            IbkrActivityFlow.user_id == user_id,
            getattr(IbkrActivityFlow, source_link_field) == record.id,
        )
        .first()
    )
    if broker_source or ibkr_source:
        raise HTTPException(status_code=409, detail=detail)
