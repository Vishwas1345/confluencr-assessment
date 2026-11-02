import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, func, or_, select

from app.celery_app import celery_app
from app.config import get_settings
from app.db import SessionLocal
from app.models import Transaction, TransactionStatus
from app.services.dispatch import DispatchOutcome, dispatch_transaction
from app.task_names import RECONCILE_TRANSACTIONS_TASK
from app.timestamps import utc_now

logger = logging.getLogger(__name__)

ADVISORY_LOCK_KEY = 0x434F4E46
RECONCILE_BATCH_SIZE = 100


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    candidates: int
    dispatched: int
    skipped: int
    failed: int


def enqueue_reconcile() -> bool:
    try:
        celery_app.send_task(RECONCILE_TRANSACTIONS_TASK, retry=False)
    except Exception:
        logger.exception("reconcile_dispatch_failed", extra={"event": "reconcile_dispatch_failed"})
        return False
    logger.info("reconcile_dispatched", extra={"event": "reconcile_dispatched"})
    return True


def reconcile_undispatched() -> ReconcileResult:
    settings = get_settings()
    now = utc_now()
    stale_before = now - timedelta(seconds=settings.dispatch_stale_seconds)
    statement = (
        select(Transaction.transaction_id)
        .where(
            Transaction.status == TransactionStatus.PROCESSING.value,
            or_(
                Transaction.lease_expires_at < now,
                and_(
                    Transaction.lease_expires_at.is_(None),
                    or_(
                        Transaction.dispatched_at.is_(None),
                        Transaction.dispatched_at < stale_before,
                    ),
                ),
            ),
        )
        .order_by(Transaction.created_at)
        .limit(RECONCILE_BATCH_SIZE)
    )

    with SessionLocal() as session:
        acquired = bool(
            session.execute(select(func.pg_try_advisory_xact_lock(ADVISORY_LOCK_KEY))).scalar_one()
        )
        if not acquired:
            logger.info(
                "reconcile_skipped reason=lock_busy",
                extra={"event": "reconcile_skipped", "reason": "lock_busy"},
            )
            return ReconcileResult(0, 0, 0, 0)
        transaction_ids = list(session.scalars(statement))
        session.commit()

    if not transaction_ids:
        return ReconcileResult(0, 0, 0, 0)

    logger.info(
        "reconcile_started candidates=%s",
        len(transaction_ids),
        extra={"event": "reconcile_started", "candidates": len(transaction_ids)},
    )
    dispatched = 0
    skipped = 0
    failed = 0
    for transaction_id in transaction_ids:
        outcome = dispatch_transaction(transaction_id)
        if outcome == DispatchOutcome.DISPATCHED:
            dispatched += 1
        elif outcome == DispatchOutcome.SKIPPED:
            skipped += 1
        else:
            failed += 1

    logger.info(
        "reconcile_completed candidates=%s dispatched=%s skipped=%s failed=%s",
        len(transaction_ids),
        dispatched,
        skipped,
        failed,
        extra={
            "event": "reconcile_completed",
            "candidates": len(transaction_ids),
            "dispatched": dispatched,
            "skipped": skipped,
            "failed": failed,
        },
    )
    return ReconcileResult(
        candidates=len(transaction_ids),
        dispatched=dispatched,
        skipped=skipped,
        failed=failed,
    )
