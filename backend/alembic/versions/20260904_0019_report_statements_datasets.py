"""security_profile_data 白名单扩充：report_statement_extract / report_statements

Revision ID: 20260904_0019
Revises: 20260904_0018
Create Date: 2026-09-05

CheckConstraint 变更 alembic autogenerate 检测不到，手写迁移（模板同 0014）。
港股年报/中报 PDF 三张表抽取：report_statement_extract 按报告（period_key=报告期|annual/
interim）存定位到的结构化行与映射结果；report_statements 按会计期（period_key=末日|FY/H1）
存归一后的 26 科目行，与 Yahoo 透视行同形状。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260904_0019"
down_revision: Union[str, None] = "20260904_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_EXISTING = (
    "'fina_indicator', 'forecast', 'express', 'daily_basic', "
    "'dividend_history', 'fina_audit', 'pledge_stat', 'stk_holdertrade', "
    "'income', 'balancesheet', 'cashflow', "
    "'report_section', 'report_digest', 'business_profile', 'peer_list', "
    "'edgar_companyfacts', 'yahoo_fundamentals', 'report_target_plan', "
    "'xueqiu_income', 'xueqiu_capital_flow', 'xueqiu_holders'"
)
_ADDED = "'report_statement_extract', 'report_statements'"


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
