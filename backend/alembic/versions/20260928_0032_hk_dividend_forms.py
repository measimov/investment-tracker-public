"""港股分红同步：建议表加公告明细列，security_profile_data 白名单加 hkex_dividend_form

Revision ID: 20260928_0032
Revises: 20260928_0031
Create Date: 2026-09-28

- corporate_action_suggestions.announcement_detail（JSONB，可空）：披露易现金股息公告的
  组成/宣派与派发币种/汇率/代扣税/以股代息标记；A/B 股建议为 NULL，存量行不回填。
- hkex_dividend_form：披露易 EF001 现金股息公告表格的解析缓存（每份文档一行，
  period_key=文档号，全局，含原文以便解析器升版零下载重解析）。

CheckConstraint 变更 autogenerate 检测不到，手写（模板同 0023）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260928_0032"
down_revision: Union[str, None] = "20260928_0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DATASETS = (
    "'fina_indicator', 'forecast', 'express', 'daily_basic', "
    "'dividend_history', 'fina_audit', 'pledge_stat', 'stk_holdertrade', "
    "'income', 'balancesheet', 'cashflow', "
    "'report_section', 'report_digest', 'business_profile', 'peer_list', "
    "'edgar_companyfacts', 'yahoo_fundamentals', 'report_target_plan', "
    "'xueqiu_income', 'xueqiu_capital_flow', 'xueqiu_holders', "
    "'report_statement_extract', 'report_statements', 'report_statement_plan', "
    "'ads_ratio'"
)
_ADDED_DATASET = "'hkex_dividend_form'"


def _replace_dataset_check(datasets: str) -> None:
    op.drop_constraint("ck_security_profile_dataset", "security_profile_data", type_="check")
    op.create_check_constraint(
        "ck_security_profile_dataset", "security_profile_data", f"dataset IN ({datasets})"
    )


def upgrade() -> None:
    op.add_column(
        "corporate_action_suggestions",
        sa.Column(
            "announcement_detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="公告明细（港股披露易：股息组成/宣派与派发币种/汇率/代扣税/以股代息标记）",
        ),
    )
    _replace_dataset_check(f"{_DATASETS}, {_ADDED_DATASET}")


def downgrade() -> None:
    # 先删再收紧：留着新值的行会让 create_check_constraint 直接失败
    op.execute(f"DELETE FROM security_profile_data WHERE dataset IN ({_ADDED_DATASET})")
    _replace_dataset_check(_DATASETS)
    op.drop_column("corporate_action_suggestions", "announcement_detail")
