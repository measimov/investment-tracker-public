from typing import Iterable, List, Set, Type

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
        "broker_account_id": (BrokerAccount, "券商账户不存在"),
    }
    for field, (model, detail) in references.items():
        record_id = data.get(field)
        if record_id is not None:
            get_owned_record(db, model, record_id, user_id, detail)


# 券商来源流水上指向各类账本记录的链接列：被其中任何一列引用即为导入产物（只读）
SOURCE_LINK_COLUMNS = {
    "transaction": (
        (BrokerFundFlow, ("transaction_id",)),
        (IbkrActivityFlow, ("transaction_id",)),
    ),
    "corporate_action": (
        (BrokerFundFlow, ("corporate_action_id",)),
        (IbkrActivityFlow, ("corporate_action_id",)),
    ),
    "cash_event": (
        (BrokerFundFlow, ("cash_event_id",)),
        (IbkrActivityFlow, ("cash_event_id", "fx_quote_cash_event_id", "fx_fee_cash_event_id")),
    ),
}


def linked_record_ids(db: Session, user_id: int, kind: str, ids: Iterable[int]) -> Set[int]:
    """ids 中被该用户的券商来源流水引用的那些（每个链接列一次 IN 查询，不逐行查）。"""
    ids = [record_id for record_id in ids if record_id is not None]
    linked: Set[int] = set()
    if not ids:
        return linked
    for model, columns in SOURCE_LINK_COLUMNS[kind]:
        for name in columns:
            column = getattr(model, name)
            linked.update(
                row[0]
                for row in db.query(column)
                .filter(model.user_id == user_id, column.in_(ids))
                .distinct()
            )
    return linked


def is_imported(record, linked: Set[int]) -> bool:
    """导入产物：带导入批次，或被来源流水引用（无批次的历史回填也算）。"""
    return getattr(record, "import_batch_id", None) is not None or record.id in linked


def annotate_read_only(db: Session, user_id: int, kind: str, records: List) -> List:
    """给响应补 `read_only` 展示字段，与 ensure_record_is_mutable 同一判据（#283：此前交易、
    公司行动、现金事件三份实现，交易响应甚至没有这个标志，前端只看 import_batch_id——
    被来源流水链接但无批次的记录会显示「编辑」然后 409）。"""
    linked = linked_record_ids(
        db,
        user_id,
        kind,
        [r.id for r in records if getattr(r, "import_batch_id", None) is None],
    )
    for record in records:
        record.read_only = is_imported(record, linked)
    return records


def ensure_record_is_mutable(db: Session, user_id: int, record, *, kind: str, detail: str) -> None:
    """券商导入产物只读：带批次链接或被来源流水引用的记录一律 409。"""
    if getattr(record, "import_batch_id", None) is not None:
        raise HTTPException(status_code=409, detail=detail)
    if linked_record_ids(db, user_id, kind, [record.id]):
        raise HTTPException(status_code=409, detail=detail)
