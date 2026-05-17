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
    total_rows: int
    eligible_trade_rows: int
    eligible_dividend_rows: int = 0
    eligible_tax_rows: int = 0
    imported_transactions: int
    imported_corporate_actions: int = 0
    imported_tax_adjustments: int = 0
    duplicate_rows: int
    skipped_non_trade_rows: int
    skipped_invalid_rows: int
    skipped_option_rows: int = 0
    skipped_fx_rows: int = 0
    skipped_cash_rows: int = 0
    skipped_unsupported_rows: int = 0
    affected_symbols: int
    date_start: Optional[str] = None
    date_end: Optional[str] = None
    business_counts: Dict[str, int] = Field(default_factory=dict)
    duplicate_samples: List[BrokerImportSample] = Field(default_factory=list)
    import_samples: List[BrokerImportSample] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
