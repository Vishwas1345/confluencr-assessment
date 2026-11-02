from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import or_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Transaction, TransactionStatus
from app.timestamps import utc_now


@dataclass(frozen=True, slots=True)
class TransactionInput:
    transaction_id: str
    source_account: str
    destination_account: str
    amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class Claim:
    transaction_id: str
    token: UUID


def create_if_absent(session: Session, payload: TransactionInput) -> bool:
    statement = (
        insert(Transaction)
        .values(
            transaction_id=payload.transaction_id,
            source_account=payload.source_account,
            destination_account=payload.destination_account,
            amount=payload.amount,
            currency=payload.currency,
            status=TransactionStatus.PROCESSING.value,
            created_at=utc_now(),
            processing_attempts=0,
        )
        .on_conflict_do_nothing(index_elements=[Transaction.transaction_id])
        .returning(Transaction.transaction_id)
    )
    try:
        inserted_id = session.execute(statement).scalar_one_or_none()
        session.commit()
    except Exception:
        session.rollback()
        raise
    return inserted_id is not None


def get_transaction(session: Session, transaction_id: str) -> Transaction | None:
    return session.get(Transaction, transaction_id)


def claim_transaction(
    session: Session,
    transaction_id: str,
    *,
    lease_seconds: int,
) -> Claim | None:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")

    now = utc_now()
    token = uuid4()
    statement = (
        update(Transaction)
        .where(
            Transaction.transaction_id == transaction_id,
            Transaction.status == TransactionStatus.PROCESSING.value,
            or_(
                Transaction.lease_expires_at.is_(None),
                Transaction.lease_expires_at < now,
            ),
        )
        .values(
            processing_started_at=now,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            processing_token=token,
            processing_attempts=Transaction.processing_attempts + 1,
            last_error=None,
        )
        .returning(Transaction.processing_token)
    )
    try:
        claimed_token = session.execute(statement).scalar_one_or_none()
        session.commit()
    except Exception:
        session.rollback()
        raise

    if claimed_token is None:
        return None
    return Claim(transaction_id=transaction_id, token=claimed_token)


def complete_transaction(session: Session, transaction_id: str, token: UUID) -> bool:
    statement = (
        update(Transaction)
        .where(
            Transaction.transaction_id == transaction_id,
            Transaction.status == TransactionStatus.PROCESSING.value,
            Transaction.processing_token == token,
        )
        .values(
            status=TransactionStatus.PROCESSED.value,
            processed_at=utc_now(),
            lease_expires_at=None,
            processing_token=None,
            last_error=None,
        )
        .returning(Transaction.transaction_id)
    )
    try:
        completed_id = session.execute(statement).scalar_one_or_none()
        session.commit()
    except Exception:
        session.rollback()
        raise
    return completed_id is not None


def release_transaction(
    session: Session,
    transaction_id: str,
    token: UUID,
    error: str,
) -> bool:
    statement = (
        update(Transaction)
        .where(
            Transaction.transaction_id == transaction_id,
            Transaction.status == TransactionStatus.PROCESSING.value,
            Transaction.processing_token == token,
        )
        .values(
            lease_expires_at=None,
            processing_token=None,
            last_error=error[:2000],
        )
        .returning(Transaction.transaction_id)
    )
    try:
        released_id = session.execute(statement).scalar_one_or_none()
        session.commit()
    except Exception:
        session.rollback()
        raise
    return released_id is not None


def mark_dispatched(session: Session, transaction_id: str) -> bool:
    statement = (
        update(Transaction)
        .where(
            Transaction.transaction_id == transaction_id,
            Transaction.status == TransactionStatus.PROCESSING.value,
        )
        .values(dispatched_at=utc_now())
        .returning(Transaction.transaction_id)
    )
    try:
        dispatched_id = session.execute(statement).scalar_one_or_none()
        session.commit()
    except Exception:
        session.rollback()
        raise
    return dispatched_id is not None
