from datetime import date, datetime
from decimal import Decimal
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


ReconciliationStatus = Literal["PENDING", "MATCHED", "MISMATCHED"]


class ReconciliationPosition(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    market: str = Field(..., min_length=1, max_length=20)
    quantity: Decimal = Field(..., ge=0)
    currency: Optional[str] = Field(None, min_length=1, max_length=10)


class ReconciliationSnapshotBase(BaseModel):
    snapshot_date: date
    source_filename: Optional[str] = Field(None, max_length=255)
    cash_balances: Dict[str, Decimal] = Field(default_factory=dict)
    positions: List[ReconciliationPosition] = Field(default_factory=list)
    notes: Optional[str] = None


class ReconciliationSnapshotCreate(ReconciliationSnapshotBase):
    broker_account_id: int


class ReconciliationSnapshotUpdate(BaseModel):
    broker_account_id: Optional[int] = None
    snapshot_date: Optional[date] = None
    source_filename: Optional[str] = Field(None, max_length=255)
    cash_balances: Optional[Dict[str, Decimal]] = None
    positions: Optional[List[ReconciliationPosition]] = None
    notes: Optional[str] = None


class ReconciliationSnapshotResponse(ReconciliationSnapshotBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    # status 由自动比对派生（MATCHED/MISMATCHED；仅未比对过为 PENDING），不可客户端设置
    status: ReconciliationStatus
    broker_account_id: Optional[int]
    import_batch_id: Optional[int]
    statement_scope: Optional[str]
    diff_detail: Optional[dict] = Field(
        None, description="自动比对明细：持仓/现金逐项 diff、归属矛盾与口径说明"
    )
    compared_at: Optional[datetime] = Field(None, description="最近一次自动比对时间")
    created_at: datetime
    updated_at: datetime
