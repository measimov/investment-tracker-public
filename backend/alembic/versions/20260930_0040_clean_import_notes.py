"""导入产物备注去掉机器追溯字段（#286）

Revision ID: 20260930_0040
Revises: 20260929_0039
Create Date: 2026-09-30

三家导入器此前把 `scope=stock; row=1; 业务=证券买入; source_cny_price=…; row_hash=…` 这类追溯字段
写进交易/公司行动/现金事件的「备注」——用户可见可编辑，移动端卡片整段展示。追溯信息本来就在
来源流水（BrokerFundFlow / IbkrActivityFlow）上并有链接可查。导入器已改为只写人能读懂的一句
（broker_import_common.import_note），这里清理存量：

- 只动备注里确实带机器字段的行（row_hash= / scope= / row=数字 / source_cny_price= 等），用户自己
  写的备注不会命中；
- 按「; 」切段：机器键值段整段去掉，「业务=X」留下 X，段内夹带的 `row=N` / `row_hash=…` 去掉，
  重复段去重，最后用「 · 」连接；
- IBKR 转板合成交易的备注带逻辑标记 synthetic_relisting_transfer（代码按它识别），整条跳过。

数据迁移，downgrade 不回滚。
"""

import re
from typing import Optional, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0040"
down_revision: Union[str, None] = "20260929_0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("transactions", "corporate_actions", "cash_events")
MACHINE_KEYS = {
    "scope",
    "row",
    "row_hash",
    "source",
    "source_cny_price",
    "source_cny_amount",
    "settlement_rate",
    "raw_symbol",
    "dividend_currency",
    "account",
    "type",
    "gross",
    "net",
    "流水号",
    "合同编号",
}
MACHINE_MARKER = re.compile(r"\brow_hash=|\bscope=|\brow=\d|\bsource_cny_price=|流水号=")
INLINE_TRACE = re.compile(r"\s*\b(?:row|row_hash)=\S+")
KEY_VALUE = re.compile(r"^([A-Za-z_一-鿿]+)=(.*)$")
RELISTING_MARKER = "synthetic_relisting_transfer"


def clean_note(note: Optional[str]) -> Optional[str]:
    if not note or RELISTING_MARKER in note or not MACHINE_MARKER.search(note):
        return note
    kept = []
    for segment in re.split(r";\s*", note):
        segment = INLINE_TRACE.sub("", segment.strip()).strip()
        if not segment:
            continue
        match = KEY_VALUE.match(segment)
        if match:
            key, value = match.groups()
            if key in MACHINE_KEYS:
                continue
            if key == "业务":
                segment = value.strip()
        if segment == "IBKR Activity Statement":
            segment = "IBKR对账单导入"
        if segment and segment not in kept:
            kept.append(segment)
    return " · ".join(kept) or None


def upgrade() -> None:
    conn = op.get_bind()
    for table in TABLES:
        rows = conn.execute(
            sa.text(
                f"SELECT id, notes FROM {table} "
                "WHERE notes ~ '(row_hash=|scope=|row=[0-9]|source_cny_price=|流水号=)'"
            )
        ).fetchall()
        for row in rows:
            cleaned = clean_note(row.notes)
            if cleaned != row.notes:
                conn.execute(
                    sa.text(f"UPDATE {table} SET notes = :notes WHERE id = :id"),
                    {"notes": cleaned, "id": row.id},
                )


def downgrade() -> None:
    pass
