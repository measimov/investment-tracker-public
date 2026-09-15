"""security_profile_data 白名单扩充：xueqiu_holders（十大流通股东）

Revision ID: 20260814_0015
Revises: 20260814_0014
Create Date: 2026-08-31

CheckConstraint 变更 alembic autogenerate 检测不到，手写迁移（模板同 0013）。
period_key = 报告期|名次（如 "20260630|01"）：报告期取自响应顶层 times[0]，
名次即 items 顺序。库 adapter 用 holder_name 作键，那样只存得住最新一期且
退出前十的股东会永久残留，故本项目在 wrapper 层自构复合键。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260814_0015"
down_revision: Union[str, None] = "20260814_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_EXISTING = (
    "'fina_indicator', 'forecast', 'express', 'daily_basic', "
    "'dividend_history', 'fina_audit', 'pledge_stat', 'stk_holdertrade', "
    "'income', 'balancesheet', 'cashflow', "
    "'report_section', 'report_digest', 'business_profile', 'peer_list', "
    "'edgar_companyfacts', 'yahoo_fundamentals', 'report_target_plan', "
    "'xueqiu_income', 'xueqiu_capital_flow'"
)
_ADDED = "'xueqiu_holders'"


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
