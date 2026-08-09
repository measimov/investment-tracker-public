from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional


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
    duplicate_rows: int
    skipped_non_trade_rows: int
    skipped_invalid_rows: int
    skipped_option_rows: int = 0
    # IBKR：可入账的现金业务/外汇行（生成 CashEvent），与"跳过"分列展示
    eligible_cash_event_rows: int = 0
    eligible_fx_rows: int = 0
    # 设计上有意只归档的行（如 IBKR「调整」纸面损益），预期跳过不拖批次状态
    expected_archived_rows: int = 0
    skipped_fx_rows: int = 0
    skipped_cash_rows: int = 0
    skipped_unsupported_rows: int = 0
    skipped_conflict_rows: int = 0
    # 排除清单命中的行数：只归档不入账，预览时必须可见（高影响配置需事前核对）
    skipped_excluded_rows: int = 0
    # 本批新增（非重复）的排除行：批次状态的预期跳过抵扣口径
    excluded_unbooked_rows: int = 0
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
    # 两个列表都截断到 50 条，前端再截到 8 条。没有真实条数的话，"看到 8 条"
    # 和"一共 8 条"在界面上完全无法区分——排查会建立在错误的前提上。
    warnings_total: int = 0
    errors_total: int = 0
    # 脱敏诊断报告（目前仅招商预览产出）。形态会随排查演进，故不逐字段建模：
    # 钉死 schema 只会给每次加一个诊断字段增加改动摩擦，而它不是对外契约。
    diagnostics: Optional[Dict[str, Any]] = None
