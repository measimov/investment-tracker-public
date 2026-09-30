"""响应模型不带输入约束（#283）：库里任何一行都能如实列出，不因一行脏数据整页 500。"""

import typing
from datetime import date
from decimal import Decimal

import httpx
import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.cash_event import CashEvent
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.transaction import Transaction
from app.models.user import User

from .helpers import reset_tables

RESET_MODELS = (Holding, CorporateAction, CashEvent, Transaction)


def _models_in(annotation, seen):
    if typing.get_origin(annotation) is not None:
        for arg in typing.get_args(annotation):
            yield from _models_in(arg, seen)
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in seen:
            return
        seen.add(annotation)
        yield annotation
        for field in annotation.model_fields.values():
            yield from _models_in(field.annotation, seen)


def test_response_models_carry_no_input_constraints():
    """所有 response_model（含嵌套模型）的字段都不带 gt/ge/max_length/pattern 等约束。

    输入约束属于写入口；响应模型请用 `schemas.read_models.read_model(Base)` 派生。
    """
    seen: set = set()
    offenders = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.response_model is None:
            continue
        for model in _models_in(route.response_model, seen):
            constrained = [name for name, field in model.model_fields.items() if field.metadata]
            if constrained:
                offenders.append(f"{model.__name__}: {constrained}")
    assert offenders == []


@pytest.fixture
def seeded():
    password = "read-models-password"
    db = SessionLocal()
    try:
        reset_tables(db, RESET_MODELS)
        user = db.query(User).filter(User.username == "demo").one()
        original = user.hashed_password
        user.hashed_password = get_password_hash(password)
        # 零成本转入这类行绕过了写入口校验（导入器/脚本/迁移写入）：price=0、超长代码
        db.add(
            Transaction(
                user_id=user.id,
                symbol="600000",
                name="浦发银行",
                market="A股",
                transaction_type="BUY",
                quantity=Decimal("100"),
                price=Decimal("0"),
                fee=Decimal("0"),
                transaction_date=date(2026, 1, 1),
                currency="CNY",
            )
        )
        db.commit()
        yield password
        user.hashed_password = original
        db.commit()
        reset_tables(db, RESET_MODELS)
    finally:
        db.close()


@pytest.mark.anyio
async def test_transaction_list_shows_rows_that_fail_input_constraints(seeded):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        token = (
            await client.post("/api/auth/token", json={"username": "demo", "password": seeded})
        ).json()["access_token"]
        response = await client.get(
            "/api/transactions", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
    rows = response.json()
    assert [Decimal(row["price"]) for row in rows] == [Decimal("0")]
