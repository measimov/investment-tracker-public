from uuid import uuid4

import pytest

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.import_batch import ImportBatch
from app.services.broker_import_common import base_import_result
from app.services.import_batch_service import (
    complete_import_batch,
    start_import_batch,
    validate_source_file_account,
)


def test_identical_source_file_cannot_be_booked_into_two_accounts():
    db = SessionLocal()
    tag = f"source-account-{uuid4().hex}"
    contents = f"statement-{tag}".encode()
    try:
        first_account = BrokerAccount(
            user_id=1,
            broker=tag,
            account_name="First",
            notes=tag,
        )
        second_account = BrokerAccount(
            user_id=1,
            broker=tag,
            account_name="Second",
            notes=tag,
        )
        db.add_all([first_account, second_account])
        db.commit()

        first_batch = start_import_batch(
            db,
            user_id=1,
            broker_account_id=first_account.id,
            broker=tag,
            source_type="statement_pdf",
            filename="statement.pdf",
            contents=contents,
            parser_name="test",
            parser_version="1",
        )
        first_batch.status = "COMPLETED"
        first_batch.row_count = 1
        first_batch.archived_count = 0
        first_batch.imported_count = 0
        db.commit()

        with pytest.raises(ValueError, match="another broker account"):
            validate_source_file_account(
                db,
                user_id=1,
                broker_account_id=second_account.id,
                broker=tag,
                contents=contents,
            )
        with pytest.raises(ValueError, match="another broker account"):
            start_import_batch(
                db,
                user_id=1,
                broker_account_id=second_account.id,
                broker=tag,
                source_type="statement_pdf",
                filename="statement.pdf",
                contents=contents,
                parser_name="test",
                parser_version="1",
            )

        same_account_batch = start_import_batch(
            db,
            user_id=1,
            broker_account_id=first_account.id,
            broker=tag,
            source_type="statement_pdf",
            filename="statement.pdf",
            contents=contents,
            parser_name="test",
            parser_version="1",
        )
        assert same_account_batch.status == "PENDING"
    finally:
        db.rollback()
        db.query(ImportBatch).filter(ImportBatch.broker == tag).delete(
            synchronize_session=False
        )
        db.query(BrokerAccount).filter(BrokerAccount.broker == tag).delete(
            synchronize_session=False
        )
        db.commit()
        db.close()


def test_import_batch_separates_source_archival_from_canonical_booking():
    db = SessionLocal()
    tag = f"batch-counts-{uuid4().hex}"
    try:
        account = BrokerAccount(
            user_id=1,
            broker=tag,
            account_name="Counts",
            notes=tag,
        )
        db.add(account)
        db.commit()
        batch = start_import_batch(
            db,
            user_id=1,
            broker_account_id=account.id,
            broker=tag,
            source_type="statement_pdf",
            filename="counts.pdf",
            contents=tag.encode(),
            parser_name="test",
            parser_version="1",
        )

        # 结算契约要求键齐全（complete_import_batch 按键直取），所以用生产方
        # 同一个骨架构造输入，而不是手拼部分 dict。
        completed = complete_import_batch(
            db,
            batch.id,
            result=base_import_result(
                broker=tag,
                filename="counts.pdf",
                total_rows=5,
                eligible_trade_rows=2,
                eligible_dividend_rows=0,
                eligible_tax_rows=0,
                eligible_cash_rows=1,
                imported_transactions=2,
                imported_corporate_actions=0,
                imported_tax_adjustments=0,
                imported_cash_events=1,
                duplicate_rows=0,
                skipped_non_trade_rows=2,
                skipped_invalid_rows=0,
                skipped_excluded_rows=0,
                excluded_unbooked_rows=0,
                affected_symbols=1,
                date_start=None,
                date_end=None,
                business_counts={},
                duplicate_samples=[],
                import_samples=[],
                errors=[],
            ),
            imported_count=3,
            archived_count=5,
        )

        assert completed.status == "PARTIAL"
        assert completed.row_count == 5
        assert completed.archived_count == 5
        assert completed.imported_count == 3
        assert completed.skipped_count == 2
        assert completed.error_count == 2
        assert "not booked to a canonical event" in completed.error_message
    finally:
        db.rollback()
        db.query(ImportBatch).filter(ImportBatch.broker == tag).delete(
            synchronize_session=False
        )
        db.query(BrokerAccount).filter(BrokerAccount.broker == tag).delete(
            synchronize_session=False
        )
        db.commit()
        db.close()
