import logging
import time

from celery import Task

from app.celery_app import celery_app
from app.config import get_settings
from app.db import SessionLocal
from app.services.reconcile import reconcile_undispatched
from app.services.transactions import (
    claim_transaction,
    complete_transaction,
    release_transaction,
)
from app.task_names import PROCESS_TRANSACTION_TASK, RECONCILE_TRANSACTIONS_TASK

logger = logging.getLogger(__name__)


def process_transaction_impl(transaction_id: str) -> dict[str, str]:
    settings = get_settings()

    with SessionLocal() as session:
        claim = claim_transaction(
            session,
            transaction_id,
            lease_seconds=settings.processing_lease_seconds,
        )
    if claim is None:
        logger.info(
            "transaction_skipped transaction_id=%s reason=not_claimable",
            transaction_id,
            extra={
                "event": "transaction_skipped",
                "transaction_id": transaction_id,
                "reason": "not_claimable",
            },
        )
        return {"transaction_id": transaction_id, "result": "skipped"}

    logger.info(
        "transaction_started transaction_id=%s",
        transaction_id,
        extra={
            "event": "transaction_started",
            "transaction_id": transaction_id,
        },
    )
    try:
        time.sleep(settings.processing_delay_seconds)
        with SessionLocal() as session:
            completed = complete_transaction(session, transaction_id, claim.token)
    except Exception as exc:
        logger.exception(
            "transaction_failed transaction_id=%s",
            transaction_id,
            extra={
                "event": "transaction_failed",
                "transaction_id": transaction_id,
            },
        )
        with SessionLocal() as session:
            release_transaction(session, transaction_id, claim.token, str(exc))
        raise

    if not completed:
        logger.info(
            "transaction_completion_skipped transaction_id=%s reason=stale_token",
            transaction_id,
            extra={
                "event": "transaction_completion_skipped",
                "transaction_id": transaction_id,
                "reason": "stale_token",
            },
        )
        return {"transaction_id": transaction_id, "result": "stale"}

    logger.info(
        "transaction_completed transaction_id=%s",
        transaction_id,
        extra={
            "event": "transaction_completed",
            "transaction_id": transaction_id,
        },
    )
    return {"transaction_id": transaction_id, "result": "processed"}


@celery_app.task(bind=True, name=PROCESS_TRANSACTION_TASK, max_retries=3)
def process_transaction(self: Task, transaction_id: str) -> dict[str, str]:
    try:
        return process_transaction_impl(transaction_id)
    except Exception as exc:
        logger.exception(
            "transaction_retry transaction_id=%s retries=%s",
            transaction_id,
            self.request.retries,
            extra={
                "event": "transaction_retry",
                "transaction_id": transaction_id,
                "retries": self.request.retries,
            },
        )
        raise self.retry(
            exc=exc,
            countdown=min(60, 2**self.request.retries),
        ) from exc


@celery_app.task(name=RECONCILE_TRANSACTIONS_TASK)
def reconcile_transactions() -> dict[str, int]:
    result = reconcile_undispatched()
    return {
        "candidates": result.candidates,
        "dispatched": result.dispatched,
        "skipped": result.skipped,
        "failed": result.failed,
    }
