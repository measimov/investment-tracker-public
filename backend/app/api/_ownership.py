from typing import Type

from fastapi import HTTPException
from sqlalchemy.orm import Session


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
