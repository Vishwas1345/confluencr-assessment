from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select, update

from app.models import Transaction, TransactionStatus
from app.services.transactions import (
    TransactionInput,
    claim_transaction,
    complete_transaction,
    create_if_absent,
    get_transaction,
    release_transaction,
)
from app.timestamps import utc_now


def make_payload(transaction_id: str = "txn_service_1") -> TransactionInput:
    return TransactionInput(
        transaction_id=transaction_id,
        source_account="acc_user_789",
        destination_account="acc_merchant_456",
        amount=Decimal("1500.00"),
        currency="INR",
    )


def test_create_if_absent_is_idempotent(db_session):
    payload = make_payload()

    assert create_if_absent(db_session, payload) is True
    assert create_if_absent(db_session, payload) is False

    count = db_session.scalar(select(func.count()).select_from(Transaction))
    transaction = get_transaction(db_session, payload.transaction_id)

    assert count == 1
    assert transaction is not None
    assert transaction.status == TransactionStatus.PROCESSING.value
    assert transaction.amount == Decimal("1500.00")
    assert transaction.currency == "INR"
    assert transaction.processed_at is None
    assert transaction.created_at.tzinfo is not None


def test_claim_and_complete_transaction(db_session):
    payload = make_payload()
    create_if_absent(db_session, payload)

    claim = claim_transaction(db_session, payload.transaction_id, lease_seconds=120)

    assert claim is not None
    assert claim_transaction(db_session, payload.transaction_id, lease_seconds=120) is None
    assert complete_transaction(db_session, payload.transaction_id, claim.token) is True

    transaction = get_transaction(db_session, payload.transaction_id)
    assert transaction is not None
    assert transaction.status == TransactionStatus.PROCESSED.value
    assert transaction.processed_at is not None
    assert transaction.processing_token is None
    assert transaction.lease_expires_at is None
    assert complete_transaction(db_session, payload.transaction_id, claim.token) is False


def test_release_allows_reclaim_and_rejects_stale_token(db_session):
    payload = make_payload()
    create_if_absent(db_session, payload)

    first_claim = claim_transaction(db_session, payload.transaction_id, lease_seconds=120)
    assert first_claim is not None

    assert (
        release_transaction(db_session, payload.transaction_id, first_claim.token, "boom") is True
    )

    released = get_transaction(db_session, payload.transaction_id)
    assert released is not None
    assert released.lease_expires_at is None
    assert released.processing_token is None
    assert released.last_error == "boom"

    second_claim = claim_transaction(db_session, payload.transaction_id, lease_seconds=120)
    assert second_claim is not None

    assert complete_transaction(db_session, payload.transaction_id, first_claim.token) is False
    assert complete_transaction(db_session, payload.transaction_id, second_claim.token) is True


def test_expired_lease_can_be_reclaimed(db_session):
    payload = make_payload()
    create_if_absent(db_session, payload)

    first_claim = claim_transaction(db_session, payload.transaction_id, lease_seconds=120)
    assert first_claim is not None

    db_session.execute(
        update(Transaction)
        .where(Transaction.transaction_id == payload.transaction_id)
        .values(lease_expires_at=utc_now() - timedelta(seconds=1))
    )
    db_session.commit()

    second_claim = claim_transaction(db_session, payload.transaction_id, lease_seconds=120)
    assert second_claim is not None
    assert second_claim.token != first_claim.token


def test_unknown_transaction_returns_none(db_session):
    assert get_transaction(db_session, "txn_missing") is None
