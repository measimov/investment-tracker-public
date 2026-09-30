"""特例规则的证券代码按手工入口口径归一（#278）

Revision ID: 20260929_0036
Revises: 20260929_0035
Create Date: 2026-09-29

此前规则创建只做 upper()，港股填「700」存成「700」；读取端（除 INDUSTRY 外）按原样匹配，
EXCLUDE / CASH_MANAGEMENT / NAME_OVERRIDE / PRICE_GAP_EXEMPTION 规则静默失效。新写入已在
schema 里经 normalize_manual_symbol 归一，这里把存量行改成同一形态：
- 去首尾空白、大写；港股纯数字代码补零到 5 位（与 services/symbol_normalization.py 同规则，
  迁移里内联一份，不依赖应用代码的将来版本）
- RELISTING 的 payload.new_symbol 按 payload.new_market 归一
- CMB_CASH_BUSINESS 的 symbol 是中文业务名，不动

归一后与同用户同类型的另一行撞唯一键时**不改、不删**，只在迁移输出里列出，由用户在界面上
删掉重复的那条（两条规则内容可能不同，自动合并会丢信息）。数据迁移，downgrade 不回滚。
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0036"
down_revision: Union[str, None] = "20260929_0035"
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
            sa.text(
                "SELECT id, user_id, rule_type, symbol, market, payload FROM security_rules "
                "WHERE rule_type <> 'CMB_CASH_BUSINESS' ORDER BY id"
            )
        )
        .mappings()
        .all()
    )
    taken = {(r["user_id"], r["rule_type"], r["symbol"], r["market"]) for r in rows}
    conflicts = []
    for row in rows:
        new_symbol = _normalize(row["symbol"], row["market"])
        payload = row["payload"]
        new_payload = payload
        if row["rule_type"] == "RELISTING" and isinstance(payload, dict):
            relisted = _normalize(payload.get("new_symbol"), payload.get("new_market"))
            if relisted and relisted != payload.get("new_symbol"):
                new_payload = {**payload, "new_symbol": relisted}
        # payload 不参与唯一键：撞键只挡 symbol 列，RELISTING 的新代码照样归一
        if new_payload is not payload:
            bind.execute(
                sa.text(
                    "UPDATE security_rules SET payload = CAST(:payload AS jsonb) WHERE id = :id"
                ),
                {"id": row["id"], "payload": json.dumps(new_payload, ensure_ascii=False)},
            )
        if new_symbol != row["symbol"]:
            key = (row["user_id"], row["rule_type"], new_symbol, row["market"])
            if key in taken:
                conflicts.append(
                    f"id={row['id']} user={row['user_id']} {row['rule_type']} "
                    f"{row['symbol']!r}→{new_symbol!r} {row['market']}"
                )
                continue
            taken.discard((row["user_id"], row["rule_type"], row["symbol"], row["market"]))
            taken.add(key)
            bind.execute(
                sa.text("UPDATE security_rules SET symbol = :symbol WHERE id = :id"),
                {"id": row["id"], "symbol": new_symbol},
            )
    if conflicts:
        print("security_rules 归一后与已有规则重复、未修改（请在界面删掉重复的一条）：")
        for line in conflicts:
            print("  " + line)


def downgrade() -> None:
    pass
