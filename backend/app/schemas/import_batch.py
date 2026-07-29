from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict


ImportBatchStatus = Literal["PENDING", "COMPLETED", "PARTIAL", "FAILED"]


class ImportBatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    broker_account_id: Optional[int]
    broker: str
    source_type: str
    source_filename: Optional[str]
    source_sha256: Optional[str]
    period_start: Optional[date]
    period_end: Optional[date]
    status: ImportBatchStatus
    row_count: int
    archived_count: int
    imported_count: int
    duplicate_count: int
    skipped_count: int
    error_count: int
    parser_name: Optional[str]
    parser_version: Optional[str]
    error_message: Optional[str]
    created_at: datetime
    completed_at: Optional[datetime]
