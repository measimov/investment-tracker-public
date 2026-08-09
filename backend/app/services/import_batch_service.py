from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from sqlalchemy.orm import Session

from ..models.broker_account import BrokerAccount
from ..models.import_batch import ImportBatch

# 批次结算契约：complete_import_batch 按键**直取**的键集合。生产方是
# broker_import_common.base_import_result（少给的券商在骨架里落显式默认 0），
# tests/test_broker_import_contract.py 钉住三家结果都覆盖这组键——此前三家
# 键集合各不相同，这里只能靠 .get 默认值逐键调和三种方言。
BATCH_SETTLEMENT_KEYS = frozenset(
    {
        "total_rows",
        "duplicate_rows",
        "errors",
        "excluded_unbooked_rows",
        "expected_archived_rows",
        "skipped_invalid_rows",
        "skipped_unsupported_rows",
        "skipped_conflict_rows",
        "skipped_non_trade_rows",
    }
)


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
    """Finalize counts and status without changing the importer's result fields.

    `result` 必须覆盖 `BATCH_SETTLEMENT_KEYS`（由 base_import_result 骨架保证，
    契约测试看住），这里按键直取；此前三家键集合各不相同，只能靠 .get 默认值
    逐键调和三种方言。留下来的 min/max 各自防一个真实的口径差，逐个注明。
    """
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise ValueError("Import batch not found while finalizing import")

    row_count = int(result["total_rows"])
    archived_count = int(archived_count)
    duplicate_rows = int(result["duplicate_rows"])
    # imported_count 是**入账对象数**（交易+公司行动+税+现金事件），与"来源行"
    # 不是一个单位：一条分红行会同时建 CA 和现金事件。批次的 imported_count
    # 语义是"已入账的来源行"，所以要用非重复来源行的容量封顶。
    imported_count = min(max(0, row_count - duplicate_rows), int(imported_count))
    errors = [str(error) for error in result["errors"] if str(error).strip()]
    booked_source_rows = imported_count + duplicate_rows
    # 命中排除清单的行是"预期跳过"（归档不入账是配置使然，不是数据问题），
    # 不应把批次拖成 PARTIAL。只抵扣本批新增、非重复的排除行
    # （excluded_unbooked_rows），不用可能含重复行的审计总数
    # （skipped_excluded_rows）——否则会掩盖真实的 unsupported/invalid 行；
    # 且抵扣不得超过未入账余量，防止把真实未入账行一并抵没。
    excluded_unbooked_rows = min(
        int(result["excluded_unbooked_rows"]),
        max(0, row_count - booked_source_rows),
    )
    # 设计上有意只归档的行（如 IBKR "调整" 纸面损益）同属预期跳过，
    # 与排除清单行一样不把批次拖成 PARTIAL；同样以剩余未入账余量封顶。
    expected_archived_rows = min(
        int(result["expected_archived_rows"]),
        max(0, row_count - booked_source_rows - excluded_unbooked_rows),
    )
    unbooked_source_rows = max(
        0,
        row_count - booked_source_rows - excluded_unbooked_rows - expected_archived_rows,
    )
    unresolved_count = max(
        unbooked_source_rows,
        int(result["skipped_invalid_rows"]),
        int(result["skipped_unsupported_rows"]),
        int(result["skipped_conflict_rows"]),
        int(result["skipped_non_trade_rows"]),
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
