"""security_profile_data 白名单扩充：report_statement_plan

Revision ID: 20260927_0022
Revises: 20260926_0021
Create Date: 2026-09-27

CheckConstraint 变更 alembic autogenerate 检测不到，手写迁移（模板同 0019）。
港股报表抽取的计划份数（每个标的一行，period_key=current）：最近一次完整的披露易清单计划到
多少份年报/中报。第二上市公司（09618）在披露易不发中期报告，planned_interim=0 让进度面板说清楚
「没有」而不是「缺」。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260927_0022"
down_revision: Union[str, None] = "20260926_0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_EXISTING = (
    "'fina_indicator', 'forecast', 'express', 'daily_basic', "
    "'dividend_history', 'fina_audit', 'pledge_stat', 'stk_holdertrade', "
    "'income', 'balancesheet', 'cashflow', "
    "'report_section', 'report_digest', 'business_profile', 'peer_list', "
    "'edgar_companyfacts', 'yahoo_fundamentals', 'report_target_plan', "
    "'xueqiu_income', 'xueqiu_capital_flow', 'xueqiu_holders', "
    "'report_statement_extract', 'report_statements'"
)
_ADDED = "'report_statement_plan'"


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
