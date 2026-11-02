import time
from datetime import timedelta
from decimal import Decimal

from app.config import get_settings
from app.db import SessionLocal
from app.models import Transaction, TransactionStatus
from app.services.transactions import (
    TransactionInput,
    claim_transaction,
    create_if_absent,
)
from app.tasks import process_transaction_impl
from app.timestamps import utc_now


def _create(transaction_id: str) -> None:
    with SessionLocal() as session:
        create_if_absent(
            session,
            TransactionInput(
                transaction_id=transaction_id,
                source_account="acc_user_789",
                destination_account="acc_merchant_456",
                amount=Decimal("1500.00"),
                currency="INR",
            ),
        )


def test_process_transaction_marks_row_processed() -> None:
    _create("txn_worker_1")

    result = process_transaction_impl("txn_worker_1")

    assert result == {"transaction_id": "txn_worker_1", "result": "processed"}
    with SessionLocal() as session:
        transaction = session.get(Transaction, "txn_worker_1")
        assert transaction is not None
        assert transaction.status == TransactionStatus.PROCESSED.value
        assert transaction.processed_at is not None
        assert transaction.processing_token is None
        assert transaction.lease_expires_at is None


def test_redelivery_after_completion_is_skipped() -> None:
    _create("txn_worker_2")
    assert process_transaction_impl("txn_worker_2")["result"] == "processed"
    with SessionLocal() as session:
        processed_at = session.get(Transaction, "txn_worker_2").processed_at

    result = process_transaction_impl("txn_worker_2")

    assert result["result"] == "skipped"
    with SessionLocal() as session:
        assert session.get(Transaction, "txn_worker_2").processed_at == processed_at


def test_active_lease_prevents_duplicate_processing() -> None:
    _create("txn_worker_3")
    with SessionLocal() as session:
        claim = claim_transaction(session, "txn_worker_3", lease_seconds=120)
        assert claim is not None

    result = process_transaction_impl("txn_worker_3")

    assert result["result"] == "skipped"
    with SessionLocal() as session:
        transaction = session.get(Transaction, "txn_worker_3")
        assert transaction.status == TransactionStatus.PROCESSING.value


def test_expired_lease_can_be_reclaimed() -> None:
    _create("txn_worker_4")
    with SessionLocal() as session:
        claim = claim_transaction(session, "txn_worker_4", lease_seconds=120)
        assert claim is not None
        transaction = session.get(Transaction, "txn_worker_4")
        transaction.lease_expires_at = utc_now() - timedelta(seconds=1)
        session.commit()

    result = process_transaction_impl("txn_worker_4")

    assert result["result"] == "processed"
    with SessionLocal() as session:
        assert session.get(Transaction, "txn_worker_4").status == TransactionStatus.PROCESSED.value


def test_missing_transaction_is_skipped() -> None:
    result = process_transaction_impl("txn_missing")

    assert result == {"transaction_id": "txn_missing", "result": "skipped"}


def test_processing_uses_configured_delay(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "processing_delay_seconds", 0.2)
    _create("txn_worker_5")

    started = time.perf_counter()
    result = process_transaction_impl("txn_worker_5")
    elapsed = time.perf_counter() - started

    assert result["result"] == "processed"
    assert elapsed >= 0.18
    with SessionLocal() as session:
        transaction = session.get(Transaction, "txn_worker_5")
        assert transaction.processed_at is not None
        assert (transaction.processed_at - transaction.created_at).total_seconds() >= 0.18
