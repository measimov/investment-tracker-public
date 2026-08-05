"""security_profile_data 数据集白名单扩充三大报表

Revision ID: 20260803_0008
Revises: 20260802_0007
Create Date: 2026-08-03

CheckConstraint 变更 alembic autogenerate 检测不到，手写迁移：重建
ck_security_profile_dataset，加入 income / balancesheet / cashflow。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260803_0008"
down_revision: Union[str, None] = "20260802_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_DATASETS = (
    "'fina_indicator', 'forecast', 'express', 'daily_basic', "
    "'dividend_history', 'fina_audit', 'pledge_stat', 'stk_holdertrade'"
)
_NEW_DATASETS = _OLD_DATASETS + ", 'income', 'balancesheet', 'cashflow'"


def upgrade() -> None:
    op.drop_constraint(
        "ck_security_profile_dataset", "security_profile_data", type_="check"
    )
    op.create_check_constraint(
        "ck_security_profile_dataset",
        "security_profile_data",
        f"dataset IN ({_NEW_DATASETS})",
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM security_profile_data "
        "WHERE dataset IN ('income', 'balancesheet', 'cashflow')"
    )
    op.drop_constraint(
        "ck_security_profile_dataset", "security_profile_data", type_="check"
    )
    op.create_check_constraint(
        "ck_security_profile_dataset",
        "security_profile_data",
        f"dataset IN ({_OLD_DATASETS})",
    )
