"""RELISTING 规则的 payload.new_symbol 按新市场归一（补 0036 旧版，#312 复审）

Revision ID: 20260929_0039
Revises: 20260929_0038
Create Date: 2026-09-29

0036 的第一版对「归一后撞唯一键」的规则整行跳过，连 RELISTING 的 payload.new_symbol 也没有
归一；#312 改正了 0036，但已经跑过旧版 0036 的库不会重跑它。这里对全部 RELISTING 行再做一次
payload 归一（与 services/symbol_normalization.py 同规则，迁移内联一份）：已归一的行原样不动，
所以在跑过新版 0036 的库上是空操作。payload 不参与唯一键，不会撞键。数据迁移，downgrade 不回滚。
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0039"
down_revision: Union[str, None] = "20260929_0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _normalize(symbol, market):
    text = (symbol or "").strip().upper()
    if market == "港股" and text.isdigit() and len(text) <= 5:
        return text.zfill(5)
    return text


def upgrade() -> None:
    bind = op.get_bind()
    rows = (
        bind.execute(
            sa.text("SELECT id, payload FROM security_rules WHERE rule_type = 'RELISTING'")
        )
        .mappings()
        .all()
    )
    for row in rows:
        payload = row["payload"]
        if not isinstance(payload, dict) or not payload.get("new_symbol"):
            continue
        normalized = _normalize(payload["new_symbol"], payload.get("new_market"))
        if normalized == payload["new_symbol"]:
            continue
        bind.execute(
            sa.text("UPDATE security_rules SET payload = CAST(:payload AS jsonb) WHERE id = :id"),
            {
                "id": row["id"],
                "payload": json.dumps({**payload, "new_symbol": normalized}, ensure_ascii=False),
            },
        )


def downgrade() -> None:
    pass
