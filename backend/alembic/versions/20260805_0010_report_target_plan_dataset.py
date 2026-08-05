"""security_profile_data 白名单扩充：report_target_plan（年报清单缓存）

Revision ID: 20260805_0010
Revises: 20260804_0009
Create Date: 2026-08-05

CheckConstraint 变更 alembic autogenerate 检测不到，手写迁移（模板同 0009）。
report_target_plan 缓存 cninfo/EDGAR 的年报清单：清单一整年不变，而每次分析
都重新检索会打 2-10 次 cninfo（每次 1s 限速），批量分析时纯浪费。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260805_0010"
down_revision: Union[str, None] = "20260804_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_EXISTING = (
    "'fina_indicator', 'forecast', 'express', 'daily_basic', "
    "'dividend_history', 'fina_audit', 'pledge_stat', 'stk_holdertrade', "
    "'income', 'balancesheet', 'cashflow', "
    "'report_section', 'report_digest', 'business_profile', 'peer_list', "
    "'edgar_companyfacts', 'yahoo_fundamentals'"
)
_ADDED = "'report_target_plan'"


def upgrade() -> None:
    op.drop_constraint(
        "ck_security_profile_dataset", "security_profile_data", type_="check"
    )
    op.create_check_constraint(
        "ck_security_profile_dataset",
        "security_profile_data",
        f"dataset IN ({_EXISTING}, {_ADDED})",
    )


def downgrade() -> None:
    op.execute(
        f"DELETE FROM security_profile_data WHERE dataset IN ({_ADDED})"
    )
    op.drop_constraint(
        "ck_security_profile_dataset", "security_profile_data", type_="check"
    )
    op.create_check_constraint(
        "ck_security_profile_dataset",
        "security_profile_data",
        f"dataset IN ({_EXISTING})",
    )
