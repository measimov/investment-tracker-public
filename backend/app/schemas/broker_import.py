from pydantic import BaseModel, Field
from typing import Dict, List, Optional


class BrokerImportSample(BaseModel):
    row_number: int
    symbol: str
    name: Optional[str] = None
    market: str
    transaction_type: str
    trade_date: str
    quantity: str
    price: str
    fee: str
    row_hash: str
    duplicate: bool


class BrokerImportResult(BaseModel):
    broker: str = "招商证券"
    filename: str
    import_batch_id: Optional[int] = None
    broker_account_id: Optional[int] = None
    batch_status: Optional[str] = None
    total_rows: int
    archived_source_rows: int = 0
    eligible_trade_rows: int
    eligible_dividend_rows: int = 0
    eligible_tax_rows: int = 0
    eligible_cash_rows: int = 0
    imported_transactions: int
    imported_corporate_actions: int = 0
    imported_tax_adjustments: int = 0
    imported_cash_events: int = 0
    canonical_objects_changed: int = 0
    booked_source_rows: int = 0
    unbooked_source_rows: int = 0
    eligible_unbooked_source_rows: int = 0
    claimable_unassigned_rows: int = 0
    migrated_legacy_rows: int = 0
    migrated_unassigned_rows: int = 0
    duplicate_rows: int
    skipped_non_trade_rows: int
    skipped_invalid_rows: int
    skipped_option_rows: int = 0
    skipped_fx_rows: int = 0
    skipped_cash_rows: int = 0
    skipped_unsupported_rows: int = 0
    skipped_conflict_rows: int = 0
    # 排除清单命中的行数：只归档不入账，预览时必须可见（高影响配置需事前核对）
    skipped_excluded_rows: int = 0
    affected_symbols: int
    date_start: Optional[str] = None
    date_end: Optional[str] = None
    event_date_start: Optional[str] = None
    event_date_end: Optional[str] = None
    statement_scope: Optional[str] = None
    source_account_masks: List[str] = Field(default_factory=list)
    reported_position_count: int = 0
    reconciliation_snapshot_id: Optional[int] = None
    reconciliation_status: Optional[str] = None
    business_counts: Dict[str, int] = Field(default_factory=dict)
    duplicate_samples: List[BrokerImportSample] = Field(default_factory=list)
    import_samples: List[BrokerImportSample] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
