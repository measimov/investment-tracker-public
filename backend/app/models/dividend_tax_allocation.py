"""显式股息归属不改变扣税事实的金额、币种和发生日。"""

from sqlalchemy import CheckConstraint, Column, ForeignKey, Integer, Numeric, UniqueConstraint

from ..database import Base


class DividendTaxAllocation(Base):
    __tablename__ = "dividend_tax_allocations"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cash_event_id = Column(
        Integer, ForeignKey("cash_events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    corporate_action_id = Column(
        Integer, ForeignKey("corporate_actions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount = Column(Numeric(24, 8), nullable=False)

    __table_args__ = (
        UniqueConstraint("cash_event_id", "corporate_action_id", name="uq_dividend_tax_allocation"),
        CheckConstraint("amount > 0", name="ck_dividend_tax_allocation_positive"),
    )
