"""PUT /api/transactions/{id}：空白必填字段必须在写库前被拒。

复审指出 TransactionUpdate 没有复用 TransactionBase 的非空校验（它不继承
TransactionBase），于是 symbol=" " 这类值会先 setattr + commit，直到用
TransactionResponse 序列化响应时才失败——用户看到 500，而**脏数据已经落库**。

这里断言两件事：请求被拒（422 而非 500），且库里的行原封不动。
"""

from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.transaction import Transaction
from app.models.user import User

from .helpers import reset_tables

RESET_MODELS = (Holding, CorporateAction, Transaction)


@pytest.fixture
def seeded():
    password = "update-blank-field-password"
    db = SessionLocal()
    try:
        reset_tables(db, RESET_MODELS)
        user = db.query(User).filter(User.username == "demo").one()
        original = user.hashed_password
        user.hashed_password = get_password_hash(password)
        txn = Transaction(
            user_id=user.id, symbol="600000", name="浦发银行", market="A股",
            transaction_type="BUY", quantity=Decimal("100"), price=Decimal("10"),
            fee=Decimal("1"), transaction_date=date(2026, 1, 1), currency="CNY",
        )
        db.add(txn)
        db.commit()
        db.refresh(txn)
        yield password, txn.id
        user.hashed_password = original
        db.commit()
        reset_tables(db, RESET_MODELS)
    finally:
        db.close()


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload,field",
    [
        ({"symbol": ""}, "symbol"),
        ({"symbol": "   "}, "symbol"),
        ({"market": ""}, "market"),
        ({"market": " "}, "market"),
        ({"currency": ""}, "currency"),
        ({"currency": "  "}, "currency"),
        # 显式 null：validator 曾用 `if value is None: return value` 放行，
        # 于是它进 update_data → setattr → commit，直到响应序列化才 500。
        ({"symbol": None}, "symbol"),
        ({"market": None}, "market"),
        ({"currency": None}, "currency"),
        # 第四轮复审：其余业务必填字段的显式 null 同样会落库后 500
        ({"transaction_type": None}, "transaction_type"),
        ({"quantity": None}, "quantity"),
        ({"price": None}, "price"),
        ({"fee": None}, "fee"),
        ({"transaction_date": None}, "transaction_date"),
    ],
)
async def test_blank_field_update_is_rejected_before_commit(seeded, payload, field):
    password, txn_id = seeded

    async with _client() as client:
        token = (await client.post(
            "/api/auth/token", json={"username": "demo", "password": password}
        )).json()["access_token"]
        response = await client.put(
            f"/api/transactions/{txn_id}",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )

    # 422（请求校验）而不是 500（提交后序列化失败）
    assert response.status_code == 422, (
        f"{field} 空白应在写库前被拒，实际 {response.status_code}：{response.text[:200]}"
    )

    # 关键：库里的行必须原封不动
    db = SessionLocal()
    try:
        row = db.query(Transaction).filter(Transaction.id == txn_id).one()
        assert row.symbol == "600000"
        assert row.market == "A股"
        assert row.currency == "CNY"
    finally:
        db.close()


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["notes", "name"])
async def test_nullable_fields_still_accept_explicit_null(seeded, field):
    """可空字段传 null 是合法的清空操作——非空校验不得误伤它们。"""
    password, txn_id = seeded

    async with _client() as client:
        token = (await client.post(
            "/api/auth/token", json={"username": "demo", "password": password}
        )).json()["access_token"]
        response = await client.put(
            f"/api/transactions/{txn_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={field: None},
        )

    assert response.status_code == 200, response.text

    db = SessionLocal()
    try:
        assert getattr(db.query(Transaction).filter(Transaction.id == txn_id).one(), field) is None
    finally:
        db.close()


@pytest.mark.anyio
async def test_partial_update_still_works(seeded):
    """部分更新不得被非空校验误伤——只改 price 时其余字段未传。"""
    password, txn_id = seeded

    async with _client() as client:
        token = (await client.post(
            "/api/auth/token", json={"username": "demo", "password": password}
        )).json()["access_token"]
        response = await client.put(
            f"/api/transactions/{txn_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"price": "12.5"},
        )

    assert response.status_code == 200, response.text
    assert Decimal(response.json()["price"]) == Decimal("12.5")


@pytest.mark.anyio
async def test_update_strips_surrounding_whitespace(seeded):
    """合法值带首尾空白时应 strip 后入库。"""
    password, txn_id = seeded

    async with _client() as client:
        token = (await client.post(
            "/api/auth/token", json={"username": "demo", "password": password}
        )).json()["access_token"]
        response = await client.put(
            f"/api/transactions/{txn_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"market": "  A股  "},
        )

    assert response.status_code == 200, response.text

    db = SessionLocal()
    try:
        assert db.query(Transaction).filter(Transaction.id == txn_id).one().market == "A股"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 守护：防止这一族缺陷再出现第五轮
#
# 前四轮复审是同一个缺陷被逐个字段发现的：symbol/market/currency 的空串 →
# 它们的显式 null → transaction_type/quantity/price/fee/transaction_date 的
# 显式 null。每轮都只补了被点名的那几个字段，下一轮又漏。
#
# 这两条用例把判据变成**自动推导**：TransactionUpdate 的每个字段都必须在
# TRANSACTION_REQUIRED_FIELDS 或 TRANSACTION_NULLABLE_FIELDS 里显式登记，
# 且必填字段必须真的拒绝 null。新增字段忘了归类就会直接报红。
# ---------------------------------------------------------------------------


def test_every_update_field_is_explicitly_classified():
    """新增字段必须显式归类为「业务必填」或「可空」，不得漏登记。"""
    from app.schemas.transaction import (
        TRANSACTION_NULLABLE_FIELDS,
        TRANSACTION_REQUIRED_FIELDS,
        TransactionUpdate,
    )

    classified = set(TRANSACTION_REQUIRED_FIELDS) | set(TRANSACTION_NULLABLE_FIELDS)
    actual = set(TransactionUpdate.model_fields)

    assert actual - classified == set(), (
        f"这些字段未归类，显式传 null 可能落库后 500：{sorted(actual - classified)}"
    )
    assert classified - actual == set(), (
        f"清单里有已不存在的字段：{sorted(classified - actual)}"
    )
    # 两个集合不得重叠，否则语义自相矛盾
    assert set(TRANSACTION_REQUIRED_FIELDS) & set(TRANSACTION_NULLABLE_FIELDS) == set()


def test_required_fields_reject_null_and_nullable_fields_accept_it():
    """逐字段验证分类真的生效——清单登记了但 validator 没挂上同样会红。"""
    from app.schemas.transaction import (
        TRANSACTION_NULLABLE_FIELDS,
        TRANSACTION_REQUIRED_FIELDS,
        TransactionUpdate,
    )

    for field in TRANSACTION_REQUIRED_FIELDS:
        with pytest.raises(Exception, match=r"(?s).*"):
            TransactionUpdate(**{field: None})

    for field in TRANSACTION_NULLABLE_FIELDS:
        # 不应抛异常
        TransactionUpdate(**{field: None})


def test_model_non_nullable_columns_are_all_required_in_schema():
    """ORM 里非空的列，schema 侧必须也当作必填——两边口径不得漂移。"""
    from app.models.transaction import Transaction
    from app.schemas.transaction import TRANSACTION_REQUIRED_FIELDS, TransactionUpdate

    # 服务端自管的列不在更新 schema 里，排除
    server_managed = {"id", "user_id", "created_at", "updated_at",
                      "import_batch_id", "linked_transaction_id"}
    non_nullable = {
        c.name for c in Transaction.__table__.columns
        if not c.nullable and c.name not in server_managed
    }
    updatable = non_nullable & set(TransactionUpdate.model_fields)

    missing = updatable - set(TRANSACTION_REQUIRED_FIELDS)
    assert missing == set(), (
        f"这些列在 DB 里非空，但 schema 未按必填处理：{sorted(missing)}"
    )
