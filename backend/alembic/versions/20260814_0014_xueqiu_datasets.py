"""security_profile_data 白名单扩充：xueqiu_income / xueqiu_capital_flow

Revision ID: 20260814_0014
Revises: 20260813_0013
Create Date: 2026-08-31

CheckConstraint 变更 alembic autogenerate 检测不到，手写迁移（模板同 0010）。
雪球数据源接入：利润表（period_key=报告期）与资金流历史（period_key=交易日）。
两者都只覆盖 A 股——上游 finance/f10 端点为 /cn/ 口径。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260814_0014"
down_revision: Union[str, None] = "20260813_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_EXISTING = (
    "'fina_indicator', 'forecast', 'express', 'daily_basic', "
    "'dividend_history', 'fina_audit', 'pledge_stat', 'stk_holdertrade', "
    "'income', 'balancesheet', 'cashflow', "
    "'report_section', 'report_digest', 'business_profile', 'peer_list', "
    "'edgar_companyfacts', 'yahoo_fundamentals', 'report_target_plan'"
)
_ADDED = "'xueqiu_income', 'xueqiu_capital_flow'"


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
    # 先删再收紧：留着新 dataset 的行会让 create_check_constraint 直接失败
    op.execute(f"DELETE FROM security_profile_data WHERE dataset IN ({_ADDED})")
    op.drop_constraint(
        "ck_security_profile_dataset", "security_profile_data", type_="check"
    )
    op.create_check_constraint(
        "ck_security_profile_dataset",
        "security_profile_data",
        f"dataset IN ({_EXISTING})",
    )
