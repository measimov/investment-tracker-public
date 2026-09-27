"""ADS 换算比：security_profile_data 白名单加 ads_ratio，security_rules 加 ADS_RATIO

Revision ID: 20260927_0023
Revises: 20260927_0022
Create Date: 2026-09-27

CheckConstraint 变更 alembic autogenerate 检测不到，手写迁移（模板同 0022）。
- ads_ratio：美股 20-F 封面解析出的「1 ADS = N 股普通股」（每标的一行，period_key=current，全局）
- ADS_RATIO：用户手工维护的换算比（payload {ratio}），解析失败或有误时覆盖解析值
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260927_0023"
down_revision: Union[str, None] = "20260927_0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DATASETS = (
    "'fina_indicator', 'forecast', 'express', 'daily_basic', "
    "'dividend_history', 'fina_audit', 'pledge_stat', 'stk_holdertrade', "
    "'income', 'balancesheet', 'cashflow', "
    "'report_section', 'report_digest', 'business_profile', 'peer_list', "
    "'edgar_companyfacts', 'yahoo_fundamentals', 'report_target_plan', "
    "'xueqiu_income', 'xueqiu_capital_flow', 'xueqiu_holders', "
    "'report_statement_extract', 'report_statements', 'report_statement_plan'"
)
_ADDED_DATASET = "'ads_ratio'"

_RULE_TYPES = (
    "'EXCLUDE', 'CASH_MANAGEMENT', 'RELISTING', "
    "'NAME_OVERRIDE', 'PRICE_GAP_EXEMPTION', 'CMB_CASH_BUSINESS'"
)
_ADDED_RULE_TYPE = "'ADS_RATIO'"


def _replace_checks(datasets: str, rule_types: str) -> None:
    op.drop_constraint("ck_security_profile_dataset", "security_profile_data", type_="check")
    op.create_check_constraint(
        "ck_security_profile_dataset", "security_profile_data", f"dataset IN ({datasets})"
    )
    op.drop_constraint("ck_security_rules_rule_type", "security_rules", type_="check")
    op.create_check_constraint(
        "ck_security_rules_rule_type", "security_rules", f"rule_type IN ({rule_types})"
    )


def upgrade() -> None:
    _replace_checks(f"{_DATASETS}, {_ADDED_DATASET}", f"{_RULE_TYPES}, {_ADDED_RULE_TYPE}")


def downgrade() -> None:
    # 先删再收紧：留着新值的行会让 create_check_constraint 直接失败
    op.execute(f"DELETE FROM security_profile_data WHERE dataset IN ({_ADDED_DATASET})")
    op.execute(f"DELETE FROM security_rules WHERE rule_type IN ({_ADDED_RULE_TYPE})")
    _replace_checks(_DATASETS, _RULE_TYPES)
