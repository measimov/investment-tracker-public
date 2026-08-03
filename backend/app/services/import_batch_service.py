from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from sqlalchemy.orm import Session

from ..models.broker_account import BrokerAccount
from ..models.import_batch import ImportBatch


def _safe_filename(filename: Optional[str]) -> Optional[str]:
    if not filename:
        return None
    return Path(filename).name


def _error_message(error: Any, limit: int = 4000) -> str:
    message = str(error).strip() or error.__class__.__name__
    return message[:limit]


def validate_import_account(
    db: Session,
    *,
    user_id: int,
    broker_account_id: Optional[int],
    broker: str,
) -> Optional[BrokerAccount]:
    """Return an owned, broker-compatible account for an import."""
    if broker_account_id is None:
        return None

    account = (
        db.query(BrokerAccount)
        .filter(
            BrokerAccount.id == broker_account_id,
            BrokerAccount.user_id == user_id,
        )
        .first()
    )
    if account is None:
        raise ValueError("Broker account not found")
    if account.broker != broker:
        raise ValueError(f"Broker account belongs to {account.broker}, not {broker}")
    return account


def validate_source_file_account(
    db: Session,
    *,
    user_id: int,
    broker_account_id: int,
    broker: str,
    contents: bytes,
) -> str:
    """
    Prevent the exact same broker file from being booked into two accounts.

    Per-account row hashes intentionally allow two real accounts to contain
    economically identical rows. An identical source file is different: once
    it produced archived or canonical records for one account, selecting a
    second account is almost certainly an operator error.
    """
    source_sha256 = hashlib.sha256(contents).hexdigest()
    conflicting_batch = (
        db.query(ImportBatch.id)
        .filter(
            ImportBatch.user_id == user_id,
            ImportBatch.broker == broker,
            ImportBatch.source_sha256 == source_sha256,
            ImportBatch.broker_account_id.is_not(None),
            ImportBatch.broker_account_id != broker_account_id,
            (
                (ImportBatch.status == "COMPLETED")
                | (
                    (ImportBatch.status == "PARTIAL")
                    & (
                        (ImportBatch.archived_count > 0)
                        | (ImportBatch.imported_count > 0)
                    )
                )
            ),
        )
        .first()
    )
    if conflicting_batch is not None:
        raise ValueError(
            "This exact statement file was already imported into another "
            "broker account. Select the original account instead."
        )
    return source_sha256


def start_import_batch(
    db: Session,
    *,
    user_id: int,
    broker_account_id: int,
    broker: str,
    source_type: str,
    filename: Optional[str],
    contents: bytes,
    parser_name: str,
    parser_version: str,
) -> ImportBatch:
    """Persist a PENDING audit record before parsing an actual import."""
    validate_import_account(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        broker=broker,
    )
    source_sha256 = validate_source_file_account(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        broker=broker,
        contents=contents,
    )
    batch = ImportBatch(
        user_id=user_id,
        broker_account_id=broker_account_id,
        broker=broker,
        source_type=source_type,
        source_filename=_safe_filename(filename),
        source_sha256=source_sha256,
        status="PENDING",
        parser_name=parser_name,
        parser_version=parser_version,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def set_import_batch_source_stats(
    batch: ImportBatch,
    *,
    row_count: int,
    period_start: Optional[date],
    period_end: Optional[date],
) -> None:
    batch.row_count = max(0, row_count)
    batch.period_start = period_start
    batch.period_end = period_end


def complete_import_batch(
    db: Session,
    batch_id: int,
    *,
    result: Mapping[str, Any],
    imported_count: int,
    archived_count: int = 0,
) -> ImportBatch:
    """Finalize counts and status without changing the importer's result fields."""
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise ValueError("Import batch not found while finalizing import")

    row_count = max(0, int(result.get("total_rows", batch.row_count or 0)))
    archived_count = max(0, int(archived_count))
    duplicate_rows = min(row_count, max(0, int(result.get("duplicate_rows", 0))))
    imported_count = min(
        max(0, row_count - duplicate_rows),
        max(0, int(imported_count)),
    )
    errors = [str(error) for error in result.get("errors", []) if str(error).strip()]
    booked_source_rows = imported_count + duplicate_rows
    # 命中排除清单的行是"预期跳过"（归档不入账是配置使然，不是数据问题），
    # 不应把批次拖成 PARTIAL。只抵扣本批新增、非重复的排除行
    # （excluded_unbooked_rows），不用可能含重复行的审计总数
    # （skipped_excluded_rows）——否则会掩盖真实的 unsupported/invalid 行。
    excluded_unbooked_rows = min(
        max(0, int(result.get("excluded_unbooked_rows", 0))),
        max(0, row_count - booked_source_rows),
    )
    # 设计上有意只归档的行（如 IBKR "调整" 纸面损益）同属预期跳过，
    # 与排除清单行一样不把批次拖成 PARTIAL
    expected_archived_rows = min(
        max(0, int(result.get("expected_archived_rows", 0))),
        max(0, row_count - booked_source_rows - excluded_unbooked_rows),
    )
    unbooked_source_rows = max(
        0,
        row_count - booked_source_rows - excluded_unbooked_rows - expected_archived_rows,
    )
    unresolved_count = max(
        0,
        unbooked_source_rows,
        int(result.get("skipped_invalid_rows", 0)),
        int(result.get("skipped_unsupported_rows", 0)),
        int(result.get("skipped_conflict_rows", 0)),
        int(result.get("skipped_non_trade_rows", 0)),
    )
    error_count = max(
        unresolved_count,
        len(errors),
    )
    partial = bool(errors) or error_count > 0 or unbooked_source_rows > 0

    batch.row_count = row_count
    batch.archived_count = archived_count
    batch.imported_count = imported_count
    batch.duplicate_count = duplicate_rows
    batch.skipped_count = unbooked_source_rows
    batch.error_count = error_count
    batch.status = "PARTIAL" if partial else "COMPLETED"
    if errors:
        batch.error_message = _error_message("; ".join(errors))
    elif unbooked_source_rows:
        batch.error_message = (
            f"{unbooked_source_rows} source row(s) were not booked to a canonical event"
        )
    elif unresolved_count:
        batch.error_message = (
            f"{unresolved_count} source row(s) were preserved without a canonical event"
        )
    elif partial:
        batch.error_message = f"{error_count} invalid source row(s) were skipped"
    else:
        batch.error_message = None
    batch.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(batch)
    return batch


def fail_import_batch(
    db: Session,
    batch_id: int,
    error: Exception,
    *,
    records_committed: bool,
    row_count: int = 0,
    imported_count: int = 0,
    duplicate_count: int = 0,
    archived_count: int = 0,
) -> None:
    """Leave an honest FAILED/PARTIAL audit record after an import exception."""
    db.rollback()
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        return

    batch.row_count = max(batch.row_count or 0, row_count)
    if records_committed:
        batch.archived_count = max(batch.archived_count or 0, archived_count)
        batch.duplicate_count = min(
            batch.row_count,
            max(batch.duplicate_count or 0, duplicate_count),
        )
        batch.imported_count = min(
            max(0, batch.row_count - batch.duplicate_count),
            max(batch.imported_count or 0, imported_count),
        )
        batch.skipped_count = max(
            0,
            batch.row_count - batch.imported_count - batch.duplicate_count,
        )
    else:
        # A rolled-back import did not create canonical or archived records,
        # even if in-memory counters were incremented before the exception.
        batch.archived_count = 0
        batch.imported_count = 0
        batch.duplicate_count = 0
        batch.skipped_count = batch.row_count
    batch.error_count = max(1, batch.error_count or 0)
    batch.status = "PARTIAL" if records_committed else "FAILED"
    batch.error_message = _error_message(error)
    batch.completed_at = datetime.now(timezone.utc)
    db.commit()
