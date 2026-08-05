"""security_profile_data 白名单扩充：财报节选/摘要/商业画像/同业/港美股数据集

Revision ID: 20260804_0009
Revises: 20260803_0008
Create Date: 2026-08-04

CheckConstraint 变更 alembic autogenerate 检测不到，手写迁移（模板同 0008）。
一次性扩齐后续各阶段所需数据集，避免多次重建约束。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260804_0009"
down_revision: Union[str, None] = "20260803_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_DATASETS = (
    "'fina_indicator', 'forecast', 'express', 'daily_basic', "
    "'dividend_history', 'fina_audit', 'pledge_stat', 'stk_holdertrade', "
    "'income', 'balancesheet', 'cashflow'"
)
_ADDED = (
    "'report_section', 'report_digest', 'business_profile', 'peer_list', "
    "'edgar_companyfacts', 'yahoo_fundamentals'"
)
_NEW_DATASETS = _OLD_DATASETS + ", " + _ADDED


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
        "DELETE FROM security_profile_data WHERE dataset IN ("
        "'report_section', 'report_digest', 'business_profile', 'peer_list', "
        "'edgar_companyfacts', 'yahoo_fundamentals')"
    )
    op.drop_constraint(
        "ck_security_profile_dataset", "security_profile_data", type_="check"
    )
    op.create_check_constraint(
        "ck_security_profile_dataset",
        "security_profile_data",
        f"dataset IN ({_OLD_DATASETS})",
    )
